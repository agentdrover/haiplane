"""Решения человека во входящих: действие, основание, порядок (#1501).

Входящие показывали задачи, но не то, ЧТО именно от человека ждут, на чём это
стоит и в каком порядке это делать; черновики резались на 20 молча, а задачи
на ревью, где вердикт за человеком, во входящие не попадали вовсе.

Здесь ни одного своего правила:

* порядок черновиков — ``project_path.order_nodes``, тот же расчёт по
  ``depends_on`` (#1527), с рангом #253 (``DRAFT_QUEUE_ORDER_BY``) внутри
  уровня; родитель идёт раньше ребёнка;
* «вердикт у человека» — ответ ``verdict_route`` (#1440), без пересчёта;
* сводка ревью — те же читатели, что у брифа: ``review_evidence.report_view``
  и ``review_availability.generation_review``; CI — отчёт о прогоне на
  закреплённом sha;
* причина остановки needs_decision — ``review_queue.last_stall``.

Лимит не молчит: показано всё до ``DECISION_CAP``, а при обрезке строка
«показано N из M» называет разницу.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall
from hub.services import project_path, review_evidence, state_review
from hub.services.result_kind import automation_not_applicable
from hub.services.ci_report import (
    VALIDATION_FAIL,
    VALIDATION_PASS,
    ci_report_state,
)
from hub.services.review_availability import generation_review
from hub.services.review_queue import last_stall
from hub.services.verdict_route import DECIDER_HUMAN, verdict_route

ACTION_APPROVE = "approve"
ACTION_ANSWER = "answer"
ACTION_VERDICT = "verdict"
ACTION_DECIDE = "decide"

#: Что человек делает раньше: вердикт и решение держат уже сданную работу.
ACTION_ORDER = (ACTION_VERDICT, ACTION_DECIDE, ACTION_ANSWER, ACTION_APPROVE)

#: Сколько строк каждого вида показывать; больше — только с «показано N из M».
DECISION_CAP = 500


async def _nearest_in_set(
    db: aiosqlite.Connection, row: Any, ids: set[int], seen: dict[int, int | None]
) -> int | None:
    """Ближайший предок ``row`` среди ``ids`` — родитель раньше ребёнка."""
    current = row["parent_id"]
    for _ in range(20):
        if current is None:
            return None
        current = int(current)
        if current in ids:
            return current
        if current not in seen:
            parent = await repo.get_task(db, current)
            seen[current] = parent["parent_id"] if parent is not None else None
        current = seen[current]
    return None


async def _draft_edges(
    db: aiosqlite.Connection, rows: list[Any]
) -> dict[int, list[int]]:
    ids = {int(r["id"]) for r in rows}
    edges: dict[int, list[int]] = {}
    if not ids:
        return edges
    marks = ",".join("?" for _ in ids)
    for dep in await fetchall(
        db,
        "SELECT task_id, depends_on_task_id FROM task_dependencies "  # nosec B608
        f"WHERE task_id IN ({marks}) AND depends_on_task_id IN ({marks}) "
        "ORDER BY task_id, depends_on_task_id",
        (*ids, *ids),
    ):
        edges.setdefault(int(dep["task_id"]), []).append(int(dep["depends_on_task_id"]))
    ancestors: dict[int, int | None] = {}
    for row in rows:
        parent = await _nearest_in_set(db, row, ids, ancestors)
        if parent is not None:
            edges.setdefault(int(row["id"]), []).append(parent)
    return edges


async def order_drafts(
    db: aiosqlite.Connection, rows: list[Any]
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Черновики топологически; ``rows`` уже в порядке DRAFT_QUEUE_ORDER_BY (#253)."""
    by_id = {int(r["id"]): r for r in rows}
    tasks = {i: {"id": i, "rank": n} for n, i in enumerate(by_id)}
    order, _, cycles = project_path.order_nodes(
        set(by_id),
        await _draft_edges(db, rows),
        tasks,
        key=lambda t: t["rank"],
    )
    return [by_id[i] for i in order], cycles


def _draft_grounds(row: Any) -> str:
    score = row["readiness_score"]
    ready = "не посчитан" if score is None else str(score)
    if row["dor_passed"]:
        return f"DoR пройден (dor_passed), readiness {ready}"
    if score is None:
        return "DoR не пройден: черновик не доработан, readiness не посчитан"
    return f"DoR не пройден, readiness {ready}"


async def _question_grounds(db: aiosqlite.Connection, row: Any) -> str:
    updates = await repo.get_task_updates(db, int(row["id"]))
    asked = [u["content"] for u in updates if u["kind"] == "question"]
    return str(asked[-1])[:200] if asked else "вопрос в ленте не найден"


async def _decide_grounds(db: aiosqlite.Connection, row: Any) -> str:
    reason, _ = await last_stall(
        db, int(row["id"]), str(row["status_entered_at"] or "")
    )
    return reason[:300] if reason else "причина остановки в ленте не записана"


async def _ci_ground(db: aiosqlite.Connection, task: dict[str, Any]) -> str:
    pinned = str(task.get("submission_sha") or "").strip()
    if not pinned:
        return "CI: коммит сдачи не закреплён"
    report = await repo.get_ci_run_report(db, int(task["id"]), pinned)
    if report is None:
        _, reason = await ci_report_state(db, task)
        return f"CI: {reason}"
    status = str(dict(report).get("validation_status") or "").strip()
    if status == VALIDATION_PASS:
        return f"CI зелёный на {pinned[:12]}"
    if status == VALIDATION_FAIL:
        return f"CI красный на {pinned[:12]}"
    return f"CI на {pinned[:12]}: итог проверки не назван"


async def _verdict_grounds(
    db: aiosqlite.Connection, task: dict[str, Any], route_line: str
) -> tuple[str, bool]:
    """(основание, предлагать ли вердикт): только по полному отчёту текущего поколения."""
    if automation_not_applicable(task):
        # #1648: у задачи-состояния машинного отчёта не будет — вердикт
        # предлагается по полному комплекту доказательств ТЕКУЩЕГО поколения.
        view = await state_review.state_view_of(db, task)
        if view.evidence_complete:
            return (
                f"доказательства поколения {view.generation} по "
                f"{len(view.evidence)} AC, rollback "
                f"{'назван' if view.rollback else 'не назван'}; {route_line}",
                True,
            )
        return f"{view.headline}; {route_line}", False
    report = await review_evidence.report_view(
        db, task, await repo.get_latest_machine_review(db, int(task["id"]))
    )
    review = report.machine_review
    generation = await generation_review(db, task)
    ci = await _ci_ground(db, task)
    if review is None or not review.is_current or not generation.has_review:
        return f"отчёт не пришёл: {generation.headline}; {ci}; {route_line}", False
    if generation.complete is not True:
        return (
            f"отчёт не пришёл полным: {generation.headline}; {ci}; {route_line}",
            False,
        )
    counts = (
        f"{len(review.findings_confirmed)}/{len(review.unresolved)}"
        f"/{len(review.findings_rejected)}"
    )
    return (
        f"ревью поколения {generation.generation}: confirmed/unresolved/rejected "
        f"{counts}; {ci}; {route_line}",
        True,
    )


async def _verdict_rows(
    db: aiosqlite.Connection, rows: list[Any]
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Задачи на ревью, где вердикт за человеком по маршруту вердикта (#1440)."""
    kept: list[Any] = []
    entries: list[dict[str, Any]] = []
    for row in rows:
        route = await verdict_route(db, int(row["id"]))
        if route.final != DECIDER_HUMAN:
            continue
        grounds, offer = await _verdict_grounds(db, dict(row), route.as_dict()["line"])
        kept.append(row)
        entries.append(
            {
                "task_id": int(row["id"]),
                "action": ACTION_VERDICT,
                "grounds": grounds,
                "offer": offer,
            }
        )
    return kept, entries


def _entry(row: Any, action: str, grounds: str) -> dict[str, Any]:
    return {
        "task_id": int(row["id"]),
        "action": action,
        "grounds": grounds,
        "offer": True,
    }


async def _fetch(
    db: aiosqlite.Connection, status: str, order_by: str, scoped: dict[str, Any]
) -> list[Any]:
    """ВСЕ строки статуса: размер ограничен самим статусом, а не окном.

    Режет только показ, и после порядка и фильтра (#1501, раунд 2): окно до
    топологии теряет родителя за краем, окно до фильтра вердикта вытесняет
    человеческий вердикт делегированными. ``LIMIT -1`` в SQLite — без предела.
    """
    return list(
        await repo.list_tasks_by_status(
            db, status, order_by=order_by, limit=-1, **scoped
        )
    )


def _numbered(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rank = {name: i for i, name in enumerate(ACTION_ORDER)}
    ordered = sorted(entries, key=lambda e: rank[e["action"]])
    for number, entry in enumerate(ordered, start=1):
        entry["order"] = number
    return ordered


def _cap(items: list[Any]) -> list[Any]:
    return items[:DECISION_CAP]


async def collect(db: aiosqlite.Connection, scoped: dict[str, Any]) -> dict[str, Any]:
    """Всё, что человек решает во входящих: строки, их порядок и честная обрезка."""
    drafts, cycles = await order_drafts(
        db, await _fetch(db, "draft", repo.DRAFT_QUEUE_ORDER_BY, scoped)
    )
    asked = await _fetch(db, "needs_info", "id DESC", scoped)
    decide = await _fetch(db, "needs_decision", "id DESC", scoped)
    reviewing, verdicts = await _verdict_rows(
        db, await _fetch(db, "review", "id DESC", scoped)
    )
    # Обрезка — последним шагом и по уже отфильтрованному: M считается после
    # фильтра маршрута, делегированное в «из M» не входит.
    totals = {
        "черновики": len(drafts),
        "вопросы": len(asked),
        "решения": len(decide),
        "ревью": len(reviewing),
    }
    drafts, asked, decide = _cap(drafts), _cap(asked), _cap(decide)
    reviewing, verdicts = _cap(reviewing), _cap(verdicts)
    shown = {
        "черновики": len(drafts),
        "вопросы": len(asked),
        "решения": len(decide),
        "ревью": len(reviewing),
    }
    entries = [_entry(r, ACTION_APPROVE, _draft_grounds(r)) for r in drafts]
    entries += [_entry(r, ACTION_ANSWER, await _question_grounds(db, r)) for r in asked]
    entries += [_entry(r, ACTION_DECIDE, await _decide_grounds(db, r)) for r in decide]
    cut = [
        f"{label}: показано {shown[label]} из {total}"
        for label, total in totals.items()
        if shown[label] < total
    ]
    return {
        "drafts": drafts,
        "asked": asked,
        "decide": decide,
        "reviewing": reviewing,
        "queue": _numbered(entries + verdicts),
        "note": "; ".join(cut),
        "cycles": cycles,
    }
