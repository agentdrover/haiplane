"""Who wakes the steward (#1073, epic #994).

The steward is not a session and does not wait for work. The hub does: the
poller ticks every thirty seconds, sees a submission whose generation carries
no judgement yet, and ORDERS a run. The run is ephemeral by construction —
one order, one judgement, one generation — and dies. What lives permanently
is this loop, not the agent.

That direction is the security property, not an implementation detail. A
judge that can start itself decides WHEN it judges, and the packet it reads
(#1074/#1075) stops being tied to a moment somebody else chose. So ordering
is a hub-only verb: the steward principal has two operations (#1021), and
neither of them is this one.

Four guards stand between a submission and an order, and each of them fails
toward today's human route rather than toward a run:

``STEWARD_MODE``
    off (or any unrecognised value) closes the dispatcher entirely;
at-most-once
    the unique index on (task_id, generation, kind) makes a second order
    impossible rather than unlikely — two ticks racing on one generation is
    the ordinary case, and a duplicate costs a second paid run;
daily cap
    twenty runs per project per UTC day; hitting it is `daily_cap` in the
    feed and the human route, never "checked and clean";
deadline
    ``review:client`` is a human-owned slot with no deadline of its own, so a
    hung run would sit there looking ordered forever. The slot has one.

What this module does NOT do is start the cloud agent. Ordering a run and
executing it are different jobs with different failure modes, and the second
one belongs to F3 (#997) along with the shadow table and the canaries. The
order is the contract between them: this module writes it, F3 picks it up.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

from hub import config
from hub import repository as repo
from hub.db import fetchall

log = logging.getLogger(__name__)

KIND_VERDICT = "verdict"

RUN_OPEN = "open"
RUN_JUDGED = "judged"
RUN_TIMEOUT = "timeout"
RUN_SUPERSEDED = "superseded"

# Refusal codes. They are the vocabulary of the escalate reasons the contract
# already closes over (#1022), so a refusal here and an escalation there mean
# the same thing by name rather than by resemblance.
REFUSED_MODE_OFF = "steward_off"
REFUSED_DAILY_CAP = "daily_cap"
REFUSED_ALREADY_ORDERED = "already_ordered"
REFUSED_NO_GENERATION = "no_generation"

EVENT_ORDERED = "steward_run_ordered"
EVENT_REFUSED = "steward_run_refused"
EVENT_CLOSED = "steward_run_closed"

_MODES = {"off", "shadow", "act"}


def steward_mode() -> str:
    """The global switch, read strictly (#835's rule for typos).

    An unknown value is ``off``: a mistyped drop-in must never be the thing
    that switches a contour on.
    """
    mode = (config.STEWARD_MODE or "off").strip().lower()
    return mode if mode in _MODES else "off"


def dispatcher_enabled() -> bool:
    return steward_mode() != "off"


async def _refuse(
    db: aiosqlite.Connection, task_id: int, reason: str, detail: str
) -> None:
    """Say no in the feed. A silent refusal is indistinguishable from a bug."""
    await repo.insert_event(
        db,
        kind=EVENT_REFUSED,
        task_id=task_id,
        actor="hub",
        payload={"reason": reason, "detail": detail},
    )
    await db.commit()


async def runs_today(db: aiosqlite.Connection, project_id: int | None) -> int:
    """Orders placed for this project within the current UTC day."""
    rows = await fetchall(
        db,
        "SELECT COUNT(*) AS n FROM steward_runs "
        "WHERE project_id IS ? AND date(created_at) = date('now')",
        (project_id,),
    )
    return int(dict(rows[0]).get("n") or 0) if rows else 0


async def open_run(
    db: aiosqlite.Connection, task_id: int, generation: int, kind: str = KIND_VERDICT
) -> dict[str, Any] | None:
    """The open order for this generation, or None."""
    rows = await fetchall(
        db,
        "SELECT * FROM steward_runs "
        "WHERE task_id=? AND generation=? AND kind=? AND status=?",
        (task_id, generation, kind, RUN_OPEN),
    )
    return dict(rows[0]) if rows else None


async def order_run(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    kind: str = KIND_VERDICT,
) -> dict[str, Any] | None:
    """Place one order, or refuse with a named reason.

    Returns the order on success and None on every refusal — the caller has
    nothing to do either way, because a refusal is not a failure: it is the
    human route continuing to work exactly as it does today.
    """
    if not dispatcher_enabled():
        await _refuse(
            db,
            task_id,
            REFUSED_MODE_OFF,
            f"STEWARD_MODE={config.STEWARD_MODE!r} — контур закрыт",
        )
        return None
    if generation <= 0:
        await _refuse(
            db, task_id, REFUSED_NO_GENERATION, "у задачи нет закреплённой сдачи"
        )
        return None

    project = await repo.resolve_project_for_task(db, task_id)
    project_id = dict(project)["id"] if project is not None else None
    used = await runs_today(db, project_id)
    if used >= config.STEWARD_DAILY_CAP:
        await _refuse(
            db,
            task_id,
            REFUSED_DAILY_CAP,
            f"суточный потолок исчерпан: {used}/{config.STEWARD_DAILY_CAP} "
            "прогонов на проект за UTC-сутки — задача идёт человеческим маршрутом",
        )
        return None

    # The order and its uniqueness are one statement: a check-then-insert
    # would be exactly the race the index exists to lose.
    try:
        cursor = await db.execute(
            "INSERT INTO steward_runs "
            "(task_id, generation, kind, status, model, project_id, deadline_at) "
            "VALUES (?, ?, ?, ?, ?, ?, "
            "datetime('now', ?))",
            (
                task_id,
                generation,
                kind,
                RUN_OPEN,
                config.STEWARD_MODEL,
                project_id,
                f"+{config.STEWARD_RUN_DEADLINE_MIN} minutes",
            ),
        )
    except aiosqlite.IntegrityError:
        await _refuse(
            db,
            task_id,
            REFUSED_ALREADY_ORDERED,
            f"прогон на генерацию {generation} ({kind}) уже заказан",
        )
        return None

    run_id = cursor.lastrowid
    await repo.insert_event(
        db,
        kind=EVENT_ORDERED,
        task_id=task_id,
        actor="hub",
        payload={
            "run_id": run_id,
            "generation": generation,
            "kind": kind,
            "model": config.STEWARD_MODEL,
            "mode": steward_mode(),
        },
    )
    await db.commit()
    log.info("steward run ordered: task #%s gen %s kind %s", task_id, generation, kind)
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run_id,))
    return dict(rows[0]) if rows else None


async def close_run(
    db: aiosqlite.Connection, run: dict[str, Any], status: str, reason: str
) -> None:
    """Close a slot and say why, in the feed as well as in the row."""
    await db.execute(
        "UPDATE steward_runs SET status=?, closed_reason=?, "
        "closed_at=datetime('now') WHERE id=?",
        (status, reason, run["id"]),
    )
    await repo.insert_event(
        db,
        kind=EVENT_CLOSED,
        task_id=run["task_id"],
        actor="hub",
        payload={
            "run_id": run["id"],
            "generation": run["generation"],
            "status": status,
            "reason": reason,
        },
    )
    await db.commit()


def _policy_wants_steward(project_row: Any | None) -> bool:
    """Does the project's own gate policy ask for a steward verdict (#743)?

    Resolution failures refuse toward the human, like every other read of this
    policy: a project that cannot be resolved has not asked for anything.
    """
    if project_row is None:
        return False
    try:
        policy = json.loads(dict(project_row).get("gate_policy") or "{}")
    except ValueError:
        return False
    return isinstance(policy, dict) and policy.get("verdict") == "steward"


async def order_due_runs(db: aiosqlite.Connection) -> int:
    """Order a run for every submission that is waiting for one."""
    if not dispatcher_enabled():
        return 0
    ordered = 0
    rows = await fetchall(
        db,
        "SELECT id, submission_generation FROM tasks "
        "WHERE status='review' AND (review_job_id IS NULL OR review_job_id='') "
        "AND submission_generation > 0",
    )
    for row in rows:
        task = dict(row)
        task_id, generation = task["id"], task["submission_generation"] or 0
        project = await repo.resolve_project_for_task(db, task_id)
        if not _policy_wants_steward(project):
            continue
        if await open_run(db, task_id, generation) is not None:
            continue
        if await _judged(db, task_id, generation):
            continue
        if await order_run(db, task_id, generation) is not None:
            ordered += 1
    return ordered


async def _judged(db: aiosqlite.Connection, task_id: int, generation: int) -> bool:
    """Has any run for this generation already been closed as judged?"""
    rows = await fetchall(
        db,
        "SELECT 1 FROM steward_runs WHERE task_id=? AND generation=? "
        "AND status != ? LIMIT 1",
        (task_id, generation, RUN_OPEN),
    )
    return bool(rows)


async def close_finished_runs(db: aiosqlite.Connection) -> int:
    """Close slots that are over: overdue, or overtaken by a human.

    Runs even when the dispatcher is switched off. Turning the contour off
    must not leave slots hanging open — the switch stops new orders, it does
    not abandon the ones already placed.
    """
    closed = 0
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE status=?", (RUN_OPEN,))
    for row in rows:
        run = dict(row)
        task_row = await repo.get_task(db, run["task_id"])
        task = dict(task_row) if task_row is not None else {}
        # A human verdict on this very generation ends the run: the judgement
        # it was ordered for is no longer anybody's to make (#1022 gives such
        # a late judgement a 409, and this closes the slot behind it).
        verdict_generation = task.get("review_verdict_generation")
        if task and verdict_generation == run["generation"]:
            await close_run(
                db,
                run,
                RUN_SUPERSEDED,
                "человеческий вердикт на эту генерацию — судить больше нечего",
            )
            closed += 1
            continue
        overdue = await fetchall(
            db,
            "SELECT 1 FROM steward_runs WHERE id=? AND deadline_at <= datetime('now')",
            (run["id"],),
        )
        if overdue:
            await close_run(
                db,
                run,
                RUN_TIMEOUT,
                "прогон не вернул суждение до дедлайна слота",
            )
            closed += 1
    return closed


async def sweep_steward_runs(db: aiosqlite.Connection) -> None:
    """One poller pass: close what is over, then order what is due."""
    await close_finished_runs(db)
    await order_due_runs(db)
