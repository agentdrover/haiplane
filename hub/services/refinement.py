"""Refinement service — thin wrappers around the repository for the
structured task form (Epic #32).

The handlers in ``hub.app`` stay thin: they validate the request body,
delegate to one of these helpers, and serialize the result. The same
helpers are reusable by the CLI (#42) and MCP server (#43) so that
business rules (atomic AC replace, single commit per request, error
translation) live in exactly one place.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from hub import config
from hub import repository as repo
from hub.models import (
    MAX_ACCEPTANCE_CRITERIA,
    MAX_RISKS,
    AcceptanceCriterion,
    BulkRefine,
    BulkRefineResult,
    ReadinessReport,
    ReadinessTreeNode,
    ReadinessTreeReport,
    TaskRefine,
    TaskRefineOutcome,
    TaskRisk,
)
from hub.services.readiness import parse_risks_from_row
from hub.services.recommendations import calculate_readiness_with_recommendations
from hub.services.test_locator import validate_test_locators


# Statuses where the Definition of Ready gate still applies. DoR is a
# pre-execution check, so once a task has started or finished its readiness is
# no longer the relevant question — those nodes are excluded from subtree rollups.
_DOR_RELEVANT_STATUSES = frozenset({"draft", "open", "needs_info"})


class TaskNotFoundError(LookupError):
    """Raised when an operation targets a non-existent task."""


class DuplicateAcceptanceCriterionError(ValueError):
    """Raised when ac_id collides with an existing one for the same task."""


class LimitExceededError(ValueError):
    """Raised when an insert would push a task past a structured-form cap.

    A ValueError so the API layer maps it to 422 like the other malformed-input
    errors: asking for a 51st item is a bad request, not a server fault (#366).
    """


class ProjectBindError(ValueError):
    """Raised when a project binding is invalid (#338)."""


def get_write_lock(db: aiosqlite.Connection) -> asyncio.Lock:
    """Return a per-connection write lock, created lazily on the running loop.

    The Hub uses a single shared aiosqlite connection across requests. Two
    concurrent mutations would otherwise interleave their SAVEPOINT/commit
    pairs on that one connection, so one coroutine's ``commit()`` flushes
    another's half-written rows — surfacing as sporadic HTTP 500s where the
    write "sometimes still landed" (feedback #3). Serializing the critical
    section makes list-append writes (e.g. parallel add_acceptance_criterion)
    atomic and predictable. The lock is stored on the connection (not a module
    global) so it always binds to the event loop that owns the connection,
    which keeps per-test loops happy.

    Serialization boundary (important): the lock is acquired by ``_atomic``
    (refine / AC add / upsert / replace / bulk refine), ``create_subtasks_bulk``,
    and the lifecycle completion paths (``add_update`` done-flow,
    ``force_complete_task``) — i.e. the writers that contend over the same task
    rows. Short single-statement lifecycle transitions and ``log_activity``
    calls elsewhere are NOT lock-guarded; under high cross-path concurrency a
    stray commit could still flush an open SAVEPOINT. Fully serializing every
    commit on the shared connection (or moving to per-request connections /
    ``BEGIN IMMEDIATE``) is tracked as separate hardening work.
    """
    lock: asyncio.Lock | None = getattr(db, "_oc_write_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        db._oc_write_lock = lock  # type: ignore[attr-defined]
    return lock


@asynccontextmanager
async def _atomic(db: aiosqlite.Connection, name: str):
    """SAVEPOINT-scoped atomic block, serialized by the per-connection lock.

    Without an explicit SAVEPOINT, a partial failure inside a multi-step
    mutation (e.g. ``update_task_structured`` then
    ``replace_acceptance_criteria``) leaves dirty rows in the implicit
    transaction; the next handler's ``commit()`` then promotes them. The
    SAVEPOINT gives per-operation atomicity; the write lock prevents
    concurrent mutations from interleaving on the shared connection.
    """
    sp = name.replace("-", "_").replace(" ", "_")
    async with get_write_lock(db):
        await db.execute(f"SAVEPOINT {sp}")
        try:
            yield
        except BaseException:
            await db.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            await db.execute(f"RELEASE SAVEPOINT {sp}")
            raise
        else:
            await db.execute(f"RELEASE SAVEPOINT {sp}")
            await db.commit()


def row_to_ac(row: aiosqlite.Row) -> AcceptanceCriterion:
    """Map a row from ``acceptance_criteria`` to the Pydantic model.

    Inverse of ``hub.db.ac_to_row_kwargs``: ``ac_id``/``when_clause``/
    ``then_clause`` columns are unpacked back to the model's ``id``/
    ``when``/``then`` field names.
    """
    return AcceptanceCriterion(
        id=row["ac_id"],
        given=row["given"],
        when=row["when_clause"],
        then=row["then_clause"],
        verifiable_by=row["verifiable_by"],
        test_ref=row["test_ref"],
    )


async def _ensure_task_exists(db: aiosqlite.Connection, task_id: int) -> None:
    if await repo.get_task(db, task_id) is None:
        raise TaskNotFoundError(f"task {task_id} not found")


# ---------------------------------------------------------------------------
# Refine — PATCH-style update of structured fields (and optionally ACs)
# ---------------------------------------------------------------------------


async def refine_task(
    db: aiosqlite.Connection,
    task_id: int,
    payload: TaskRefine,
) -> dict[str, Any]:
    """Apply a TaskRefine PATCH and (optionally) replace ACs atomically.

    Behavior:
    - structured fields explicitly set on ``payload`` are written via
      ``repo.update_task_structured``;
    - if ``payload.acceptance_criteria`` is not None (even empty list),
      the AC table is fully replaced for this task — passing ``[]``
      clears the criteria deliberately;
    - everything is committed in a single transaction so a partial
      refine cannot land.

    Returns a small audit dict with ``updated_columns`` and ``ac_count``
    so callers / tests can assert without re-reading the row.
    """
    await _ensure_task_exists(db, task_id)
    old_row = await repo.get_task(db, task_id)

    project_id: int | None = None
    if payload.project is not None:
        # Epic-to-project binding (#338): projects live on epics only.
        if old_row is None or old_row["task_type"] != "epic":
            raise ProjectBindError(
                "project can only be set on an epic; descendants inherit it"
            )
        project_row = await repo.get_project_by_slug(db, payload.project)
        if project_row is None:
            raise ProjectBindError(f"unknown project slug: {payload.project!r}")
        if project_row["status"] != "active":
            raise ProjectBindError(f"project {payload.project!r} is pending activation")
        project_id = project_row["id"]

    async with _atomic(db, "refine_task"):
        updated_columns, ac_count = await _apply_refine_writes(
            db, task_id, payload, old_row
        )
        if project_id is not None:
            await repo.update_task(db, task_id, project_id=project_id)
            updated_columns["project_id"] = project_id
        await recalc_readiness_inline(db, task_id)

    return {"updated_columns": updated_columns, "ac_count": ac_count}


async def _apply_refine_writes(
    db: aiosqlite.Connection,
    task_id: int,
    payload: TaskRefine,
    old_row: aiosqlite.Row | None,
) -> tuple[dict[str, Any], int | None]:
    """Write a refine PATCH WITHOUT opening its own transaction.

    The caller is responsible for the surrounding SAVEPOINT/commit so this
    helper is reusable by both single (`refine_task`) and bulk
    (`refine_tasks_bulk`) flows. Returns ``(updated_columns, ac_count)``.
    """
    # Validate the input BEFORE writing anything (#573). The surrounding
    # SAVEPOINT rolls a late rejection back either way, so this changes no
    # outcome — it just stops the code from writing rows it already knows it
    # will discard, and keeps the order readable: check input, then persist.
    if payload.acceptance_criteria is not None:
        # Verifiable SDD (#505): reject verifiable_by=test AC without a
        # resolvable pytest locator when the project opts in. Gated by config
        # so the default-off policy leaves existing refine flows untouched.
        validate_test_locators(
            payload.acceptance_criteria,
            enforce=config.SDD_AC_LOCATOR == "require",
        )

    updated_columns = await repo.update_task_structured(db, task_id, payload)

    if (
        old_row
        and payload.title is not None
        and "title" in updated_columns
        and old_row["title"] != updated_columns["title"]
    ):
        await repo.add_task_update(
            db,
            task_id,
            "",
            "status",
            f"Title refined: {old_row['title']!r} → {updated_columns['title']!r}",
        )

    ac_count: int | None = None
    if payload.acceptance_criteria is not None:
        try:
            ac_count = await repo.replace_acceptance_criteria(
                db, task_id, payload.acceptance_criteria
            )
        except ValueError as exc:
            # SAVEPOINT rolls back the structured-fields write too.
            raise DuplicateAcceptanceCriterionError(str(exc)) from exc

    return updated_columns, ac_count


async def refine_tasks_bulk(
    db: aiosqlite.Connection,
    payload: BulkRefine,
) -> BulkRefineResult:
    """Apply a TaskRefine PATCH to many tasks in ONE transaction.

    Either every item lands or none does: any error (missing task, duplicate
    AC id) rolls back the whole batch via the single SAVEPOINT. This collapses
    the previous "~28 refine calls" into one request.
    """
    for item in payload.items:
        await _ensure_task_exists(db, item.task_id)

    outcomes: list[TaskRefineOutcome] = []
    async with _atomic(db, "refine_bulk"):
        for item in payload.items:
            old_row = await repo.get_task(db, item.task_id)
            refine = TaskRefine.model_validate(
                item.model_dump(exclude={"task_id"}, exclude_unset=True)
            )
            _updated_columns, ac_count = await _apply_refine_writes(
                db, item.task_id, refine, old_row
            )
            # Unify with hub_refine_task: report the fields actually SENT in the
            # request (PATCH keys), not a post-write column diff. model_fields_set
            # reflects the keys provided (refine was validated with exclude_unset),
            # and includes acceptance_criteria / risks when present.
            fields_set = sorted(refine.model_fields_set)
            risks_count = len(refine.risks) if refine.risks is not None else None
            outcomes.append(
                TaskRefineOutcome(
                    task_id=item.task_id,
                    fields_set=fields_set,
                    acceptance_criteria_count=ac_count,
                    risks_count=risks_count,
                )
            )

    # Readiness is computed after commit so each report reflects the final row,
    # and persisted (#250) so lists/boards can rely on the stored values.
    async with get_write_lock(db):
        for outcome in outcomes:
            report = await calculate_readiness_with_recommendations(db, outcome.task_id)
            await _persist_readiness_fields(db, outcome.task_id, report)
            outcome.readiness_score = report.score
            outcome.dor_passed = report.dor_passed
        await db.commit()

    return BulkRefineResult(results=outcomes)


async def _persist_readiness_fields(
    db: aiosqlite.Connection,
    task_id: int,
    report: ReadinessReport,
) -> None:
    """Write score/dor_passed/ready_at onto the task row (#250). No locking —
    the caller owns the transaction (an ``_atomic`` block or an explicit
    write-lock + commit)."""
    row = await repo.get_task(db, task_id)
    if row is None:
        return
    fields: dict[str, Any] = {
        "readiness_score": report.score,
        "dor_passed": int(report.dor_passed),
    }
    if report.dor_passed:
        if not row["ready_at"]:
            fields["ready_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
    else:
        # DoR regressed (e.g. a required AC was deleted): the persisted
        # readiness must not go stale.
        fields["ready_at"] = None
    await repo.update_task(db, task_id, **fields)


async def recalc_readiness_inline(
    db: aiosqlite.Connection,
    task_id: int,
) -> ReadinessReport:
    """Recompute readiness and persist it — call INSIDE an ``_atomic`` block
    (it must not re-acquire the write lock)."""
    report = await calculate_readiness_with_recommendations(db, task_id)
    await _persist_readiness_fields(db, task_id, report)
    return report


def _persisted_readiness_stale(row: aiosqlite.Row, report: ReadinessReport) -> bool:
    stored_passed = row["dor_passed"]
    return (
        row["readiness_score"] != report.score
        or stored_passed is None
        or bool(stored_passed) != report.dor_passed
        or (report.dor_passed and not row["ready_at"])
        or (not report.dor_passed and bool(row["ready_at"]))
    )


async def add_risk(
    db: aiosqlite.Connection,
    task_id: int,
    risk: TaskRisk,
) -> None:
    """Append one risk atomically without replacing the existing list."""
    await _ensure_task_exists(db, task_id)
    async with _atomic(db, "add_risk"):
        # The 50-item cap lived only in the full-replace validator, so
        # repeated single adds walked straight past it (#366). One limit,
        # every path.
        row = await repo.get_task(db, task_id)
        existing = parse_risks_from_row(row["risks"]) if row is not None else []
        if len(existing) >= MAX_RISKS:
            raise LimitExceededError(
                f"too many risks: {len(existing)} already at the limit of {MAX_RISKS}"
            )
        updated = await repo.append_task_risk(db, task_id, risk)
        if not updated:
            raise TaskNotFoundError(f"task {task_id} not found")
        await recalc_readiness_inline(db, task_id)


# ---------------------------------------------------------------------------
# Acceptance criteria — CRUD
# ---------------------------------------------------------------------------


async def list_acceptance_criteria(
    db: aiosqlite.Connection,
    task_id: int,
) -> list[AcceptanceCriterion]:
    await _ensure_task_exists(db, task_id)
    rows = await repo.list_acceptance_criteria(db, task_id)
    return [row_to_ac(r) for r in rows]


async def _guard_ac_limit(db: aiosqlite.Connection, task_id: int) -> None:
    """Refuse an insert that would push a task past MAX_ACCEPTANCE_CRITERIA.

    The cap was enforced in the bulk-replace path only, so single adds could
    accumulate without bound (#366).
    """
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS n FROM acceptance_criteria WHERE task_id=?", (task_id,)
    )
    count = dict(rows[0])["n"] if rows else 0
    if count >= MAX_ACCEPTANCE_CRITERIA:
        raise LimitExceededError(
            f"too many acceptance criteria: {count} already at the limit "
            f"of {MAX_ACCEPTANCE_CRITERIA}"
        )


async def add_acceptance_criterion(
    db: aiosqlite.Connection,
    task_id: int,
    ac: AcceptanceCriterion,
) -> tuple[AcceptanceCriterion, bool]:
    """Insert one AC idempotently by ``(task_id, ac_id)``.

    Returns ``(ac, created)`` where ``created`` is False when the same
    ``ac_id`` already exists (deterministic no-op, no 409).
    """
    await _ensure_task_exists(db, task_id)
    async with _atomic(db, "add_ac"):
        rows = await db.execute_fetchall(
            "SELECT * FROM acceptance_criteria WHERE task_id=? AND ac_id=?",
            (task_id, ac.id),
        )
        if rows:
            return row_to_ac(rows[0]), False
        # Counted only when this call would actually add a row: resending an
        # existing ac_id returned above without touching the count (#366).
        await _guard_ac_limit(db, task_id)
        try:
            await repo.add_acceptance_criterion(db, task_id, ac)
        except aiosqlite.IntegrityError as exc:
            rows = await db.execute_fetchall(
                "SELECT * FROM acceptance_criteria WHERE task_id=? AND ac_id=?",
                (task_id, ac.id),
            )
            if rows:
                return row_to_ac(rows[0]), False
            raise DuplicateAcceptanceCriterionError(
                f"acceptance criterion {ac.id!r} already exists for task {task_id}"
            ) from exc
        await recalc_readiness_inline(db, task_id)
    return ac, True


async def upsert_acceptance_criterion(
    db: aiosqlite.Connection,
    task_id: int,
    ac: AcceptanceCriterion,
) -> tuple[AcceptanceCriterion, bool]:
    """Insert or update an AC by ``ac_id`` (idempotent, no 409 on resend).

    Returns ``(ac, created)`` where ``created`` is True for a fresh insert
    and False when an existing criterion was overwritten.
    """
    await _ensure_task_exists(db, task_id)
    async with _atomic(db, "upsert_ac"):
        # Overwriting an existing criterion must keep working at the limit —
        # it does not add a row. Checking unconditionally here would make a
        # task with 50 criteria impossible to edit (#366).
        existing = await db.execute_fetchall(
            "SELECT 1 FROM acceptance_criteria WHERE task_id=? AND ac_id=?",
            (task_id, ac.id),
        )
        if not existing:
            await _guard_ac_limit(db, task_id)
        created = await repo.upsert_acceptance_criterion(db, task_id, ac)
        await recalc_readiness_inline(db, task_id)
    return ac, created


async def replace_acceptance_criteria(
    db: aiosqlite.Connection,
    task_id: int,
    items: list[AcceptanceCriterion],
) -> list[AcceptanceCriterion]:
    await _ensure_task_exists(db, task_id)
    async with _atomic(db, "replace_ac"):
        try:
            await repo.replace_acceptance_criteria(db, task_id, items)
        except ValueError as exc:
            raise DuplicateAcceptanceCriterionError(str(exc)) from exc
        await recalc_readiness_inline(db, task_id)
    return items


async def delete_acceptance_criterion(
    db: aiosqlite.Connection,
    task_id: int,
    ac_id: str,
) -> bool:
    """Delete one AC. Always commit (even on no-op) and rollback on
    any error so we never leave foreign in-flight state behind (review I8).
    """
    await _ensure_task_exists(db, task_id)
    async with _atomic(db, "delete_ac"):
        removed = await repo.delete_acceptance_criterion(db, task_id, ac_id)
        await recalc_readiness_inline(db, task_id)
    return removed


# ---------------------------------------------------------------------------
# Readiness — convenience wrapper around the recommendations service
# ---------------------------------------------------------------------------


async def get_readiness(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    explain: bool = False,
) -> ReadinessReport:
    await _ensure_task_exists(db, task_id)
    report = await calculate_readiness_with_recommendations(
        db, task_id, explain=explain
    )
    # Lazy repair (#250): reading readiness heals stale persisted values for
    # tasks refined before persistence existed.
    row = await repo.get_task(db, task_id)
    if row is not None and _persisted_readiness_stale(row, report):
        async with get_write_lock(db):
            await _persist_readiness_fields(db, task_id, report)
            await db.commit()
    return report


async def readiness_tree(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    include_root: bool = False,
) -> ReadinessTreeReport:
    """DoR rollup for a root task and its descendants in one pass.

    For each node we reuse the same calculator as ``/readiness`` so the
    subtree report can never drift from the per-task view. ``blocking_reasons``
    surfaces the actionable "why" (blocking recommendation messages) so a
    caller sees what to fix without a second round-trip.

    Only DoR-relevant statuses are scored (see ``_DOR_RELEVANT_STATUSES``):
    DoR is a pre-execution gate, so already-started (running/claimed/…) and
    terminal (completed/failed/rejected) descendants are skipped — otherwise a
    finished task that no longer satisfies presence-DoR would inflate
    ``not_ready`` and make a done backlog look unready.
    """
    await _ensure_task_exists(db, task_id)
    ids = await repo.collect_subtree_ids(db, task_id)
    if not include_root:
        ids = [tid for tid in ids if tid != task_id]

    nodes: list[ReadinessTreeNode] = []
    ready = 0
    for tid in ids:
        row = await repo.get_task(db, tid)
        if row is None:
            continue
        if row["status"] not in _DOR_RELEVANT_STATUSES:
            continue
        report = await calculate_readiness_with_recommendations(db, tid)
        blocking = [
            rec.message for rec in report.recommendations if rec.severity == "blocking"
        ]
        nodes.append(
            ReadinessTreeNode(
                id=tid,
                title=row["title"],
                task_type=row["task_type"],
                status=row["status"],
                score=report.score,
                dor_passed=report.dor_passed,
                missing_required=report.missing_required,
                blocking_reasons=blocking,
            )
        )
        if report.dor_passed:
            ready += 1

    return ReadinessTreeReport(
        root_id=task_id,
        total=len(nodes),
        ready=ready,
        not_ready=len(nodes) - ready,
        nodes=nodes,
    )


__all__ = [
    "DuplicateAcceptanceCriterionError",
    "TaskNotFoundError",
    "add_acceptance_criterion",
    "delete_acceptance_criterion",
    "get_readiness",
    "list_acceptance_criteria",
    "refine_task",
    "replace_acceptance_criteria",
    "row_to_ac",
]
