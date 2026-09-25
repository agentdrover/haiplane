"""Возврат из fix_requested должен работать при живом job_id — это прод-форма статуса."""

from __future__ import annotations

from unittest.mock import MagicMock

import aiosqlite
from fastapi import HTTPException

from hub import repository as repo
from hub.integrations.registry import plugins
from hub.models import TaskReturnToWork
from hub.services.lifecycle import return_to_work


async def test_return_to_work_from_fix_requested_with_live_job(
    db: aiosqlite.Connection,
) -> None:
    task_id = await repo.create_task(
        db,
        title="брошенный fix_requested",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(
        db,
        task_id,
        status="fix_requested",
        job_id="oc-dev-job-abandoned",
        claimed_by="dev",
        claim_session_id="sess-gone",
        claimed_at="2026-09-17T12:00:00+00:00",
    )
    await db.commit()
    plugins.dispatch.get_job = MagicMock(
        return_value={"status": "running", "exit_code": None}
    )

    try:
        view = await return_to_work(
            db,
            task_id,
            TaskReturnToWork(reason="исполнитель пропал, job всё ещё running"),
            actor="denis",
        )
    except HTTPException as exc:
        assert exc.status_code != 409, (
            f"live job_id is the production form of fix_requested; "
            f"return-to-work 409: {exc.detail}"
        )
        raise

    assert view.status.value == "open"
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "open"
    assert not (row.get("job_id") or "")
