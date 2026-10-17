"""A production defect does not close with nothing after it (#919, feature #908).

A task with ``found_in='prod'`` reaches ``completed`` only with one of three
prevention outputs stored in ``tasks.defect_prevention``:

* ``regression_test`` — ``ref`` names the test that now fails on this defect;
* ``rule`` — ``ref`` names a category already recorded in ``category_checks``
  (#878). The catalogue is where a rule lives: it is what the review brief and
  the repeat report read (#920), so a rule outside it would be a rule nobody
  applies. Record the category first, then name it here;
* ``accepted_risk`` — a full answer, not a loophole, and therefore owes both a
  ``reason`` and a ``revisit`` condition or date. Without either it is refused:
  a risk accepted "forever, because" is a close without output by another name.

Every door into ``completed`` meets the gate, each in the way its caller can
hear it:

* agent doors — the done report and the pair submission — are REFUSED (422),
  naming the three options. After a pair submission the poller delivers on its
  own, so that is the last moment the agent is asked;
* automatic doors — ``transition_after_agent_done``, the poller's delivery
  sweep, the parent rollup — cannot refuse anybody, so they HOLD: the task goes
  to ``needs_decision`` (the rollup skips) instead of completing silently;
* human doors — decide accept, force-complete — stay an emergency exit. They
  are not blocked, but a close without an output is written as its own event
  (``prod_defect_closed_without_prevention``) and alert, so it is counted and
  visible rather than indistinguishable from a close that had one.

The kind of cause (#1791, ``tasks.cause_kind``) rides the same gate as a second,
independent reason. A prod defect closes only with a named kind of cause —
``unknown`` is an answer ("looked, not established"), an empty field is not —
and it must not be read off the prevention the caller happened to send: a
valid prevention does not make the kind present. Both reasons are judged on the
row as it stands in the transaction, apart from the task dict a caller carries.
``unknown`` does not stand in for the prevention either: the gate wants both.
A human close without a kind is its own event
(``prod_defect_closed_without_cause_kind``), counted apart from the missing
prevention.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import repository as repo
from hub.db import fetchall
from hub.models import FINAL_STATUSES, DefectPrevention
from hub.services import watch_notices
from hub.services.cause_kind import CAUSE_KINDS

_FINAL = frozenset(s.value for s in FINAL_STATUSES)

CLOSED_WITHOUT_PREVENTION = "prod_defect_closed_without_prevention"
CLOSED_WITHOUT_CAUSE_KIND = "prod_defect_closed_without_cause_kind"

PREVENTION_OPTIONS = (
    "regression_test — ref: локатор регрессионного теста (tests/x.py::test_y)",
    "rule — ref: категория из category_checks (#878); сначала запишите её "
    "через POST /api/metrics/category-checks",
    "accepted_risk — reason: почему риск принят, и revisit: условие или срок "
    "пересмотра; без любого из двух не засчитывается",
)


def _options() -> str:
    return "; ".join(f"({i}) {o}" for i, o in enumerate(PREVENTION_OPTIONS, 1))


#: Named per door: on a pair task the done report bypasses the submission.
DONE_DOOR = (
    "Передайте его в prevention отчёта о готовности: hub_report_done(prevention), "
    "POST /api/tasks/{id}/updates или oc-hub update --kind done --prevention."
)
SUBMIT_DOOR = (
    "Передайте его в prevention сдачи: hub_submit_for_review(prevention), "
    "POST /api/tasks/{id}/submit-review или oc-hub submit-review --prevention."
)


#: How the missing kind is set, per door; the same call for every door.
CAUSE_KIND_HINT = (
    "Задайте его: hub_refine_task(cause_kind), POST /api/tasks/{id}/refine или "
    "oc-hub refine --cause-kind; regression требует caused_by_task_id, "
    "unknown — «причина не установлена»."
)


def cause_kind_gap(task: dict[str, Any]) -> str:
    """Why the kind of cause is missing, or "" when the gate does not apply."""
    if (task.get("found_in") or "") != "prod":
        return ""
    if (task.get("cause_kind") or "").strip():
        return ""
    return (
        "cause_kind_required: у прод-дефекта (found_in='prod') не назван вид "
        f"причины — один из: {CAUSE_KINDS}. {CAUSE_KIND_HINT}"
    )


def completion_gap(task: dict[str, Any], door: str = "") -> str:
    """Every reason a prod defect may not complete yet, or "".

    The two reasons are independent: neither is read off the other.
    """
    return " ".join(g for g in (prevention_gap(task, door), cause_kind_gap(task)) if g)


def prevention_gap(task: dict[str, Any], door: str = "") -> str:
    """Why this task may not complete yet, or "" when the gate does not apply."""
    if (task.get("found_in") or "") != "prod":
        return ""
    if (task.get("defect_prevention") or "").strip():
        return ""
    return (
        "prevention_required: прод-дефект (found_in='prod') закрывается только "
        f"с выводом — одним из трёх: {_options()}. {door}"
    ).rstrip()


def refuse_without_prevention(task: dict[str, Any], door: str = DONE_DOOR) -> None:
    """Agent doors: refuse, naming the three options and this door's field."""
    gap = prevention_gap(task, door)
    if gap:
        raise HTTPException(422, gap)


async def _fresh(db: aiosqlite.Connection, task: dict[str, Any]) -> dict[str, Any]:
    """The row as it stands now: the caller's dict may predate a refine (#1791)."""
    row = await repo.get_task(db, int(task["id"]))
    return dict(row) if row is not None else task


def refuse_without_cause_kind(task: dict[str, Any], door: str = "") -> None:
    """Agent doors: refuse a prod defect with no kind of cause, whatever came
    with the report — the prevention sent along does not name the cause.

    When the prevention is missing too, one refusal names both.
    """
    gap = completion_gap(task, door) if door else cause_kind_gap(task)
    if gap:
        raise HTTPException(422, gap)


def _require(value: str, message: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise HTTPException(422, f"prevention_invalid: {message}. {_options()}")
    return stripped


async def validate_prevention(
    db: aiosqlite.Connection, prevention: DefectPrevention
) -> dict[str, str]:
    """The stored record, or 422 when this output does not count."""
    record: dict[str, str] = {"kind": str(prevention.kind)}
    if prevention.kind == "accepted_risk":
        record["reason"] = _require(
            prevention.reason, "accepted_risk без обоснования (reason)"
        )
        record["revisit"] = _require(
            prevention.revisit,
            "accepted_risk без условия или срока пересмотра (revisit)",
        )
        return record
    record["ref"] = _require(prevention.ref, f"{prevention.kind} без ref")
    if prevention.kind == "rule":
        rows = await fetchall(
            db,
            "SELECT check_ref FROM category_checks WHERE category=?",
            (record["ref"],),
        )
        if not rows:
            raise HTTPException(
                422,
                f"prevention_invalid: категории {record['ref']!r} нет в "
                "category_checks (#878) — правило живёт в каталоге; запишите "
                "его через POST /api/metrics/category-checks и повторите",
            )
        record["check_ref"] = rows[0][0]
    return record


async def record_done_prevention(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    *,
    kind: str,
    prevention: DefectPrevention | None,
    actor: str,
) -> None:
    """Done-report door: store the output sent with it, or refuse without one.

    Runs inside the done-report savepoint, so a refusal rolls the report row
    back with it: a feed line saying "done" next to a refused close is the
    false record #364 removed.
    """
    if kind == "done":
        # #1791: the cause is judged on every done report, with or without a
        # prevention in it; before anything is stored, so a refusal leaves
        # nothing behind but the savepoint rollback.
        fresh = await _fresh(db, task)
        refuse_without_cause_kind(fresh, DONE_DOOR if prevention is None else "")
    if prevention is None:
        if kind == "done":
            refuse_without_prevention(task)
        return
    record = await validate_prevention(db, prevention)
    await store_prevention(db, task["id"], record, actor=actor)


async def store_prevention(
    db: aiosqlite.Connection, task_id: int, record: dict[str, str], *, actor: str
) -> None:
    """Write a validated output next to the defect, with its event. No commit."""
    stored = dict(record)
    stored["recorded_by"] = actor
    stored["recorded_at"] = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    await repo.update_task(
        db, task_id, defect_prevention=json.dumps(stored, ensure_ascii=False)
    )
    await repo.insert_event(
        db,
        kind="defect_prevention_recorded",
        task_id=task_id,
        actor=actor,
        payload=stored,
    )


async def check_submission(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    prevention: DefectPrevention | None,
) -> dict[str, str] | None:
    """Pair-submission door: the validated output to store, or a refusal.

    Validates only — the write waits for the transition, past every gate
    that could still refuse, like the finding outcomes (#911): an output
    recorded for a submission that never happened would read as answered.
    """
    # #1791: the kind of cause is judged whatever prevention came with the
    # submission, on the row as it stands in this transaction.
    fresh = await _fresh(db, task)
    refuse_without_cause_kind(fresh, SUBMIT_DOOR if prevention is None else "")
    if prevention is None:
        refuse_without_prevention(task, SUBMIT_DOOR)
        return None
    return await validate_prevention(db, prevention)


ROLLUP_HELD_NOTE = (
    "Родитель — прод-дефект (found_in='prod') без вывода или без вида причины: "
    "роллап не закрыл его, хотя дети завершены. Закрытие ждёт собственного "
    "отчёта с выводом (prevention в hub_report_done или hub_submit_for_review) — "
    "тест, правило из category_checks или принятый риск с причиной и сроком "
    "пересмотра — и названного вида причины (cause_kind в refine)."
)


async def note_rollup_held(db: aiosqlite.Connection, parent_id: int) -> None:
    """Rollup skipped for a prod defect: said once, not on every child (#919)."""
    rows = await fetchall(
        db,
        "SELECT 1 FROM task_updates WHERE task_id=? AND agent='hub' AND content=?",
        (parent_id, ROLLUP_HELD_NOTE),
    )
    if rows:
        return
    await repo.add_task_update(db, parent_id, "hub", "status", ROLLUP_HELD_NOTE)


async def hold_completion(
    db: aiosqlite.Connection, task_id: int, *, via: str, actor: str
) -> bool:
    """Automatic doors: True when the task was held in needs_decision instead.

    Reads the row afresh — the caller's dict may predate an output recorded
    earlier in the same transaction. No commit: the caller owns it.
    """
    row = await repo.get_task(db, task_id)
    if row is None or dict(row)["status"] in _FINAL:
        # Only the way INTO completed is gated. A task already closed — by a
        # human through the emergency exit, say — is not reopened by a sweep
        # that happens to pass over it.
        return False
    gap = completion_gap(dict(row))
    if not gap:
        return False
    reason = "prevention_missing" if prevention_gap(dict(row)) else "cause_kind_missing"
    # Conditional, in one UPDATE: a close that landed between the read above
    # and this write leaves the row as it is — "already closed", not an error.
    if not await repo.transition_status_if(
        db, task_id, expected_from=dict(row)["status"], new_status="needs_decision"
    ):
        return False
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"Прод-дефект не завершён ({via}): {gap} Решение за человеком "
        "(REST POST /api/tasks/<id>/decide): rework вернёт задачу в работу, чтобы агент "
        "пересдал отчёт с выводом и названным видом причины; accept закроет её "
        "без недостающего, и это будет записано отдельным событием.",
    )
    await watch_notices.emit_needs_decision(
        db,
        task_id=task_id,
        actor=actor,
        payload={"reason": reason, "via": via},
    )
    return True


async def note_close_without_prevention(
    db: aiosqlite.Connection, task: dict[str, Any], *, via: str, actor: str
) -> None:
    """Human doors: the close goes through, and what is missing is named.

    The missing prevention and the missing kind of cause (#1791) are two
    bypasses: each is its own alert and event, so the count of closes without
    a kind is not mixed into the count of closes without an output.
    """
    if prevention_gap(task):
        await repo.add_task_update(
            db,
            task["id"],
            "hub",
            "alert",
            f"Прод-дефект закрыт человеком ({via}) без вывода: ни регрессионного "
            "теста, ни правила, ни принятого риска. Закрытие засчитано как "
            "аварийный выход и видно в отчёте отдельно.",
        )
        await repo.insert_event(
            db,
            kind=CLOSED_WITHOUT_PREVENTION,
            task_id=task["id"],
            actor=actor,
            payload={"via": via},
        )
    if cause_kind_gap(task):
        await repo.add_task_update(
            db,
            task["id"],
            "hub",
            "alert",
            f"Прод-дефект закрыт человеком ({via}) без вида причины (cause_kind). "
            "Закрытие засчитано как аварийный выход и видно отдельно от "
            "закрытий без вывода.",
        )
        await repo.insert_event(
            db,
            kind=CLOSED_WITHOUT_CAUSE_KIND,
            task_id=task["id"],
            actor=actor,
            payload={"via": via},
        )
