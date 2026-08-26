from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hub import config
from hub import repository as repo
from hub import services
from hub.integrations.noop import NoopDispatch, NoopGitOps
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
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


async def _run_poll_once(db) -> None:
    """Run the poll body exactly once with a fresh break counter.

    The shared ``_sleep_once`` mock keeps its counter for a whole test, so a
    second ``_poll_running_tasks`` call would break before its body runs. A
    fresh mock per invocation lets a test drive several real poll passes.
    """
    with patch("hub.poller.asyncio.sleep", new_callable=_sleep_once):
        with pytest.raises(_BreakLoop):
            await _poll_running_tasks(_make_app(db))


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


async def test_stale_machine_dead_ends_alerted(db):
    # AC-1/AC-3 (#393): ci_check, fix_requested and pending_report past their
    # thresholds each get exactly one alert naming the status; the status is
    # unchanged and a second real pass does not duplicate the alert. The durable
    # ci_no_pr_attempts counter (#416) stays under MAX_CI_NO_PR_ATTEMPTS across
    # these two passes, so the CI conveyor does not escalate ci_check.
    ci_id = await _make_stale_task(db, status="ci_check")
    fix_id = await _make_stale_task(db, status="fix_requested")
    pending_id = await _make_stale_task(db, status="pending_report")

    await _run_poll_once(db)

    for tid, status in (
        (ci_id, "ci_check"),
        (fix_id, "fix_requested"),
        (pending_id, "pending_report"),
    ):
        alerts = await _stale_alerts(db, tid)
        assert len(alerts) == 1, status
        assert f"stale in {status}" in alerts[0]
        assert "hub_force_complete_task" in alerts[0]
        assert dict(await repo.get_task(db, tid))["status"] == status

    await _run_poll_once(db)
    for tid in (ci_id, fix_id, pending_id):
        assert len(await _stale_alerts(db, tid)) == 1


async def test_stale_pending_report_threshold_from_config(db):
    # AC-5 (#393): the threshold is read from config, not a literal —
    # pending_report has no conveyor, so the same task is silent under a 30m
    # threshold and alerted under a 5m one.
    task_id = await _make_stale_task(db, status="pending_report", minutes=10)

    with patch("hub.poller.config.STALE_PENDING_REPORT_MINUTES", 30):
        await _run_poll_once(db)
    assert await _stale_alerts(db, task_id) == []

    with patch("hub.poller.config.STALE_PENDING_REPORT_MINUTES", 5):
        await _run_poll_once(db)
    assert len(await _stale_alerts(db, task_id)) == 1


async def test_stale_alert_not_suppressed_across_statuses(db):
    # AC-2 (#393): a historical stale alert in one status must not suppress a
    # later stale alert in a different status. Uses claimed→needs_info, both
    # free of conveyor interference, to isolate the watchdog dedup.
    task_id = await _make_stale_task(db, status="claimed")
    await _run_poll_once(db)
    assert len(await _stale_alerts(db, task_id)) == 1

    await repo.update_task(db, task_id, status="needs_info")
    await db.execute(
        "UPDATE tasks SET updated_at = datetime('now', '-999 minutes') WHERE id=?",
        (task_id,),
    )
    await db.commit()

    await _run_poll_once(db)
    alerts = await _stale_alerts(db, task_id)
    assert len(alerts) == 2
    assert any("stale in needs_info" in a for a in alerts)


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


# ---- Bounded recovery for missing jobs and expired claims (#417) ----


async def _run_poll_once(db) -> None:
    """Run the poll body once with a fresh break counter (see #416 tests)."""
    with patch("hub.poller.asyncio.sleep", new_callable=_sleep_once):
        with pytest.raises(_BreakLoop):
            await _poll_running_tasks(_make_app(db))


async def _make_headless(db, *, status, job_field="job_id", job_value="gone-1"):
    task_id = await repo.create_task(
        db,
        title=f"Headless {status}",
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
    await repo.update_task(db, task_id, **{job_field: job_value})
    await db.commit()
    return task_id


async def _events_for(db, task_id, kind):
    rows = await repo.list_events(db, kinds=[kind])
    return [dict(r) for r in rows if dict(r)["task_id"] == task_id]


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_missing_dispatch_job_escalates_after_grace(mock_sleep, db):
    # AC-1 (#417): a headless running task whose dispatch job is gone starts a
    # grace clock on first miss and escalates once to needs_decision after it.
    task_id = await _make_headless(db, status="running")
    mock_dispatch = NoopDispatch()
    mock_dispatch.get_job = MagicMock(return_value=None)
    plugins.dispatch = mock_dispatch

    # First miss: clock starts, status unchanged (within grace).
    await _run_poll_once(db)
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "running"
    assert row["job_missing_since"] is not None

    # Past the grace: escalate once with a machine reason.
    await db.execute(
        "UPDATE tasks SET job_missing_since = datetime('now', '-10 minutes') WHERE id=?",
        (task_id,),
    )
    await db.commit()
    await _run_poll_once(db)
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "needs_decision"
    events = await _events_for(db, task_id, "needs_decision")
    assert len(events) == 1
    assert "dispatch_job_missing" in (events[0]["payload"] or "")

    # Idempotent: needs_decision is not re-selected, so no second event.
    await _run_poll_once(db)
    assert len(await _events_for(db, task_id, "needs_decision")) == 1


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_missing_review_job_escalates_client_review_untouched(mock_sleep, db):
    # AC-2 (#417): a headless review with an unknown review_job_id escalates,
    # while a client-driven review (no review_job_id) is never touched.
    headless = await _make_headless(
        db, status="review", job_field="review_job_id", job_value="rev-gone"
    )
    await db.execute(
        "UPDATE tasks SET job_missing_since = datetime('now', '-10 minutes') WHERE id=?",
        (headless,),
    )
    client = await repo.create_task(
        db,
        title="Client review",
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
    await db.commit()

    mock_dispatch = NoopDispatch()
    mock_dispatch.get_job = MagicMock(return_value=None)
    plugins.dispatch = mock_dispatch

    await _run_poll_once(db)
    assert dict(await repo.get_task(db, headless))["status"] == "needs_decision"
    assert dict(await repo.get_task(db, client))["status"] == "review"


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_expired_claim_released_to_open(mock_sleep, db):
    # AC-3 (#417): a claim held past the lease is released back to open, all
    # claim fields cleared, with exactly one claim_expired event.
    task_id = await repo.create_task(
        db,
        title="Abandoned claim",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="claimed",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(db, task_id, claimed_by="dev", claim_session_id="sess-1")
    await db.execute(
        "UPDATE tasks SET claimed_at = datetime('now', '-300 minutes') WHERE id=?",
        (task_id,),
    )
    await db.commit()
    plugins.dispatch = NoopDispatch()

    await _run_poll_once(db)
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "open"
    assert row["claimed_by"] is None
    assert row["claim_session_id"] is None
    assert row["claimed_at"] is None
    assert len(await _events_for(db, task_id, "claim_expired")) == 1

    # Idempotent: an already-released task is not re-expired.
    await _run_poll_once(db)
    assert len(await _events_for(db, task_id, "claim_expired")) == 1


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_missing_job_decision_uses_persisted_timestamp(mock_sleep, db):
    # AC-4 (#417): the grace decision reads the persisted job_missing_since, so
    # it is identical whether or not the process restarted — a timestamp within
    # grace never escalates, one past it always does, in a single poll.
    task_id = await _make_headless(db, status="running")
    mock_dispatch = NoopDispatch()
    mock_dispatch.get_job = MagicMock(return_value=None)
    plugins.dispatch = mock_dispatch

    await db.execute(
        "UPDATE tasks SET job_missing_since = datetime('now', '-4 minutes') WHERE id=?",
        (task_id,),
    )
    await db.commit()
    await _run_poll_once(db)
    assert dict(await repo.get_task(db, task_id))["status"] == "running"

    await db.execute(
        "UPDATE tasks SET job_missing_since = datetime('now', '-6 minutes') WHERE id=?",
        (task_id,),
    )
    await db.commit()
    await _run_poll_once(db)
    assert dict(await repo.get_task(db, task_id))["status"] == "needs_decision"


# ---- Ownership/deadline matrix (#418) ----


def test_lifecycle_matrix_covers_all_instances():
    # AC-1 (#418): every status plus each running/review discriminator resolves
    # to a policy with an owner, next actor, surface, and — for machine-owned
    # instances — a finite deadline config, escalation and reason.
    from hub import config
    from hub.lifecycle_matrix import (
        LIFECYCLE_MATRIX,
        OWNER_MACHINE,
        machine_deadline_policies,
    )

    expected = {
        "draft",
        "open",
        "claimed",
        "needs_info",
        "needs_decision",
        "running:headless",
        "running:pair",
        "review:headless",
        "review:client",
        "fix_requested",
        "ci_check",
        "pending_report",
    }
    assert set(LIFECYCLE_MATRIX) == expected

    for p in LIFECYCLE_MATRIX.values():
        assert p.next_actor
        assert p.surface
        if p.owner == OWNER_MACHINE:
            assert p.deadline_config is not None
            assert isinstance(getattr(config, p.deadline_config), int)
            assert p.escalation in ("needs_decision", "open")
            assert p.reason

    # claimed escalates to open (handled by #417); the rest to needs_decision.
    dec = [p for p in machine_deadline_policies() if p.escalation == "needs_decision"]
    assert {p.status for p in dec} == {
        "running",
        "review",
        "fix_requested",
        "ci_check",
        "pending_report",
    }


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_machine_deadline_transitions_to_needs_decision(mock_sleep, db):
    # AC-2 (#418): a machine-owned instance past its deadline is transitioned
    # once to needs_decision with a reason-coded event. pending_report has no
    # conveyor, so the matrix backstop is the only thing that acts.
    task_id = await _make_stale_task(db, status="pending_report", minutes=0)
    await db.execute(
        "UPDATE tasks SET status_entered_at = datetime('now', '-500 minutes') WHERE id=?",
        (task_id,),
    )
    await db.commit()
    plugins.dispatch = NoopDispatch()

    await _run_poll_once(db)
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "needs_decision"
    events = await _events_for(db, task_id, "needs_decision")
    assert len(events) == 1
    assert "pending_report_deadline" in (events[0]["payload"] or "")

    # Idempotent: needs_decision is not machine-owned, so no second escalation.
    await _run_poll_once(db)
    assert len(await _events_for(db, task_id, "needs_decision")) == 1


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_machine_deadline_leaves_fresh_task_untouched(mock_sleep, db):
    # AC-2 guard (#418): a task within its deadline is never transitioned — the
    # backstop must not yank live work.
    task_id = await _make_stale_task(db, status="pending_report", minutes=0)
    await db.commit()  # status_entered_at is now (fresh)
    plugins.dispatch = NoopDispatch()

    await _run_poll_once(db)
    assert dict(await repo.get_task(db, task_id))["status"] == "pending_report"


# ---- Typed CI outcome → poller transitions (#419) ----


async def _make_ci_task(db, *, pr_number=99):
    task_id = await repo.create_task(
        db,
        title="CI task",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="ci_check",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(db, task_id, pr_number=pr_number, branch="task-x/b")
    # Past the CI grace so the loop actually probes the PR.
    await db.execute(
        "UPDATE tasks SET ci_check_started_at = datetime('now', '-30 minutes') WHERE id=?",
        (task_id,),
    )
    await db.commit()
    return task_id


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_ci_absent_dispatches_review(mock_sleep, db):
    # AC-2 (#419): a PR with no checks skips the CI wait, records an audit
    # update and goes to review instead of waiting forever.
    task_id = await _make_ci_task(db)
    mock_dispatch = NoopDispatch()
    mock_dispatch.submit_task = AsyncMock(return_value={"job_id": "rev-1"})
    plugins.dispatch = mock_dispatch
    mock_git = NoopGitOps()
    mock_git.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.absent, "no_checks")
    )
    plugins.git_ops = mock_git

    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(_make_app(db))

    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "review"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert any("CI checks absent" in u["content"] for u in updates)


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_ci_pending_stays_in_ci_check(mock_sleep, db):
    # AC-3 (#419): pending within the deadline keeps the task in ci_check with
    # no escalation and no duplicate alert.
    task_id = await _make_ci_task(db)
    mock_git = NoopGitOps()
    mock_git.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.pending, "checks_running")
    )
    plugins.git_ops = mock_git
    plugins.dispatch = NoopDispatch()

    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(_make_app(db))

    assert dict(await repo.get_task(db, task_id))["status"] == "ci_check"
    assert await _events_for(db, task_id, "needs_decision") == []


async def test_ci_unavailable_before_and_after_deadline(db):
    # AC-4 (#419): unavailable keeps a diagnostic and retries before the
    # deadline; past the #418 deadline the task escalates to needs_decision,
    # idempotently.
    task_id = await _make_ci_task(db)
    mock_git = NoopGitOps()
    mock_git.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.unavailable, "gh_error")
    )
    plugins.git_ops = mock_git
    plugins.dispatch = NoopDispatch()

    await _run_poll_once(db)
    assert dict(await repo.get_task(db, task_id))["status"] == "ci_check"

    await db.execute(
        "UPDATE tasks SET status_entered_at = datetime('now', '-500 minutes') WHERE id=?",
        (task_id,),
    )
    await db.commit()
    await _run_poll_once(db)
    assert dict(await repo.get_task(db, task_id))["status"] == "needs_decision"

    await _run_poll_once(db)
    assert len(await _events_for(db, task_id, "needs_decision")) == 1


# ---- Project repo context propagation (#420) ----


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_ci_check_passes_project_context(mock_sleep, db):
    # AC-1/AC-3 (#420): the ci_check conveyor resolves project context and
    # passes repo + gh_repo to the CI probe, not the global default.
    await _make_ci_task(db)
    mock_git = NoopGitOps()
    mock_git.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.pending, "checks_running")
    )
    plugins.git_ops = mock_git
    plugins.dispatch = NoopDispatch()

    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(_make_app(db))

    assert mock_git.check_pr_ci.await_count >= 1
    kwargs = mock_git.check_pr_ci.await_args.kwargs
    assert "gh_repo" in kwargs
    assert "repo" in kwargs


# ---- At-most-once arbiter dispatch (#421) ----


async def _make_review_gen1(db):
    task_id = await repo.create_task(
        db,
        title="Arbiter task",
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
    await repo.bump_submission_generation(db, task_id)  # generation 1
    await db.commit()
    return task_id


async def test_arbiter_dispatch_skipped_when_marker_exists(db):
    # AC-2 (#421): an existing dispatching/running marker for the generation
    # means a repeat call never submits a second paid job.
    task_id = await _make_review_gen1(db)
    await repo.claim_arbiter_dispatch(db, task_id, 1)
    await repo.mark_arbiter_running(db, task_id, "arb-1")
    await db.commit()

    mock_dispatch = NoopDispatch()
    mock_dispatch.submit_task = AsyncMock(return_value={"job_id": "arb-2"})
    plugins.dispatch = mock_dispatch

    task = dict(await repo.get_task(db, task_id))
    await services.dispatch_arbiter(db, task, [])

    mock_dispatch.submit_task.assert_not_called()


async def test_arbiter_dispatch_marks_running_on_success(db):
    # AC-3 (#421): a successful submit atomically moves the same marker to
    # running and records the arbiter job id.
    task_id = await _make_review_gen1(db)
    mock_dispatch = NoopDispatch()
    mock_dispatch.submit_task = AsyncMock(return_value={"job_id": "arb-9"})
    plugins.dispatch = mock_dispatch

    task = dict(await repo.get_task(db, task_id))
    await services.dispatch_arbiter(db, task, [])

    row = dict(await repo.get_task(db, task_id))
    assert row["arbiter_state"] == "running"
    assert row["arbiter_job_id"] == "arb-9"
    assert row["review_job_id"] == "arb-9"
    assert row["status"] == "review"
    mock_dispatch.submit_task.assert_called_once()


async def test_arbiter_dispatching_ambiguity_escalates(db):
    # AC-4 (#421): a marker stuck dispatching (submit started, no job id) past
    # the grace fails safe to needs_decision, never re-submitting.
    task_id = await _make_review_gen1(db)
    await repo.claim_arbiter_dispatch(db, task_id, 1)  # dispatching, no job id
    await db.execute(
        "UPDATE tasks SET arbiter_dispatch_at = datetime('now', '-60 minutes') WHERE id=?",
        (task_id,),
    )
    await db.commit()

    mock_dispatch = NoopDispatch()
    mock_dispatch.submit_task = AsyncMock()
    plugins.dispatch = mock_dispatch

    await _run_poll_once(db)

    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "needs_decision"
    assert row["arbiter_state"] == "finished"
    events = await _events_for(db, task_id, "needs_decision")
    assert any("arbiter_dispatch_ambiguous" in (e["payload"] or "") for e in events)
    mock_dispatch.submit_task.assert_not_called()


# ---- Server-owned arbiter termination (#422) ----


async def _make_arbiter_running(db, *, job_id="arb-1"):
    task_id = await _make_review_gen1(db)  # review, generation 1
    await repo.claim_arbiter_dispatch(db, task_id, 1)
    await repo.mark_arbiter_running(db, task_id, job_id)
    await repo.update_task(db, task_id, review_job_id=job_id)
    await db.commit()
    return task_id


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_arbiter_job_completed_routes_to_needs_decision(mock_sleep, db):
    # AC-1 (#422): a completed arbiter job — with no agent arbitration update —
    # ends the phase server-side: needs_decision once, marker finished, audit
    # summary from the job result.
    task_id = await _make_arbiter_running(db)
    mock_dispatch = NoopDispatch()
    mock_dispatch.get_job = MagicMock(
        return_value={
            "status": "completed",
            "exit_code": 0,
            "result_text": "Arbiter: the change is incomplete, fix X.",
        }
    )
    plugins.dispatch = mock_dispatch

    with (
        patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
        pytest.raises(_BreakLoop),
    ):
        await _poll_running_tasks(_make_app(db))

    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "needs_decision"
    assert row["arbiter_state"] == "finished"
    events = await _events_for(db, task_id, "needs_decision")
    assert any("arbitration_finished" in (e["payload"] or "") for e in events)
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert any("Arbiter summary" in u["content"] for u in updates)


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_arbiter_job_failed_routes_to_needs_decision(mock_sleep, db):
    # AC-2 (#422): a failed arbiter job goes to needs_decision with a failure
    # reason and never re-dispatches.
    task_id = await _make_arbiter_running(db)
    mock_dispatch = NoopDispatch()
    mock_dispatch.get_job = MagicMock(return_value={"status": "failed", "exit_code": 1})
    mock_dispatch.submit_task = AsyncMock(return_value={"job_id": "arb-2"})
    plugins.dispatch = mock_dispatch

    with (
        patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
        pytest.raises(_BreakLoop),
    ):
        await _poll_running_tasks(_make_app(db))

    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "needs_decision"
    assert row["arbiter_state"] == "finished"
    events = await _events_for(db, task_id, "needs_decision")
    assert any("arbiter_job_failed" in (e["payload"] or "") for e in events)
    mock_dispatch.submit_task.assert_not_called()


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_new_submission_completes_despite_old_arbitration(mock_sleep, db):
    # AC-4 (#422): a stale finished arbiter marker from an earlier generation
    # does not block a new submission whose current verdict is APPROVED.
    task_id = await _make_review_task(db, review_job_id="rev-2", generation=2)
    await repo.update_task(
        db,
        task_id,
        arbiter_state="finished",
        arbiter_job_id="arb-old",
        arbiter_generation=1,
    )
    await repo.record_review_verdict(db, task_id, "approved")  # binds to gen 2
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

    assert dict(await repo.get_task(db, task_id))["status"] == "completed"


# ---- Unified review budget: headless boundary (#423) ----


@pytest.mark.parametrize("review_cycle, expects_arbiter", [(2, False), (3, True)])
@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_headless_review_budget_boundary(
    mock_sleep, db, review_cycle, expects_arbiter
):
    # AC-1 (#423): with MAX=3 the headless flow dispatches fixes 1,2,3 and only
    # escalates to arbiter at review_cycle=3 — the same budget as pair (no more
    # off-by-one that stopped headless one fix early).
    task_id = await _make_review_task(db, review_job_id="rev-1", generation=1)
    await repo.update_task(db, task_id, review_cycle=review_cycle)
    await repo.record_review_verdict(db, task_id, "changes_requested")
    await db.commit()

    mock_dispatch = NoopDispatch()
    mock_dispatch.get_job = MagicMock(
        return_value={"status": "completed", "exit_code": 0}
    )
    plugins.dispatch = mock_dispatch
    mock_git = NoopGitOps()
    mock_git.checkout = AsyncMock(return_value=True)
    plugins.git_ops = mock_git

    with (
        patch("hub.poller.services.dispatch_fix", new_callable=AsyncMock) as m_fix,
        patch("hub.poller.services.dispatch_arbiter", new_callable=AsyncMock) as m_arb,
        patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
        pytest.raises(_BreakLoop),
    ):
        await _poll_running_tasks(_make_app(db))

    if expects_arbiter:
        m_arb.assert_called_once()
        m_fix.assert_not_called()
    else:
        m_fix.assert_called_once()
        m_arb.assert_not_called()


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_ci_check_passes_the_projects_actual_gh_repo(mock_sleep, db):
    """#362 AC-1: the VALUE must reach gh, not merely the keyword.

    The neighbouring test asserts only that "gh_repo" is in kwargs, which a
    None would satisfy. A wrong repo here means gh operates on a same-numbered
    PR in the global repository — the high risk recorded on this task.
    """
    from hub.db import seed_default_project

    await seed_default_project(db)
    pid = await repo.create_project(
        db,
        slug="other-proj",
        name="Other",
        repo_name="org/other-repo",
        workspace_path="/srv/other",
        default_branch="develop",
    )
    task_id = await _make_ci_task(db)
    await repo.update_task(db, task_id, project_id=pid)
    await db.commit()

    mock_git = NoopGitOps()
    mock_git.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.pending, "checks_running")
    )
    plugins.git_ops = mock_git
    plugins.dispatch = NoopDispatch()

    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(_make_app(db))

    kwargs = mock_git.check_pr_ci.await_args.kwargs
    assert kwargs["gh_repo"] == "org/other-repo"
    assert kwargs["repo"] == "/srv/other"


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_approved_merge_targets_the_projects_repo(mock_sleep, db):
    """#362 AC-1, the dangerous half: merging.

    check_pr_ci had a poller-level test; merge_pr did not. A merge aimed at the
    wrong repository merges a stranger's PR that happens to share the number.
    """
    from hub.db import seed_default_project

    await seed_default_project(db)
    pid = await repo.create_project(
        db,
        slug="other-proj-2",
        name="Other 2",
        repo_name="org/other-repo",
        workspace_path="/srv/other",
        default_branch="develop",
    )
    task_id = await _make_review_task(db)
    await repo.update_task(db, task_id, project_id=pid, pr_number=99, branch="task-x/b")
    await repo.add_task_update(db, task_id, "reviewer", "review", "LGTM\nAPPROVED")
    await db.commit()

    mock_git = NoopGitOps()
    mock_git.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.passed, "ok")
    )
    mock_git.merge_pr = AsyncMock(return_value=False)
    plugins.git_ops = mock_git
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

    assert mock_git.merge_pr.await_count >= 1, "the approved path must reach merge_pr"
    assert mock_git.merge_pr.await_args.kwargs["gh_repo"] == "org/other-repo"


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_poller_creates_the_pr_in_the_projects_repo(mock_sleep, db):
    """#362 AC-1, third gh call site. Found by mutation, not by reading.

    A ci_check task without a PR gets one from the poller. Replacing this
    call's gh_repo with None survived the whole poller suite, so the PR could
    have been opened against the global repository with nothing to notice.
    """
    from hub.db import seed_default_project

    await seed_default_project(db)
    pid = await repo.create_project(
        db,
        slug="other-proj-3",
        name="Other 3",
        repo_name="org/other-repo",
        workspace_path="/srv/other",
        default_branch="develop",
    )
    task_id = await _make_ci_task(db)
    await repo.update_task(db, task_id, project_id=pid, pr_number=None)
    await db.commit()

    mock_git = NoopGitOps()
    mock_git.push_branch = AsyncMock(return_value=True)
    mock_git.create_pr = AsyncMock(return_value=None)
    plugins.git_ops = mock_git
    plugins.dispatch = NoopDispatch()

    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(_make_app(db))

    assert mock_git.create_pr.await_count >= 1
    kwargs = mock_git.create_pr.await_args.kwargs
    assert kwargs["gh_repo"] == "org/other-repo"
    assert kwargs["base_branch"] == "develop"


# ---- #363 I5: one task's failure must not abandon the tick ----


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_a_failing_task_does_not_abandon_the_rest_of_the_tick(mock_sleep, db):
    """#363 I5. The whole sweep sat in one try/except, so one exception skipped
    every later stage — review, ci_check, stale sweeps, claim expiry — and did
    so again every tick until the cause cleared. Reproduced before the fix: a
    hung git call raised TimeoutError straight out of _run.
    """
    running_id = await repo.create_task(
        db,
        title="Running task that will blow up",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="running",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(db, running_id, job_id="job-boom")
    ci_id = await _make_ci_task(db)
    await db.commit()

    mock_dispatch = NoopDispatch()
    mock_dispatch.get_job = MagicMock(side_effect=RuntimeError("hung git call"))
    plugins.dispatch = mock_dispatch

    mock_git = NoopGitOps()
    mock_git.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.pending, "checks_running")
    )
    plugins.git_ops = mock_git

    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(_make_app(db))

    assert mock_git.check_pr_ci.await_count >= 1, (
        f"the ci_check stage must still run for task #{ci_id} after task "
        f"#{running_id} raised"
    )


# ---- #363 T2: a red PR must never be auto-merged ----


async def _approved_task_with_pr(db, *, pr_number=99):
    task_id = await _make_review_task(db)
    await repo.update_task(db, task_id, pr_number=pr_number, branch="task-x/b")
    await repo.add_task_update(db, task_id, "reviewer", "review", "LGTM\nAPPROVED")
    await db.commit()
    return task_id


def _completed_job_dispatch():
    d = NoopDispatch()
    d.get_job = MagicMock(return_value={"status": "completed", "exit_code": 0})
    return d


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_failed_ci_blocks_the_auto_merge(mock_sleep, db):
    """#363 T2, the highest-value gap in this task.

    Replacing the CI gate with `if True:` — auto-merging a RED pull request into
    the integration branch — survived all 1119 tests before this. The positive
    case had a test (#362); the refusal had none.
    """
    task_id = await _approved_task_with_pr(db)
    mock_git = NoopGitOps()
    mock_git.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.failed, "checks_failed")
    )
    mock_git.merge_pr = AsyncMock(return_value=True)
    plugins.git_ops = mock_git
    plugins.dispatch = _completed_job_dispatch()

    with (
        patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
        pytest.raises(_BreakLoop),
    ):
        await _poll_running_tasks(_make_app(db))

    mock_git.merge_pr.assert_not_awaited()
    updates = await repo.get_task_updates(db, task_id)
    assert any("CI failed" in u["content"] for u in updates)

    # An approved verdict is not delivery. Completing here would report a task
    # as done while its work sits unmerged in a branch — the one thing the
    # status exists to rule out.
    assert dict(await repo.get_task(db, task_id))["status"] == "needs_decision"
    events = await _events_for(db, task_id, "needs_decision")
    assert len(events) == 1
    assert "ci_failed" in str(events[0]["payload"])


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_pending_ci_also_blocks_the_auto_merge(mock_sleep, db):
    """Only a passing probe may merge. "Not yet answered" is not "passed"."""
    await _approved_task_with_pr(db)
    mock_git = NoopGitOps()
    mock_git.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.pending, "checks_running")
    )
    mock_git.merge_pr = AsyncMock(return_value=True)
    plugins.git_ops = mock_git
    plugins.dispatch = _completed_job_dispatch()

    with (
        patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
        pytest.raises(_BreakLoop),
    ):
        await _poll_running_tasks(_make_app(db))

    mock_git.merge_pr.assert_not_awaited()


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_passing_ci_merges_exactly_once(mock_sleep, db):
    await _approved_task_with_pr(db)
    mock_git = NoopGitOps()
    mock_git.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.passed, "checks_passed")
    )
    mock_git.merge_pr = AsyncMock(return_value=True)
    mock_git.pull_main = AsyncMock(return_value=True)
    mock_git.delete_branch = AsyncMock(return_value=None)
    plugins.git_ops = mock_git
    plugins.dispatch = _completed_job_dispatch()

    with (
        patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
        pytest.raises(_BreakLoop),
    ):
        await _poll_running_tasks(_make_app(db))

    assert mock_git.merge_pr.await_count == 1


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_the_recorded_merge_is_the_one_this_pr_produced(mock_sleep, db):
    """#534, third review round: the whitelist took the branch tip.

    The SHA came from `head_sha` of the base branch right after the merge, so a
    direct push that landed in between was written down as a pipeline merge —
    excusing the intruder, and leaving the real merge to be reported as drift.
    Exactly inverted. The pull request knows its own merge commit; ask it.
    """
    await _approved_task_with_pr(db)
    mock_git = NoopGitOps()
    mock_git.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.passed, "checks_passed")
    )
    mock_git.merge_pr = AsyncMock(return_value=True)
    # The merge produced M; a direct push then made D the tip of the base.
    mock_git.merge_commit_sha = AsyncMock(return_value="mmm111")
    mock_git.head_sha = AsyncMock(return_value="ddd222")
    mock_git.pull_main = AsyncMock(return_value=True)
    mock_git.delete_branch = AsyncMock(return_value=None)
    plugins.git_ops = mock_git
    plugins.dispatch = _completed_job_dispatch()

    with (
        patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
        pytest.raises(_BreakLoop),
    ):
        await _poll_running_tasks(_make_app(db))

    rows = [
        dict(r)
        for r in await db.execute_fetchall("SELECT merge_sha FROM pipeline_merges")
    ]
    recorded = {r["merge_sha"] for r in rows}
    assert recorded == {"mmm111"}, (
        f"the pipeline merge must be the PR's own commit, recorded {recorded}"
    )
    assert "ddd222" not in recorded, (
        "the tip of the base branch is whatever landed last — a direct push "
        "there would have been whitelisted"
    )


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_a_refused_merge_also_stops_completion(mock_sleep, db):
    """The second way a task used to reach `completed` unmerged.

    CI is green, but GitHub refuses the merge. merge_pr returned False and
    nobody read it: the completion branch ran regardless.
    """
    task_id = await _approved_task_with_pr(db)
    mock_git = NoopGitOps()
    mock_git.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.passed, "checks_passed")
    )
    mock_git.merge_pr = AsyncMock(return_value=False)  # merge refused by GitHub
    plugins.git_ops = mock_git
    plugins.dispatch = _completed_job_dispatch()

    with (
        patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
        pytest.raises(_BreakLoop),
    ):
        await _poll_running_tasks(_make_app(db))

    assert mock_git.merge_pr.await_count == 1
    assert dict(await repo.get_task(db, task_id))["status"] == "needs_decision"
    events = await _events_for(db, task_id, "needs_decision")
    assert len(events) == 1
    assert "merge_failed" in str(events[0]["payload"])


# ---- #363 T3: the CI-fix loop had no coverage at all ----


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_failed_ci_dispatches_a_fix_below_the_limit(mock_sleep, db):
    """#363 T3. Nothing in the suite mentioned ci_fix before this — removing the
    cycle limit entirely survived all 1119 tests, so a task could loop forever.
    """
    task_id = await _make_ci_task(db)
    await repo.update_task(db, task_id, ci_fix_cycle=0)
    await db.commit()

    mock_git = NoopGitOps()
    mock_git.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.failed, "checks_failed")
    )
    mock_git.get_ci_failure_logs = AsyncMock(return_value="pytest exploded")
    plugins.git_ops = mock_git
    plugins.dispatch = NoopDispatch()

    with (
        patch(
            "hub.poller.services.dispatch_ci_fix", new_callable=AsyncMock
        ) as dispatch_fix,
        pytest.raises(_BreakLoop),
    ):
        await _poll_running_tasks(_make_app(db))

    dispatch_fix.assert_awaited_once()
    assert dispatch_fix.await_args.args[2] == "pytest exploded", (
        "the fixing agent must receive the actual CI logs"
    )
    assert dict(await repo.get_task(db, task_id))["status"] != "needs_decision"


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_ci_fix_limit_escalates_instead_of_looping(mock_sleep, db):
    """At the limit the loop must stop and hand the task to a human."""
    task_id = await _make_ci_task(db)
    await repo.update_task(db, task_id, ci_fix_cycle=config.MAX_CI_FIX_CYCLES)
    await db.commit()

    mock_git = NoopGitOps()
    mock_git.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.failed, "checks_failed")
    )
    mock_git.get_ci_failure_logs = AsyncMock(return_value="still red")
    plugins.git_ops = mock_git
    plugins.dispatch = NoopDispatch()

    with (
        patch(
            "hub.poller.services.dispatch_ci_fix", new_callable=AsyncMock
        ) as dispatch_fix,
        patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock),
        pytest.raises(_BreakLoop),
    ):
        await _poll_running_tasks(_make_app(db))

    dispatch_fix.assert_not_awaited()
    assert dict(await repo.get_task(db, task_id))["status"] == "needs_decision"
    events = await _events_for(db, task_id, "needs_decision")
    assert len(events) == 1
    assert events[0]["payload"] and "ci_fix_cycle_limit" in str(events[0]["payload"])
    updates = await repo.get_task_updates(db, task_id)
    assert any("cycle limit reached" in u["content"] for u in updates)


# ---- Unrefined-draft watchdog (#751) ----


async def _make_draft(db, *, minutes=999, refined=False) -> int:
    task_id = await repo.create_task(
        db,
        title="Unrefined draft",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="proposer-bot",
        rationale="",
        status="draft",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    if refined:
        await repo.update_task(db, task_id, dor_passed=1, readiness_score=100)
    await db.execute(
        "UPDATE tasks SET created_at = datetime('now', ?) WHERE id=?",
        (f"-{minutes} minutes", task_id),
    )
    await db.commit()
    return task_id


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_stale_unrefined_draft_gets_one_alert(mock_sleep, db):
    # AC-1 (#751): one hub alert naming what is missing and how to fix it.
    task_id = await _make_draft(db)

    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(_make_app(db))

    alerts = await _stale_alerts(db, task_id)
    assert len(alerts) == 1
    assert "DoR не пройден" in alerts[0]
    assert "missing:" in alerts[0]
    assert "hub_refine_task" in alerts[0]
    assert dict(await repo.get_task(db, task_id))["status"] == "draft", (
        "the watchdog only speaks — it never transitions"
    )


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_unrefined_draft_alert_is_not_repeated(mock_sleep, db):
    # AC-2 (#751): a second pass does not duplicate the alert.
    task_id = await _make_draft(db)

    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(_make_app(db))
    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(_make_app(db))

    assert len(await _stale_alerts(db, task_id)) == 1


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_fresh_and_refined_drafts_are_left_alone(mock_sleep, db):
    # AC-3 (#751): younger than the threshold, or already DoR-passed —
    # no alert for either.
    fresh = await _make_draft(db, minutes=1)
    refined = await _make_draft(db, refined=True)

    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(_make_app(db))

    assert not await _stale_alerts(db, fresh)
    assert not await _stale_alerts(db, refined)


# ---- #929: a red base branch becomes an event, with the pair that met ----
#
# Measured in #921: the base was red for 574 of 738 minutes across two
# breakages, both caught by push-CI at the moment of the merge and followed
# by nothing. The second stood 8h25m and was found by a human busy with
# another task, while merges kept landing into it.


class _BranchCIGitOps(NoopGitOps):
    """Answers with a canned run history and commit range."""

    def __init__(self, runs, subjects=None):
        self._runs = runs
        self._subjects = subjects or []
        self.range_calls: list[tuple[str, str]] = []

    async def branch_ci_runs(self, branch, limit=20, repo=None, gh_repo=None):
        return self._runs

    async def release_range(self, base, head, repo=None, gh_repo=None):
        self.range_calls.append((base, head))
        return self._subjects


def _run(sha: str, conclusion: str, status: str = "completed") -> dict:
    return {
        "sha": sha,
        "status": status,
        "conclusion": conclusion,
        "created_at": "2026-08-21T18:53:33Z",
        "name": "CI",
    }


async def _default_project(db):
    row = await repo.get_project_by_slug(db, "default")
    if row is None:
        await repo.create_project(
            db, slug="default", name="default", workspace_path="/tmp/ws"
        )
        row = await repo.get_project_by_slug(db, "default")
    await db.commit()
    return row


async def test_red_base_branch_becomes_an_event(db):
    # AC-1: the hub notices, and the notice carries the branch and the commit.
    from hub.services import red_base

    project = await _default_project(db)
    plugins.git_ops = _BranchCIGitOps([_run("e60fef5c", "failure")])

    state = await red_base.check_project(db, project)

    assert state.status == red_base.RED
    events = [dict(r) for r in await repo.list_events(db, kinds=[red_base.EVENT_KIND])]
    assert len(events) == 1
    payload = events[0]["payload"]
    if isinstance(payload, str):
        import json

        payload = json.loads(payload)
    assert payload["sha"] == "e60fef5c"
    assert payload["branch"] == "develop"


async def test_event_names_the_commits_that_met(db):
    # AC-2: what met is the interval between the last green run and the first
    # red one — computed, never a guess at the culprit inside it.
    from hub.services import red_base

    project = await _default_project(db)
    ops = _BranchCIGitOps(
        [
            _run("e60fef5c", "failure"),
            _run("1ea5e120", "success"),
        ],
        subjects=["ci(task): mypy обязательным шагом (#847)"],
    )
    plugins.git_ops = ops

    state = await red_base.check_project(db, project)

    assert state.last_green_sha == "1ea5e120"
    assert ops.range_calls == [("1ea5e120", "e60fef5c")]
    assert state.met == ["ci(task): mypy обязательным шагом (#847)"]


async def test_red_base_is_announced_once_per_breakage(db):
    # AC-3: while the base stays red the event is not repeated — a line per
    # poll cycle is how a real signal gets muted (#534). A NEW breakage, with
    # a new sha, is announced again.
    from hub.services import red_base

    project = await _default_project(db)
    plugins.git_ops = _BranchCIGitOps([_run("e60fef5c", "failure")])

    await red_base.check_project(db, project)
    await red_base.check_project(db, project)
    await red_base.check_project(db, project)

    events = await repo.list_events(db, kinds=[red_base.EVENT_KIND])
    assert len(events) == 1, "one signal per breakage, not per cycle"

    plugins.git_ops = _BranchCIGitOps(
        [_run("0c69ba10", "failure"), _run("fe8759dd", "success")]
    )
    await red_base.check_project(db, project)

    assert len(await repo.list_events(db, kinds=[red_base.EVENT_KIND])) == 2


async def test_unreadable_ci_outcome_is_not_a_green_base(db):
    # AC-4: "could not look" is its own answer with a reason. Reporting health
    # while blind is the failure this codebase keeps re-learning (#725).
    from hub.services import red_base

    project = await _default_project(db)
    plugins.git_ops = _BranchCIGitOps(None)

    state = await red_base.check_project(db, project)

    assert state.status == red_base.UNKNOWN
    assert state.reason
    assert not await repo.list_events(db, kinds=[red_base.EVENT_KIND])

    # An unrecognised conclusion is not green either.
    assert red_base.read_state("develop", [_run("abc", "")]).status == red_base.UNKNOWN
    # Nor is a run still in flight.
    assert (
        red_base.read_state("develop", [_run("abc", "", status="in_progress")]).status
        == red_base.UNKNOWN
    )


# ---------------------------------------------------------------------------
# Характеризующие тесты перед разрезом _poll_running_tasks (#850).
#
# Функция на 966 строк переживает рефакторинг только при одном условии: есть с
# чем сравнить результат. Ветки ниже до этого файла не исполнялись ни разу —
# то есть разрез мог потерять любую из них молча. Тесты написаны ДО правки
# продакшен-кода и намеренно проверяют СЕГОДНЯШНЕЕ поведение, каким бы оно ни
# было, а не желаемое.
# ---------------------------------------------------------------------------


async def _running_task(db, **fields) -> int:
    task_id = await repo.create_task(
        db,
        title=fields.pop("title", "Задача"),
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
    await repo.update_task(db, task_id, job_id=fields.pop("job_id", "job-1"), **fields)
    await db.commit()
    return task_id


def _dispatch_with(job: dict | None, log_text: str = "") -> NoopDispatch:
    d = NoopDispatch()
    d.get_job = MagicMock(return_value=job)
    d.job_log_full = MagicMock(return_value=log_text)
    return d


async def test_failed_job_marks_the_task_failed_with_its_exit_code(db):
    """Провал агента доезжает до задачи вместе с кодом и текстом.

    Без этого задача осталась бы в running навсегда: джоб мёртв, а хаб про это
    не знает.
    """
    task_id = await _running_task(db)
    plugins.dispatch = _dispatch_with(
        {"status": "failed", "exit_code": 137, "result_text": "убит по памяти"}
    )

    with patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock):
        await _run_poll_once(db)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "failed"
    assert task["exit_code"] == 137
    assert task["result_text"] == "убит по памяти"


async def test_reported_blocker_sends_the_task_to_a_human(db):
    """Блокер — это просьба о решении, а не провал.

    Разница видна в статусе: needs_decision зовёт человека, failed просто
    закрывает попытку.
    """
    task_id = await _running_task(db)
    await repo.add_task_update(db, task_id, "agent", "blocker", "нужен доступ к прод")
    await db.commit()
    plugins.dispatch = _dispatch_with({"status": "completed", "exit_code": 0})

    with patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock):
        await _run_poll_once(db)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision"
    events = [dict(e) for e in await repo.list_events(db, since=0)]
    assert any(
        e["kind"] == "needs_decision" and e["task_id"] == task_id for e in events
    ), "переход к человеку должен быть виден в ленте, а не только в статусе"


async def test_agent_summary_is_recovered_from_the_dispatch_log(db):
    """Агент завершился, но отчёт в хаб не записал.

    Хаб достаёт сводку из лога джоба и заводит done сам — иначе задача с
    выполненной работой уходит в needs_decision как «отчёта нет».
    """
    task_id = await _running_task(db)
    plugins.dispatch = _dispatch_with(
        {"status": "completed", "exit_code": 0},
        # Формат лога джоба: JSON, РАЗЛОЖЕННЫЙ ПО СТРОКАМ. Разбор идёт построчно
        # и берёт то, что стоит после первого двоеточия, поэтому однострочный
        # {"text": "..."} он не понимает — закрывающая скобка ломает json.loads.
        # Сводкой считается только текст длиннее 30 символов, чтобы служебные
        # реплики не стали отчётом.
        log_text='{\n  "text": "Сделал что просили: поправил поллер и прогнал тесты"\n}',
    )

    with patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock):
        await _run_poll_once(db)

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    done = [u for u in updates if u["kind"] == "done"]
    assert done, "сводка из лога должна стать done-записью"
    assert "поправил поллер" in done[0]["content"]


async def test_a_job_that_came_back_clears_the_missing_mark(db):
    """Джоб пропал и вернулся: отметка о пропаже снимается.

    Иначе задача остаётся помеченной как потерянная и попадёт под эскалацию,
    хотя работа идёт.
    """
    task_id = await _running_task(db)
    await repo.mark_job_missing(db, task_id)
    await db.commit()
    assert dict(await repo.get_task(db, task_id))["job_missing_since"]

    plugins.dispatch = _dispatch_with({"status": "running"})

    await _run_poll_once(db)

    task = dict(await repo.get_task(db, task_id))
    assert not task["job_missing_since"], "вернувшийся джоб снимает отметку"
    assert task["status"] == "running", "и статус при этом не трогается"


async def test_a_job_still_running_is_left_alone(db):
    """Незавершённый джоб не двигает задачу никуда."""
    task_id = await _running_task(db)
    plugins.dispatch = _dispatch_with({"status": "running"})

    await _run_poll_once(db)

    assert dict(await repo.get_task(db, task_id))["status"] == "running"


# ---- Адресные тесты на свипы после разреза (#850) ----
#
# Главная выгода разреза видна прямо здесь: свип зовётся напрямую, без запуска
# цикла, без _sleep_once и без прохода всех остальных свипов. До разреза каждый
# из этих тестов был бы «прогнать весь поллер и молиться, что сработала нужная
# из двенадцати веток».

from hub.poller import (  # noqa: E402 - тесты дописаны после разреза
    _extract_review_from_log,
    _record_merge_and_tidy,
    _request_review_fixes,
    _seconds_since,
    _sweep_autopilot_digests,
    _sweep_delivery_discrepancies,
    _sweep_events_retention,
    _sweep_mcp_retention,
    _sweep_messages_retention,
    _sweep_review,
    _sweep_review_dispatches,
    _sweep_sessions_retention,
)


async def test_review_job_without_a_verdict_calls_a_human(db):
    """Дочитанный ревью-джоб без вердикта — не успех и не провал, а вопрос."""
    task_id = await _make_review_task(db)
    plugins.dispatch = _dispatch_with({"status": "completed", "exit_code": 0})

    with patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock):
        await _sweep_review(db)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert any("no clear verdict" in (u["content"] or "") for u in updates)


async def test_a_review_job_that_came_back_clears_the_missing_mark(db):
    """Джоб нашёлся после отметки о пропаже — отметка снимается, паника отменяется."""
    task_id = await _make_review_task(db)
    await repo.mark_job_missing(db, task_id)
    await db.commit()
    plugins.dispatch = _dispatch_with({"status": "running"})

    await _sweep_review(db)

    task = dict(await repo.get_task(db, task_id))
    assert not task.get("job_missing_since")
    assert task["status"] == "review", "живой джоб дорабатывает, задачу не трогаем"


async def test_review_fix_text_is_recovered_from_the_job_log(db):
    """Замечания ревьюера достаются из лога джоба, когда апдейта с ними нет."""
    task_id = await _make_review_task(db)
    task = dict(await repo.get_task(db, task_id))
    long_note = "Замените голый except на конкретное исключение в poller.py"
    plugins.dispatch = _dispatch_with(
        {"status": "completed"}, log_text=f'"text": "{long_note}",'
    )
    plugins.git_ops = NoopGitOps()
    plugins.git_ops.checkout = AsyncMock(return_value=True)

    with patch(
        "hub.poller.services.dispatch_fix", new_callable=AsyncMock
    ) as dispatch_fix:
        await _request_review_fixes(db, task, updates_list=[])

    assert long_note in dispatch_fix.await_args.args[2]


async def test_exhausted_review_budget_dispatches_the_arbiter(db):
    """Когда лимит кругов ревью выбран, следующий шаг — арбитр, а не ещё круг."""
    task_id = await _make_review_task(db)
    await repo.update_task(db, task_id, review_cycle=99)
    await db.commit()
    task = dict(await repo.get_task(db, task_id))
    plugins.dispatch = _dispatch_with({"status": "completed"}, log_text="")

    with patch(
        "hub.poller.services.dispatch_arbiter", new_callable=AsyncMock
    ) as arbiter:
        await _request_review_fixes(db, task, updates_list=[])

    arbiter.assert_awaited()
    task = dict(await repo.get_task(db, task_id))
    assert task["review_cycle"] == 100, "круг посчитан до передачи арбитру"


async def test_an_unreadable_merge_commit_does_not_lose_the_merge_record(db):
    """merge_commit_sha упал — мерж всё равно записан, пусть и без sha (#534)."""
    task_id = await _make_review_task(db)
    task = dict(await repo.get_task(db, task_id))
    git = NoopGitOps()
    git.merge_commit_sha = AsyncMock(side_effect=RuntimeError("gh молчит"))
    git.pull_main = AsyncMock(return_value=True)
    git.delete_branch = AsyncMock(return_value=True)
    plugins.git_ops = git

    await _record_merge_and_tidy(db, task, 77, {"repo": None, "gh_repo": None})

    assert await repo.pipeline_merge_recorded(db, task_id, 77), (
        "запись о мерже не должна зависеть от читаемости его sha"
    )


async def test_untidy_workspace_is_reported_not_fatal(db):
    """Workspace не вернулся на базу после мержа — алерт, а не падение задачи (#552)."""
    from hub.integrations.git_ops import WorkspaceNotReadyError

    task_id = await _make_review_task(db)
    await repo.update_task(db, task_id, branch="task-x/y")
    await db.commit()
    task = dict(await repo.get_task(db, task_id))
    git = NoopGitOps()
    git.merge_commit_sha = AsyncMock(return_value="abc123")
    git.pull_main = AsyncMock(side_effect=WorkspaceNotReadyError("клон занят"))
    plugins.git_ops = git

    await _record_merge_and_tidy(db, task, 78, {"repo": None, "gh_repo": None})

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert any("не возвращён" in (u["content"] or "") for u in updates)


async def test_delivery_scan_findings_reach_the_log(db, caplog):
    """Сработавший delivery-скан называет задачи с открытыми PR, а не молчит."""
    with (
        patch("hub.poller._due_for_delivery_scan", return_value=True),
        patch(
            "hub.services.delivery_state.scan_completed_deliveries",
            new_callable=AsyncMock,
            return_value=[{"task_id": 42}],
        ),
    ):
        await _sweep_delivery_discrepancies(db)

    assert any("#42" in r.getMessage() for r in caplog.records)


async def test_background_sweep_failures_do_not_kill_the_pass(db):
    """Дайджесты и review-dispatch падают — свип переживает и идёт дальше."""
    with patch(
        "hub.services.digest.generate_due_digests",
        new_callable=AsyncMock,
        side_effect=RuntimeError("digest упал"),
    ):
        await _sweep_autopilot_digests(db)
    with patch(
        "hub.services.review_dispatch.sweep_review_dispatches",
        new_callable=AsyncMock,
        side_effect=RuntimeError("dispatch упал"),
    ):
        await _sweep_review_dispatches(db)


async def test_retention_sweeps_commit_only_when_something_was_pruned(db):
    """Каждая чистка коммитит и пишет в лог только когда реально что-то удалила."""
    with (
        patch("hub.poller.repo.prune_events", new_callable=AsyncMock, return_value=3),
        patch(
            "hub.poller.repo.prune_agent_sessions",
            new_callable=AsyncMock,
            return_value=2,
        ),
        patch(
            "hub.poller.repo.prune_agent_messages",
            new_callable=AsyncMock,
            return_value=1,
        ),
        patch(
            "hub.poller.repo.prune_mcp_call_events",
            new_callable=AsyncMock,
            return_value=4,
        ),
    ):
        await _sweep_events_retention(db)
        await _sweep_sessions_retention(db)
        await _sweep_messages_retention(db)
        await _sweep_mcp_retention(db)


def test_review_text_survives_json_log_format():
    log_text = '"text": "Короткая",\n"text": "Достаточно длинное замечание ревьюера",'
    assert "Достаточно длинное" in _extract_review_from_log(log_text)
    assert _extract_review_from_log('"text": не-json') == ""


def test_seconds_since_rejects_garbage_timestamps():
    assert _seconds_since("не дата") is None
    assert _seconds_since("") is None
    assert _seconds_since("2026-01-01 00:00:00") > 0


# --- Истёкший claim отпускает общий клон (#966) ------------------------------


@pytest.mark.asyncio
async def test_expired_claim_restores_workspace_base(db):
    """AC-4: авто-релиз claim возвращает legacy-клон на базу; ошибка не роняет sweep."""
    from unittest.mock import AsyncMock, patch

    from hub.poller import _sweep_expired_claims

    for title in ("первая", "вторая"):
        await db.execute(
            "INSERT INTO tasks (title, description, status, claimed_by, "
            "claim_session_id, claimed_at) VALUES (?, '', 'claimed', 'bot', 's1', "
            "datetime('now', '-999 minutes'))",
            (title,),
        )
    await db.commit()

    restore = AsyncMock(side_effect=[RuntimeError("git down"), None])
    with patch("hub.services.orchestration.restore_pair_workspace_base", restore):
        await _sweep_expired_claims(db)

    # Обе задачи вернулись в open, хотя первый restore упал.
    cursor = await db.execute("SELECT status FROM tasks")
    assert [r["status"] for r in await cursor.fetchall()] == ["open", "open"]
    assert restore.await_count == 2
