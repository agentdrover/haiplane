from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hub import repository as repo
from hub.integrations.noop import NoopDispatch, NoopGitOps
from hub.integrations.registry import plugins
from hub.poller import _poll_running_tasks


class _BreakLoop(Exception):
    pass


def _sleep_once() -> AsyncMock:
    """Return an asyncio.sleep mock that lets one iteration run, then breaks."""
    call_count = 0

    async def _side_effect(_seconds: float) -> None:
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise _BreakLoop

    return AsyncMock(side_effect=_side_effect)


def _make_app(db) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(db=db))


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_poll_no_running_tasks(mock_sleep, db):
    await db.commit()
    app = _make_app(db)

    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(app)


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_poll_completed_task(mock_sleep, db):
    task_id = await repo.create_task(
        db, title="Running task", description="", runtime="auto",
        source="human", assigned_agent="", rationale="", status="running",
        auto_review=False, task_type="task", parent_id=None, priority="medium",
    )
    await repo.update_task(db, task_id, job_id="job-123")
    await repo.add_task_update(db, task_id, "dev", "done", "All done")
    await db.commit()

    app = _make_app(db)

    mock_dispatch = NoopDispatch()
    mock_dispatch.get_job = MagicMock(
        return_value={"status": "completed", "exit_code": 0, "result_text": "ok"}
    )
    plugins.dispatch = mock_dispatch

    with (
        patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
        pytest.raises(_BreakLoop),
    ):
        await _poll_running_tasks(app)

    row = await repo.get_task(db, task_id)
    assert dict(row)["status"] == "completed"


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_poll_stale_detection(mock_sleep, db):
    task_id = await repo.create_task(
        db, title="Stale task", description="", runtime="auto",
        source="human", assigned_agent="", rationale="", status="running",
        auto_review=False, task_type="task", parent_id=None, priority="medium",
    )
    await db.execute(
        "UPDATE tasks SET updated_at = datetime('now', '-120 minutes') WHERE id=?",
        (task_id,),
    )
    await db.commit()

    app = _make_app(db)

    with (
        patch("hub.poller.config.STALE_THRESHOLD_MINUTES", 30),
        pytest.raises(_BreakLoop),
    ):
        await _poll_running_tasks(app)

    updates = await repo.get_task_updates(db, task_id)
    alert_updates = [u for u in updates if dict(u)["kind"] == "alert"]
    assert len(alert_updates) >= 1
    assert "stale" in dict(alert_updates[0])["content"].lower()


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_poll_review_dispatch(mock_sleep, db):
    task_id = await repo.create_task(
        db, title="Auto-review task", description="", runtime="auto",
        source="human", assigned_agent="", rationale="", status="running",
        auto_review=True, task_type="task", parent_id=None, priority="medium",
    )
    await repo.update_task(db, task_id, job_id="job-456", branch="task-1/test")
    await repo.add_task_update(db, task_id, "agent", "done", "Task completed")
    await db.commit()

    app = _make_app(db)

    mock_dispatch = NoopDispatch()
    mock_dispatch.get_job = MagicMock(
        return_value={"status": "completed", "exit_code": 0, "result_text": "ok"}
    )
    plugins.dispatch = mock_dispatch

    mock_git = NoopGitOps()
    mock_git.checkout = AsyncMock(return_value=True)
    mock_git.auto_commit = AsyncMock(return_value=True)
    mock_git.squash_branch = AsyncMock(return_value=True)
    mock_git.push_branch = AsyncMock(return_value=True)
    mock_git.create_pr = AsyncMock(return_value=42)
    plugins.git_ops = mock_git

    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(app)

    row = await repo.get_task(db, task_id)
    task = dict(row)
    assert task["status"] == "ci_check"
    assert task["pr_number"] == 42
