"""Страж ожидания судьи стюарда: ждать ли отчёт ревью, добор и CI (#1289, #1600, #1695, #1925).

Вынесено из steward_dispatch, когда тот упёрся в порог размера файла: один
вопрос «рано ли заказывать или стартовать судью» живёт здесь, а заказ,
старт и отсрочка в ленте — там. steward_dispatch реэкспортирует имена.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from hub import config
from hub.db import fetchall

# Своё слово, а не steward_run_refused (#1289). Отказ РЕШАЕТ генерацию:
# рядом с ним стоит строка steward_runs, и второго заказа не будет никогда.
# Отсрочка не решает ничего — она говорит «ещё рано», строки не пишет, и
# следующий проход поллера обязан вернуться к этой задаче. Назвать их одним
# словом значило бы потерять ровно ту разницу, ради которой это заведено.
EVENT_DEFERRED = "steward_run_deferred"
#: Вид ожидания в записи отсрочки (#1925): по умолчанию — отчёт ревью.
DEFER_KIND_REVIEW = "review"


@dataclass(frozen=True)
class ReviewGate:
    """Ответ стража ожидания отчёта: ждать (``blocker``) или заказывать.

    ``ci_note`` (#1695) — окно кончилось, а CI сдачи всё ещё не подтверждён:
    вызывающий пишет одну строку в ленту и судит.

    ``because`` — что назвать в ленте, когда заказ идёт НЕ на готовом отчёте:
    окончательный исход ревью или «ждали отчёт N мин». Пусто — отчёт есть.
    """

    blocker: str = ""
    because: str = ""
    ci_note: str = ""  # #1695: судим, не дождавшись подтверждённого CI
    #: Вид ожидания для дедупа отсрочки (#1925); пусто — «ревью».
    wait_kind: str = ""


def _waited_minutes(since: datetime | None, deferred_at: str) -> tuple[int, bool]:
    """(минут от сдачи, метка нечитаема). Нет сдачи в учёте — от первой отсрочки.

    Нечитаемая метка отсрочки — «неизвестно», а не «сейчас» (#1600): отсчёт
    «от сейчас» начинался бы заново на каждом тике, и потолок был бы
    недостижим. Вызывающий читает второй элемент как «потолок достигнут».
    """
    from hub.services.review_evidence import parse_stamp

    start = since
    if start is None and deferred_at:
        start = parse_stamp(deferred_at)
        if start is None:
            return 0, True
    if start is None:
        return 0, False
    return max(0, int((datetime.now(UTC) - start).total_seconds() // 60)), False


async def _first_deferral_stamp(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> str:
    rows = await fetchall(
        db,
        "SELECT created_at FROM events WHERE task_id=? AND kind=? "
        "AND json_extract(payload, '$.generation')=? ORDER BY id LIMIT 1",
        (task_id, EVENT_DEFERRED, generation),
    )
    return str(dict(rows[0]).get("created_at") or "") if rows else ""


async def review_wait_gate(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> ReviewGate:
    """Почему судью рано заказывать или стартовать, а если пора — на каком основании.

    Наблюдено 22.09.2026, первый день тени на default: из восьми суждений
    три — эскалации no_current_report, и две из них (#1283, #1286)
    искусственные. Прогон заказывали через минуту после сдачи, пока
    кросс-модельное ревью ЭТОЙ сдачи ещё работало; gate_grounds читает
    отчёт первым и при его отсутствии эскалирует сразу. Исход такого
    прогона предрешён до его начала — а стоит он денег и суточной квоты, и
    портит измерение: доля эскалаций и есть порог выхода стюарда из тени.

    #1600: страж #1289 видел только идущий заказ и пропускал окно «ревью
    положено, но ещё не заказано» — 81 из 106 эскалаций остались
    no_current_report. Теперь ответ даёт ОДИН читатель ожидания,
    ``review_evidence.review_wait_view``: pending (положено и ещё может
    прийти) или terminal с причиной. Терминальность — предикаты диспетчера
    ревью, а не копия их условий; здесь она только читается.

    Пока pending, заказ ждёт — но не вечно: больше STEWARD_REVIEW_WAIT_MAX
    минут от сдачи — и прогон покупается с причиной «ждали отчёт N мин».
    Ожидание не стоит ни прогона, ни квоты. Тот же вопрос задаёт КАЖДАЯ
    попытка старта (steward_shadow.start_run): заказ мог быть размещён, пока
    отчёт был не нужен, а повтор после отказа происходит позже.
    """
    from hub.services.review_evidence import review_wait_view

    wait = await review_wait_view(db, task)
    gate = await _review_report_gate(db, task, wait)
    if gate.blocker:
        return gate
    return await _ci_wait_gate(db, task, wait.since, gate)


async def _ci_wait_gate(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    since: datetime | None,
    gate: ReviewGate,
) -> ReviewGate:
    """#1695: отчёт ревью не держит — держит ли CI. Окно то же, без нового.

    Судья, стартовавший при неполном CI, выносит escalate ci_pinned_sha
    absent, а суждение на поколение одно (#1022): поздний verified его не
    исправит. Ждём до STEWARD_REVIEW_WAIT_MAX от сдачи, как ждём отчёт ревью.
    """
    from hub.services.steward_ci_wait import ci_wait_view

    ci = await ci_wait_view(db, task)
    if not ci.waiting:
        if not ci.state:
            return gate
        # #2027: ждать нечего, но CI не подтверждён — судим и говорим почему.
        note = f"стюард судит без подтверждённого CI: {ci.state}"
        return ReviewGate(because=gate.because or note, ci_note=note)
    task_id = int(task["id"])
    generation = int(task.get("submission_generation") or 0)
    stamp = "" if since else await _first_deferral_stamp(db, task_id, generation)
    waited, unreadable = _waited_minutes(since, stamp)
    if not unreadable and waited < config.STEWARD_REVIEW_WAIT_MAX:
        return ReviewGate(
            blocker=f"CI этой сдачи ещё не подтверждён ({ci.state}) — судья "
            "прочитал бы неполные данные и эскалировал, а суждение на поколение "
            "одно; заказ ждёт отчёта CI",
            because=gate.because,
        )
    note = (
        f"стюард судит без подтверждённого CI: ждали {waited} мин "
        f"(потолок {config.STEWARD_REVIEW_WAIT_MAX}), состояние: {ci.state}"
    )
    return ReviewGate(because=gate.because or note, ci_note=note)


def _wait_detail(wait: Any) -> str:
    """Описание ожидания для основания заказа судьи, когда потолок вышел (#1946)."""
    from hub.services.review_evidence import WAIT_KIND_TOP_UP

    if wait.kind != WAIT_KIND_TOP_UP:
        return str(wait.detail)
    from hub.services.ladder_view import after_ceiling

    return after_ceiling(str(wait.detail))


def _review_blocker(wait: Any) -> str:
    """Фраза отсрочки: что именно ждём, и чем кончился бы ранний прогон."""
    from hub.services.review_evidence import WAIT_KIND_TOP_UP

    if wait.kind == WAIT_KIND_TOP_UP:
        return (
            f"неполный отчёт этой сдачи ждёт добора ({wait.detail}) — прогон "
            "прочитал бы неполные данные и эскалировал по report_incomplete, "
            "хотя добор вот-вот их дополнит; заказ ждёт добора"
        )
    return (
        f"ревью этой сдачи ещё не кончено ({wait.detail}) — прогон "
        "прочитал бы отсутствие отчёта и эскалировал по no_current_report, "
        "не начав судить; заказ ждёт отчёта"
    )


async def _review_report_gate(
    db: aiosqlite.Connection, task: dict[str, Any], wait: Any
) -> ReviewGate:
    """Часть стража про отчёт машинного ревью (#1289, #1600)."""
    if wait.pending:
        task_id = int(task["id"])
        generation = int(task.get("submission_generation") or 0)
        stamp = (
            "" if wait.since else await _first_deferral_stamp(db, task_id, generation)
        )
        waited, unreadable = _waited_minutes(wait.since, stamp)
        if unreadable:
            return ReviewGate(
                because="метка отсрочки нечитаема: сколько ждали, неизвестно, "
                f"потолок {config.STEWARD_REVIEW_WAIT_MAX} мин считаем "
                f"достигнутым: {_wait_detail(wait)}"
            )
        if waited < config.STEWARD_REVIEW_WAIT_MAX:
            return ReviewGate(blocker=_review_blocker(wait), wait_kind=wait.kind)
        return ReviewGate(
            because=f"ждали отчёт {waited} мин (потолок "
            f"{config.STEWARD_REVIEW_WAIT_MAX}): {_wait_detail(wait)}"
        )
    if wait.reason == "report_ready":
        return ReviewGate()
    return ReviewGate(because=f"ревью окончательно: {wait.reason} — {wait.detail}")
