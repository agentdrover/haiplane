"""Use the existing approval gate without creating per-task human decisions."""

from dataclasses import dataclass
import aiosqlite
from fastapi import HTTPException

from hub import repository as repo
from hub.epic_plan_view import PlanSnapshot
from hub.models import ReadinessReport, TaskApprove
from hub.services.dor_snapshot import DorSnapshot, load_dor_snapshot
from hub.services.recommendations import calculate_readiness_with_recommendations


@dataclass(frozen=True)
class PreparedDraft:
    snapshot: DorSnapshot
    readiness: ReadinessReport


async def prepare_drafts(
    db: aiosqlite.Connection,
    snapshot: PlanSnapshot,
) -> dict[int, PreparedDraft]:
    drafts = {}
    for node in snapshot.nodes:
        if not node.allowed:
            continue
        row = await repo.get_task(db, node.task_id)
        if row and row["status"] in {"draft", "open"}:
            dor = await load_dor_snapshot(db, node.task_id)
            report = await calculate_readiness_with_recommendations(
                db, node.task_id, snapshot=dor
            )
            drafts[node.task_id] = PreparedDraft(dor, report)
    return drafts


async def open_prepared_drafts(
    db: aiosqlite.Connection,
    snapshot: PlanSnapshot,
    version: str,
    drafts: dict[int, PreparedDraft],
) -> None:
    from hub.services.lifecycle import (
        _open_draft_under_lock,
        _require_freeze_admission,
        _StatementMoved,
    )

    for node in snapshot.nodes:
        if not node.allowed:
            continue
        row = await repo.get_task(db, node.task_id)
        if not row or row["status"] not in {"draft", "open", "completed"}:
            raise HTTPException(
                409,
                detail={"reason": "plan_task_not_admissible", "task_id": node.task_id},
            )
        if row["status"] == "completed":
            continue
        draft = drafts.get(node.task_id)
        if draft is None:
            raise HTTPException(
                409, detail={"reason": "plan_task_moved", "task_id": node.task_id}
            )
        if row["status"] == "open":
            if (
                draft.snapshot.fingerprint()
                != (await load_dor_snapshot(db, node.task_id)).fingerprint()
            ):
                raise HTTPException(
                    409, detail={"reason": "plan_task_moved", "task_id": node.task_id}
                )
            if not draft.readiness.dor_passed:
                raise HTTPException(
                    422, detail={"reason": "dor_failed", "task_id": node.task_id}
                )
            _require_freeze_admission(
                await repo.resolve_project_for_task(db, node.task_id),
                row["work_type"],
                row["freeze_rationale"] or "",
            )
            continue
        try:
            await _open_draft_under_lock(
                db,
                node.task_id,
                dict(row),
                TaskApprove(),
                draft.readiness,
                draft.snapshot,
                caller_owns_tx=True,
                approve_plan={"epic_id": snapshot.epic_id, "plan_version": version},
            )
        except _StatementMoved as exc:
            raise HTTPException(
                409, detail={"reason": "plan_task_moved", "task_id": node.task_id}
            ) from exc
