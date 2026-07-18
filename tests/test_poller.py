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
        db,
        title="Running task",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="running",
        auto_review=False,
        task_type="task",
        parent_id=None,
        priority="medium",
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
        db,
        title="Stale task",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="running",
        auto_review=False,
        task_type="task",
        parent_id=None,
        priority="medium",
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
        db,
        title="Auto-review task",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="running",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
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


# ---- Universal Review Gate unification (#309) ----


async def _make_review_task(db, *, review_job_id="rev-1", generation=1) -> int:
    task_id = await repo.create_task(
        db,
        title="Headless review task",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="review",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(db, task_id, review_job_id=review_job_id)
    for _ in range(generation):
        await repo.bump_submission_generation(db, task_id)
    await db.commit()
    return task_id


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_poll_headless_approved_converges_on_gate_transition(mock_sleep, db):
    # AC-1: a headless APPROVED verdict completes through the same
    # gate-checked transition as external submit_review — verdict stays
    # bound to the completed submission (no generation bump).
    task_id = await _make_review_task(db)
    await repo.add_task_update(db, task_id, "reviewer", "review", "LGTM\nAPPROVED")
    await db.commit()

    mock_dispatch = NoopDispatch()
    mock_dispatch.get_job = MagicMock(
        return_value={"status": "completed", "exit_code": 0}
    )
    plugins.dispatch = mock_dispatch

    with (
        patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
        pytest.raises(_BreakLoop),
    ):
        await _poll_running_tasks(_make_app(db))

    d = dict(await repo.get_task(db, task_id))
    assert d["status"] == "completed"
    assert d["review_verdict"] == "approved"
    assert d["review_verdict_generation"] == 1
    assert d["submission_generation"] == 1  # no bump on approved completion


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_poll_ignores_client_driven_review(mock_sleep, db):
    # AC-2: review without review_job_id belongs to an external reviewer —
    # the poller must not treat it as its own or as a failed dispatch.
    task_id = await _make_review_task(db, review_job_id=None)
    await db.commit()

    mock_dispatch = NoopDispatch()
    mock_dispatch.get_job = MagicMock(return_value={"status": "failed", "exit_code": 1})
    plugins.dispatch = mock_dispatch

    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(_make_app(db))

    d = dict(await repo.get_task(db, task_id))
    assert d["status"] == "review"  # untouched
    mock_dispatch.get_job.assert_not_called()


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_poll_failed_review_job_escalates_not_completes(mock_sleep, db):
    # Gap #1 from the status-model audit: a crashed review job must not
    # complete the task.
    task_id = await _make_review_task(db)

    mock_dispatch = NoopDispatch()
    mock_dispatch.get_job = MagicMock(return_value={"status": "failed", "exit_code": 2})
    plugins.dispatch = mock_dispatch

    with (
        patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
        pytest.raises(_BreakLoop),
    ):
        await _poll_running_tasks(_make_app(db))

    d = dict(await repo.get_task(db, task_id))
    assert d["status"] == "needs_decision"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert any(
        u["kind"] == "alert" and "Review job failed" in u["content"] for u in updates
    )


# ---- Stale watchdog for silent dead-end statuses (#319) ----


async def _make_stale_task(db, *, status, review_job_id=None, minutes=999) -> int:
    task_id = await repo.create_task(
        db,
        title=f"Stale {status}",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status=status,
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    if review_job_id:
        await repo.update_task(db, task_id, review_job_id=review_job_id)
    await db.execute(
        "UPDATE tasks SET updated_at = datetime('now', ?) WHERE id=?",
        (f"-{minutes} minutes", task_id),
    )
    await db.commit()
    return task_id


async def _stale_alerts(db, task_id) -> list[str]:
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    return [
        u["content"]
        for u in updates
        if u["kind"] == "alert" and "stale" in u["content"].lower()
    ]


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_stale_client_review_alerted_once(mock_sleep, db):
    # AC-1: client-driven review past the threshold gets exactly one alert;
    # the status stays review and a second pass does not duplicate it.
    task_id = await _make_stale_task(db, status="review")

    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(_make_app(db))
    alerts = await _stale_alerts(db, task_id)
    assert len(alerts) == 1
    assert "review" in alerts[0]
    assert "hub_submit_review" in alerts[0]
    assert dict(await repo.get_task(db, task_id))["status"] == "review"

    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(_make_app(db))
    assert len(await _stale_alerts(db, task_id)) == 1


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_stale_claimed_and_needs_info_alerted(mock_sleep, db):
    # AC-2: claimed and needs_info past their thresholds each get an alert
    # naming the status and the expected action.
    claimed_id = await _make_stale_task(db, status="claimed")
    info_id = await _make_stale_task(db, status="needs_info")

    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(_make_app(db))

    claimed_alerts = await _stale_alerts(db, claimed_id)
    assert len(claimed_alerts) == 1
    assert "claimed" in claimed_alerts[0]
    assert "hub_pair_start" in claimed_alerts[0]

    info_alerts = await _stale_alerts(db, info_id)
    assert len(info_alerts) == 1
    assert "needs_info" in info_alerts[0]
    assert "hub_answer_question" in info_alerts[0]


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_stale_headless_review_not_alerted(mock_sleep, db):
    # AC-3: headless review (review_job_id set) belongs to the conveyor —
    # no stale alert. Dispatch returns no job so the review loop skips it.
    task_id = await _make_stale_task(db, status="review", review_job_id="rev-9")

    mock_dispatch = NoopDispatch()
    mock_dispatch.get_job = MagicMock(return_value=None)
    plugins.dispatch = mock_dispatch

    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(_make_app(db))
    assert await _stale_alerts(db, task_id) == []


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_poll_uses_persisted_verdict_without_text_scan(mock_sleep, db):
    # AC-1 (#326): structured verdict for the current generation wins;
    # extract_review_verdict is never called.
    from unittest.mock import patch as _patch

    task_id = await _make_review_task(db)
    await repo.record_review_verdict(db, task_id, "approved")
    await db.commit()

    mock_dispatch = NoopDispatch()
    mock_dispatch.get_job = MagicMock(
        return_value={"status": "completed", "exit_code": 0}
    )
    plugins.dispatch = mock_dispatch

    with (
        _patch("hub.poller.services.extract_review_verdict") as extract_mock,
        _patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
        pytest.raises(_BreakLoop),
    ):
        await _poll_running_tasks(_make_app(db))

    extract_mock.assert_not_called()
    d = dict(await repo.get_task(db, task_id))
    assert d["status"] == "completed"


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_poll_falls_back_to_text_scan_for_legacy_reviewer(mock_sleep, db):
    # AC-2 (#326): no persisted verdict → the old text channel still works.
    task_id = await _make_review_task(db)
    await repo.add_task_update(db, task_id, "reviewer", "review", "ok\nAPPROVED")
    await db.commit()

    mock_dispatch = NoopDispatch()
    mock_dispatch.get_job = MagicMock(
        return_value={"status": "completed", "exit_code": 0}
    )
    plugins.dispatch = mock_dispatch

    with (
        patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
        pytest.raises(_BreakLoop),
    ):
        await _poll_running_tasks(_make_app(db))

    d = dict(await repo.get_task(db, task_id))
    assert d["status"] == "completed"
    assert d["review_verdict"] == "approved"


async def test_events_pruning(db):
    # AC-7 (#349): maintenance removes events older than 14 days only.
    from hub import repository as repo_module

    await repo_module.insert_event(db, kind="ancient", task_id=1)
    await repo_module.insert_event(db, kind="fresh", task_id=2)
    await db.execute(
        "UPDATE events SET created_at = datetime('now', '-20 days') "
        "WHERE kind = 'ancient'"
    )
    await db.commit()

    removed = await repo_module.prune_events(db, keep_days=14)
    await db.commit()

    assert removed == 1
    kinds = {r["kind"] for r in await repo_module.list_events(db, since=0)}
    assert kinds == {"fresh"}


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_ci_no_pr_attempts_persist_across_restart(mock_sleep, db):
    # AC-4 (#416): the no-PR retry budget lives in the row, so a restart — a
    # fresh process with no in-memory state — keeps counting from the DB
    # instead of restarting at zero. The module holds no CI state at all.
    import hub.poller as poller_mod

    assert not hasattr(poller_mod, "_ci_no_pr_retries")
    assert not hasattr(poller_mod, "_ci_pushed_at")

    task_id = await _make_stale_task(db, status="ci_check", minutes=0)
    # Two attempts were already burned before the "restart".
    await repo.update_task(db, task_id, ci_no_pr_attempts=2)
    await db.commit()

    plugins.git_ops = NoopGitOps()
    plugins.dispatch = NoopDispatch()

    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(_make_app(db))

    row = dict(await repo.get_task(db, task_id))
    # 2 (from DB) + 1 = 3 → escalated, proving the counter was not reset by
    # the restart. The budget is cleared as the task leaves the conveyor.
    assert row["status"] == "needs_decision"
    assert row["ci_no_pr_attempts"] == 0
