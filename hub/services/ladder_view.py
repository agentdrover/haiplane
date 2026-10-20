"""Лестница добора ревью глазами читателей: «добор в полёте?» и «почему не помог» (#1925).

Лестница (#879) и вторая ось (#1243) докупают прогон, когда отчёт объявил
себя неполным. Стюард читал любой отчёт поколения как готовый и мог быть
заказан через секунды после неполного lite, пока заказанный deep ещё шёл
(#1910 g1). А когда неполный отчёт всё же доезжал до эскалации
``report_incomplete``, владелец не видел, почему добор не помог.

Здесь два ЧИТАТЕЛЯ, оба только читают и ничего не решают за лестницу:

* :func:`top_up_in_flight` — у поколения есть неполный отчёт и заказ, который
  своего отчёта ещё не отдал;
* :func:`ladder_why` — исход лестницы словами, взятыми из уже записанных ею
  алертов и строк заказов. Нет данных — «причина не установлена», а не пусто.

Метки алертов — константы review_dispatch: текст и читатель не разойдутся.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall
from hub.services import review_dispatch as dispatcher
from hub.services.gate_grounds import LADDER_WHY_UNKNOWN
from hub.services.review_evidence import (
    TERMINAL_REPORT_READY,
    WAIT_KIND_TOP_UP,
    WAIT_PENDING,
    WAIT_TERMINAL,
    ReviewWait,
    generation_submitted_at,
    inflight_view,
    parse_stamp,
)

#: Коды причин; в ленте и справке они стоят рядом с человеческой фразой.
WHY_ENVIRONMENT = "environment"
WHY_IN_FLIGHT = "top_up_in_flight"
WHY_DEEP_CAP = "deep_cap"
WHY_CIRCLE = "review_circle"
WHY_CEILING = "ladder_ceiling"
WHY_NO_TOP_UP = "no_top_up_slot"
WHY_ORDER_FAILED = "top_up_not_ordered"
WHY_AXIS_EXHAUSTED = "second_axis_exhausted"
WHY_AXIS_NO_REPORT = "second_axis_no_report"
WHY_AXIS_INCOMPLETE = "second_axis_incomplete"
WHY_AXIS_UNAVAILABLE = "second_axis_unavailable"
WHY_LOCAL_DEEP = "local_deep_no_cloud"
WHY_NOT_INCOMPLETE = "report_not_incomplete"
WHY_UNKNOWN = "unknown"

UNKNOWN_TEXT = LADDER_WHY_UNKNOWN
#: Слова описания ожидания добора: попадают в основание заказа судьи.
TOP_UP_WAIT_PHRASE = "ждёт добора ревью"

_TEXTS = {
    WHY_ENVIRONMENT: "отказ среды ревьюера (#1238): повтор в той же среде даст "
    "тот же отказ",
    WHY_IN_FLIGHT: "добор ещё шёл, когда вышел срок ожидания стюарда "
    "(STEWARD_REVIEW_WAIT_MAX)",
    WHY_DEEP_CAP: "суточный потолок облачного deep исчерпан, добор не куплен (#1414)",
    WHY_CIRCLE: "круг ревью остановил deep до решения человека, добор не куплен "
    "(#1432)",
    WHY_CEILING: "потолок ступеней лестницы достигнут, третий прогон не покупается "
    "(#879)",
    WHY_NO_TOP_UP: "добор не положен: профиль отчёта выше дешёвого и вторая ось "
    "не пускает (#879)",
    WHY_ORDER_FAILED: "добор тяжёлым профилем поставить не удалось (#879)",
    WHY_AXIS_EXHAUSTED: "вторая ось (другая модель) исчерпана и тоже не дочитала "
    "(#1243)",
    WHY_AXIS_NO_REPORT: "вторая ось заказана, но её отчёта нет (#1243)",
    WHY_AXIS_INCOMPLETE: "вторая ось дала такой же неполный отчёт (#1243)",
    WHY_AXIS_UNAVAILABLE: "вторая ось недоступна: нет подходящей модели или форжа "
    "(#1243)",
    WHY_LOCAL_DEEP: "неполный отчёт локального deep облаком не лечится (#1587)",
    WHY_NOT_INCOMPLETE: "отчёт заявлен полным: неполнота не от лестницы",
    WHY_UNKNOWN: f"{UNKNOWN_TEXT}: записи лестницы по этой сдаче в ленте нет",
}


@dataclass(frozen=True)
class LadderWhy:
    """Почему добор не помог: код и фраза для ленты и справки."""

    code: str

    @property
    def text(self) -> str:
        return _TEXTS[self.code]

    def line(self) -> str:
        return f"{self.code}: {self.text}"


async def _latest_report(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> dict[str, Any] | None:
    reports = await repo.machine_reviews_of_generation(db, task_id, generation)
    return dict(reports[-1]) if reports else None


async def _unanswered_dispatch(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> tuple[dict[str, Any] | None, bool]:
    """Последний заказ поколения и то, что он свой отчёт уже отдал."""
    row = await repo.get_review_dispatch_for_generation(db, task_id, generation)
    if row is None:
        return None, False
    dispatch = dict(row)
    if dispatch.get("reviewer_principal_id") is None:
        # Заказ без принципала (открытый режим, история): правило «чей отчёт»
        # отдаёт ему ЛЮБОЙ отчёт поколения, и добор после лёгкого прогона
        # выглядел бы отвеченным. Здесь ступени считаются, как их считает сама
        # лестница: платных ступеней больше, чем отчётов, — последняя ещё в пути.
        steps = await repo.count_review_dispatches(db, task_id, generation)
        reports = await repo.machine_reviews_of_generation(db, task_id, generation)
        return dispatch, len(reports) >= steps
    own = await dispatcher.report_of_dispatch(db, task_id, generation, dispatch)
    return dispatch, own is not None


async def top_up_in_flight(db: aiosqlite.Connection, task: dict[str, Any]) -> str:
    """Строка о полёте, если у поколения неполный отчёт и добор ещё идёт; иначе "".

    «Идёт» — последний заказ поколения активен (или это долг второй двери) и
    СВОЕГО отчёта не отдал. Активная строка лёгкого прогона, уже отдавшего
    отчёт, не добор: лестница свой заказ ещё только может поставить, и этот
    случай читатель не ждёт (граница задачи #1925). У заказа без принципала
    (открытый режим, история) «свой» отчёт считается по числу платных ступеней.

    Поле ``incomplete`` пусто или нечитаемо — не «неполный»: ждать нечего, так
    же читает его гейт (``bool(None)``).
    """
    task_id = int(task["id"])
    generation = int(task.get("submission_generation") or 0)
    report = await _latest_report(db, task_id, generation)
    if report is None or report.get("incomplete") is None:
        return ""
    if not report["incomplete"]:
        return ""
    view = await inflight_view(db, task, include_owed=True)
    if view is None:
        return ""
    dispatch, answered = await _unanswered_dispatch(db, task_id, generation)
    if dispatch is None or answered:
        return ""
    return view.headline


async def wait_with_report(db, task: dict[str, Any]) -> ReviewWait:
    """Отчёт есть. Неполный отчёт при доборе в полёте — ещё не готовый (#1925).

    Заказанный добор отдаст отчёт, который судья должен прочитать; заказать
    его раньше значит получить report_incomplete на данных, которые вот-вот
    дополнятся (#1910 g1). Срок тот же — STEWARD_REVIEW_WAIT_MAX от сдачи.
    """
    flight = await top_up_in_flight(db, task)
    if flight:
        return ReviewWait(
            WAIT_PENDING,
            "",
            f"{TOP_UP_WAIT_PHRASE}: отчёт неполон, {flight}",
            kind=WAIT_KIND_TOP_UP,
        )
    return ReviewWait(WAIT_TERMINAL, TERMINAL_REPORT_READY, "отчёт этой сдачи есть")


async def _order_was_placed_on_a_wait(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> bool:
    """Судья этой сдачи заказан по сроку ожидания добора — основание в записи заказа.

    Добор мог кончиться (упал, отчёт лёг позже) уже после заказа; сохранённое
    основание не меняется вместе с состоянием заказа добора.
    """
    from hub.services.steward_dispatch import EVENT_ORDERED

    rows = await fetchall(
        db,
        "SELECT payload FROM events WHERE task_id=? AND kind=? "
        "AND json_extract(payload, '$.generation')=? "
        "AND json_extract(payload, '$.kind')='verdict' LIMIT 1",
        (task_id, EVENT_ORDERED, generation),
    )
    if not rows:
        return False
    try:
        why = str(json.loads(rows[0]["payload"]).get("why") or "")
    except ValueError:
        return False  # нечитаемая запись — не доказательство ожидания
    return TOP_UP_WAIT_PHRASE in why


async def _recorded_outcome(
    db: aiosqlite.Connection, task_id: int, generation: int, since: Any
) -> str:
    """Код исхода из НОВЕЙШЕГО алерта лестницы этой сдачи или "" — записи нет."""
    marks = (
        (dispatcher.FORCED_DEEP_CAP_MARK, WHY_DEEP_CAP),
        (dispatcher.FORCED_DEEP_CIRCLE_MARK, WHY_CIRCLE),
        (dispatcher.LADDER_CEILING_MARK, WHY_CEILING),
        (
            dispatcher.MODEL_CASCADE_EXHAUSTED_MARK.format(generation=generation),
            WHY_AXIS_EXHAUSTED,
        ),
        (dispatcher.NO_CLOUD_AFTER_LOCAL_MARK, WHY_LOCAL_DEEP),
        (dispatcher.SECOND_AXIS_UNAVAILABLE_MARK, WHY_AXIS_UNAVAILABLE),
        (dispatcher.LADDER_NO_TOP_UP_MARK, WHY_NO_TOP_UP),
        (dispatcher.LADDER_ORDER_FAILED_MARK, WHY_ORDER_FAILED),
        (
            dispatcher.MODEL_CASCADE_MARK.format(generation=generation),
            WHY_AXIS_NO_REPORT,
        ),
    )
    rows = await fetchall(
        db,
        "SELECT content, created_at FROM task_updates "
        "WHERE task_id=? AND kind='alert' ORDER BY id DESC",
        (task_id,),
    )
    generic = ""
    for row in rows:
        stamp = parse_stamp(str(row["created_at"] or ""))
        if stamp is None or stamp < since:
            continue  # метка нечитаема или алерт прежней сдачи — не наш
        text = str(row["content"] or "")
        for mark, code in marks:
            if mark in text:
                if code == WHY_ORDER_FAILED:
                    # Общий алерт «поставить не удалось» лестница пишет ПОСЛЕ
                    # конкретного отказа (потолок deep, круг): конкретный
                    # старше, но он и есть причина.
                    generic = generic or code
                    break
                return code
    return generic


async def ladder_why(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> LadderWhy | None:
    """Почему лестница не довела отчёт текущей сдачи до полного; None — отчёта нет.

    Порядок: заявленный отказ среды (по самому отчёту); добор ещё в полёте
    (значит, судью заказал срок ожидания); иначе новейший алерт лестницы за
    эту сдачу. Ничего из этого — «причина не установлена». Читает только то,
    что лестница записала сама, и новых решений не принимает.
    """
    task_id = int(task["id"])
    generation = int(task.get("submission_generation") or 0)
    report = await _latest_report(db, task_id, generation)
    if report is None:
        return None
    if report.get("incomplete") is None:
        return LadderWhy(WHY_UNKNOWN)
    if not report["incomplete"]:
        return LadderWhy(WHY_NOT_INCOMPLETE)
    if dispatcher.is_environment_refusal(report):
        return LadderWhy(WHY_ENVIRONMENT)
    if await top_up_in_flight(db, task) or await _order_was_placed_on_a_wait(
        db, task_id, generation
    ):
        return LadderWhy(WHY_IN_FLIGHT)
    since = await generation_submitted_at(db, task_id, generation)
    if since is None:
        return LadderWhy(WHY_UNKNOWN)  # без метки сдачи алерты не отнести к ней
    code = await _recorded_outcome(db, task_id, generation, since)
    if code == WHY_AXIS_NO_REPORT:
        _, answered = await _unanswered_dispatch(db, task_id, generation)
        if answered:
            code = WHY_AXIS_INCOMPLETE
    return LadderWhy(code or WHY_UNKNOWN)


async def incomplete_escalation_line(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> str:
    """Фраза для ленты и справки при эскалации report_incomplete (#1925).

    Сбой чтения не прячется под «нет добора»: он назван, а запись суждения
    из-за него не падает — причина не стоит жизни суждения.
    """
    try:
        why = await ladder_why(db, task)
    except Exception as exc:  # noqa: BLE001 - причина не стоит суждения
        return (
            f"Почему лестница добора не помогла: {UNKNOWN_TEXT}, чтение записей "
            f"лестницы не удалось ({type(exc).__name__})."
        )
    if why is None:
        return (
            f"Почему лестница добора не помогла: {UNKNOWN_TEXT}, отчёта текущей "
            "сдачи нет."
        )
    return f"Почему лестница добора не помогла: {why.line()}."
