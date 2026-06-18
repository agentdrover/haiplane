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
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.models import (
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
from hub.services.recommendations import calculate_readiness_with_recommendations


class TaskNotFoundError(LookupError):
    """Raised when an operation targets a non-existent task."""


class DuplicateAcceptanceCriterionError(ValueError):
    """Raised when ac_id collides with an existing one for the same task."""


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

    async with _atomic(db, "refine_task"):
        updated_columns, ac_count = await _apply_refine_writes(
            db, task_id, payload, old_row
        )

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
            updated_columns, ac_count = await _apply_refine_writes(
                db, item.task_id, refine, old_row
            )
            fields_set = list(updated_columns.keys())
            if refine.acceptance_criteria is not None:
                fields_set.append("acceptance_criteria")
            fields_set = sorted(set(fields_set))
            risks_count = len(refine.risks) if refine.risks is not None else None
            outcomes.append(
                TaskRefineOutcome(
                    task_id=item.task_id,
                    fields_set=fields_set,
                    acceptance_criteria_count=ac_count,
                    risks_count=risks_count,
                )
            )

    # Readiness is computed after commit so each report reflects the final row.
    for outcome in outcomes:
        report = await calculate_readiness_with_recommendations(db, outcome.task_id)
        outcome.readiness_score = report.score
        outcome.dor_passed = report.dor_passed

    return BulkRefineResult(results=outcomes)


async def add_risk(
    db: aiosqlite.Connection,
    task_id: int,
    risk: TaskRisk,
) -> None:
    """Append one risk atomically without replacing the existing list."""
    await _ensure_task_exists(db, task_id)
    async with _atomic(db, "add_risk"):
        updated = await repo.append_task_risk(db, task_id, risk)
        if not updated:
            raise TaskNotFoundError(f"task {task_id} not found")


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


async def add_acceptance_criterion(
    db: aiosqlite.Connection,
    task_id: int,
    ac: AcceptanceCriterion,
) -> AcceptanceCriterion:
    """Insert one AC, raising ``DuplicateAcceptanceCriterionError`` on
    a unique-constraint violation so the API can map it to HTTP 409.
    """
    await _ensure_task_exists(db, task_id)
    async with _atomic(db, "add_ac"):
        try:
            await repo.add_acceptance_criterion(db, task_id, ac)
        except aiosqlite.IntegrityError as exc:
            raise DuplicateAcceptanceCriterionError(
                f"acceptance criterion {ac.id!r} already exists for task {task_id}"
            ) from exc
    return ac


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
        created = await repo.upsert_acceptance_criterion(db, task_id, ac)
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
    return await calculate_readiness_with_recommendations(db, task_id, explain=explain)


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
