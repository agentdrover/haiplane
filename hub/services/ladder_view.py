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
    inflight_view,
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
WHY_AXIS_NOT_PLACED = "second_axis_not_placed"
WHY_AXIS_UNRECORDED = "second_axis_unrecorded"
WHY_TOP_UP_NO_REPORT = "top_up_no_report"
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
    WHY_AXIS_NOT_PLACED: "вторая ось не поставлена: {detail} (#1243)",
    WHY_AXIS_UNRECORDED: "попытка второй оси начата, исхода в записях нет (#1243)",
    WHY_TOP_UP_NO_REPORT: "заказ добора кончился без отчёта (статус {detail})",
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
    #: Подробность, взятая из записи о КОНКРЕТНОМ заказе или попытке.
    detail: str = ""

    @property
    def text(self) -> str:
        return _TEXTS[self.code].format(detail=self.detail or UNKNOWN_TEXT)

    def line(self) -> str:
        return f"{self.code}: {self.text}"


async def _latest_report(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> dict[str, Any] | None:
    reports = await repo.machine_reviews_of_generation(db, task_id, generation)
    return dict(reports[-1]) if reports else None


async def _ladder_acts(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> list[dict[str, Any]]:
    """Поступки лестницы по этой сдаче в порядке записи: добор и попытки второй оси.

    Каждый поступок — событие с ``after_review_id`` (после КАКОГО отчёта он
    совершён) и ``dispatch_id``: связь записью, а не выводом из времени и
    порядка строк (#1925, круг 2).
    """
    rows = await fetchall(
        db,
        "SELECT * FROM events WHERE task_id=? AND kind IN (?, ?) "
        "AND json_extract(payload, '$.generation')=? ORDER BY id",
        (
            task_id,
            dispatcher.LADDER_TOP_UP_EVENT,
            dispatcher.MODEL_CASCADE_EVENT,
            generation,
        ),
    )
    acts = []
    for row in rows:
        act = dict(row)
        try:
            act["data"] = json.loads(act["payload"] or "{}")
        except ValueError:
            act["data"] = None  # запись нечитаема — не доказательство
        acts.append(act)
    return acts


def _placed(act: dict[str, Any]) -> bool:
    """Поступок поставил заказ (у отказанной попытки второй оси заказа нет)."""
    return act["data"] is not None and not dispatcher.cascade_failed(act["payload"])


async def top_up_in_flight(db: aiosqlite.Connection, task: dict[str, Any]) -> str:
    """Строка о полёте, если у поколения неполный отчёт и добор ещё идёт; иначе "".

    «Идёт» — лестница ПОСТАВИЛА добор после неполного отчёта этой сдачи
    (запись поступка с ``after_review_id`` такого отчёта) и последний заказ
    поколения ещё активен или это долг второй двери: статус заказа ведёт сам
    хаб по своей связи отчёта с заказом. Время создания строк и порядок
    отчётов связью не служат: чужой отчёт или отчёт той же секунды заказа не
    отвечает. Активный заказ без поступка лестницы (первичный, лёгкий прогон,
    уже отдавший отчёт) добором не считается — лестница его ещё только может
    поставить (граница #1925).

    Поле ``incomplete`` пусто или нечитаемо — не «неполный»: ждать нечего, так
    же читает его гейт (``bool(None)``).
    """
    task_id = int(task["id"])
    generation = int(task.get("submission_generation") or 0)
    reports = await repo.machine_reviews_of_generation(db, task_id, generation)
    if not reports or not dict(reports[-1]).get("incomplete"):
        return ""
    view = await inflight_view(db, task, include_owed=True)
    if view is None:
        return ""
    if any(_placed(act) for act in await _ladder_acts(db, task_id, generation)):
        return view.headline
    return ""


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


async def _recorded_outcome(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> str:
    """Код исхода из НОВЕЙШЕГО алерта лестницы ЭТОЙ сдачи или "" — записи нет.

    Алерт принадлежит сдаче, если несёт её хвост ``[сдача N]`` (или метку
    второй оси с номером поколения): время записи сдачу не определяет.
    """
    tag = f"сдача {generation}]"  # хвост [сдача N] и метки второй оси с номером
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
        "SELECT content FROM task_updates WHERE task_id=? AND kind='alert' "
        "ORDER BY id DESC",
        (task_id,),
    )
    generic = ""
    for row in rows:
        text = str(row["content"] or "")
        if tag not in text:
            continue  # алерт другой сдачи (или без хвоста) — не наш
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
    if await top_up_in_flight(db, task):
        return LadderWhy(WHY_IN_FLIGHT)
    acts = await _ladder_acts(db, task_id, generation)
    acted = await _last_act_outcome(db, acts)
    if acted is not None:
        return acted
    code = await _recorded_outcome(db, task_id, generation)
    if code in (WHY_AXIS_NO_REPORT, WHY_AXIS_EXHAUSTED):
        return _axis_code(acts, code)
    return LadderWhy(code or WHY_UNKNOWN)


def _axis_code(acts: list[dict[str, Any]], alert_code: str) -> LadderWhy:
    """Исход второй оси по алерту при поставленной (и ответившей) попытке.

    Отказанную последнюю попытку разбирает ``_last_act_outcome`` раньше любого
    алерта: она не запускалась, и «тоже не дочитала» про неё сказать нельзя.
    """
    axis = [a for a in acts if a["kind"] == dispatcher.MODEL_CASCADE_EVENT]
    if not axis:
        # Алерт исчерпания сам запись; маркер попытки без события — исход неизвестен.
        return LadderWhy(
            WHY_AXIS_EXHAUSTED
            if alert_code == WHY_AXIS_EXHAUSTED
            else WHY_AXIS_UNRECORDED
        )
    if axis[-1]["data"] is None:
        return LadderWhy(WHY_UNKNOWN)
    if alert_code == WHY_AXIS_EXHAUSTED:
        return LadderWhy(WHY_AXIS_EXHAUSTED)
    return LadderWhy(WHY_AXIS_INCOMPLETE)


async def _last_act_outcome(
    db: aiosqlite.Connection, acts: list[dict[str, Any]]
) -> LadderWhy | None:
    """Исход ПОСЛЕДНЕГО поступка лестницы по его записи, если он ничего не дал.

    Отказанная попытка не
    запускалась (причина — в её записи), поставленный заказ кончился без отчёта
    (связь — ``after_review_id`` и принципал заказа, ``cascade_outcome``).
    """
    if not acts or acts[-1]["data"] is None:
        return None
    last = acts[-1]
    if not _placed(last):
        return LadderWhy(WHY_AXIS_NOT_PLACED, str(last["data"].get("reason") or ""))
    if await dispatcher.cascade_outcome(db, last) != "pending":
        return None
    if last["kind"] == dispatcher.MODEL_CASCADE_EVENT:
        return LadderWhy(WHY_AXIS_NO_REPORT)
    rows = await fetchall(
        db,
        "SELECT status FROM review_dispatches WHERE id=?",
        (int(last["data"].get("dispatch_id") or 0),),
    )
    status = str(dict(rows[0])["status"] or "") if rows else ""
    return LadderWhy(WHY_TOP_UP_NO_REPORT, status)


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
