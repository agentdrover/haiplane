from __future__ import annotations

import aiosqlite
import pytest
from fastapi import HTTPException

from hub import repository as repo
from hub import services
from hub.models import (
    TaskAnswer,
    TaskApprove,
    TaskCreate,
    TaskDecide,
    TaskPairStart,
    TaskQuestion,
    TaskReorder,
    TaskStart,
    TaskUpdateCreate,
)


async def test_create_task(db: aiosqlite.Connection):
    body = TaskCreate(title="Service test", description="desc")
    tv = await services.create_task(db, body)
    assert tv.id > 0
    assert tv.title == "Service test"
    assert tv.status.value == "open"


async def test_create_epic(db: aiosqlite.Connection):
    body = TaskCreate(title="Big epic", task_type="epic")
    tv = await services.create_task(db, body)
    assert tv.task_type.value == "epic"
    assert tv.status.value == "open"


async def test_create_agent_task_is_draft(db: aiosqlite.Connection):
    body = TaskCreate(title="Agent idea", source="agent", agent="bot")
    tv = await services.create_task(db, body)
    assert tv.status.value == "draft"
    assert tv.source.value == "agent"


async def test_approve_task(db: aiosqlite.Connection):
    body = TaskCreate(title="Draft task", source="agent", agent="bot")
    tv = await services.create_task(db, body)
    assert tv.status.value == "draft"

    # force=True bypasses the DoR gate so this test keeps focus on
    # lifecycle mechanics; gate behavior is covered in test_api_approve_gate.
    approved = await services.approve_task(db, tv.id, TaskApprove(force=True))
    assert approved.status.value == "open"


async def test_approve_with_comment(db: aiosqlite.Connection):
    body = TaskCreate(title="With comment", source="agent")
    tv = await services.create_task(db, body)

    # Force-approvals record the comment inside the 'Approve override' alert
    # instead of a separate 'Approved: ...' status update, so assert on the
    # comment substring rather than the 'Approved' prefix.
    approve_body = TaskApprove(comment="looks good", force=True)
    approved = await services.approve_task(db, tv.id, approve_body)
    assert approved.status.value == "open"
    assert approved.updates
    assert any("looks good" in u.content for u in approved.updates)


async def test_reject_task(db: aiosqlite.Connection):
    body = TaskCreate(title="To reject", source="agent")
    tv = await services.create_task(db, body)

    rejected = await services.reject_task(db, tv.id)
    assert rejected.status.value == "rejected"


async def test_reject_non_draft_fails(db: aiosqlite.Connection):
    body = TaskCreate(title="Open task")
    tv = await services.create_task(db, body)
    assert tv.status.value == "open"

    with pytest.raises(HTTPException) as exc_info:
        await services.reject_task(db, tv.id)
    assert exc_info.value.status_code == 400
    assert "draft" in str(exc_info.value.detail)


async def test_approve_non_draft_fails(db: aiosqlite.Connection):
    body = TaskCreate(title="Open task")
    tv = await services.create_task(db, body)

    with pytest.raises(HTTPException) as exc_info:
        await services.approve_task(db, tv.id)
    assert exc_info.value.status_code == 400


async def test_start_task_requires_plan(db: aiosqlite.Connection):
    body = TaskCreate(title="No plan task")
    tv = await services.create_task(db, body)
    assert tv.status.value == "open"

    with pytest.raises(HTTPException) as exc_info:
        await services.start_task(db, tv.id)
    assert exc_info.value.status_code == 400
    assert "Plan required" in str(exc_info.value.detail)


async def test_start_task_with_plan(db: aiosqlite.Connection):
    body = TaskCreate(title="With plan")
    tv = await services.create_task(db, body)

    start_body = TaskStart(plan="Plan: implement feature X")
    started = await services.start_task(db, tv.id, start_body)
    assert started.status.value == "running"


async def test_start_task_with_prior_plan_update(db: aiosqlite.Connection):
    body = TaskCreate(title="Plan via update")
    tv = await services.create_task(db, body)

    await repo.add_task_update(
        db, tv.id, "dev", "status", "Plan: step-by-step approach"
    )
    await db.commit()

    started = await services.start_task(db, tv.id)
    assert started.status.value == "running"


async def test_pair_start_requires_plan(db: aiosqlite.Connection):
    body = TaskCreate(title="Pair no plan")
    tv = await services.create_task(db, body)

    with pytest.raises(HTTPException) as exc_info:
        await services.pair_start_task(db, tv.id)
    assert exc_info.value.status_code == 400
    assert "Plan required" in str(exc_info.value.detail)


async def test_pair_start_without_dispatch(db: aiosqlite.Connection):
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins

    submit_mock = AsyncMock(return_value={"job_id": "must-not-run"})
    plugins.dispatch.submit_task = submit_mock

    body = TaskCreate(title="Pair task")
    tv = await services.create_task(db, body)
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: pair in Cursor")
    await db.commit()

    started = await services.pair_start_task(
        db,
        tv.id,
        TaskPairStart(assigned_agent="composer-analyst"),
        caller="denis",
    )

    assert started.status.value == "running"
    assert started.job_id is None
    assert started.branch == f"task-{tv.id}/test"
    assert started.assigned_agent == "composer-analyst"
    submit_mock.assert_not_called()


async def test_pair_start_uses_caller_when_agent_unset(db: aiosqlite.Connection):
    body = TaskCreate(title="Pair caller")
    tv = await services.create_task(db, body)
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: work locally")
    await db.commit()

    started = await services.pair_start_task(db, tv.id, caller="cursor-dev")

    assert started.status.value == "running"
    assert started.assigned_agent == "cursor-dev"


async def test_pair_done_from_running_completes_without_auto_review(
    db: aiosqlite.Connection,
):
    task_id = await repo.create_task(
        db,
        title="Pair done",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="running",
        auto_review=False,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(db, task_id, branch="task-99/test")
    await db.commit()

    await services.add_update(
        db,
        task_id,
        TaskUpdateCreate(agent="dev", kind="done", content="Shipped docs"),
    )
    row = await repo.get_task(db, task_id)
    assert row["status"] == "completed"


async def test_pair_done_from_running_enters_ci_check_with_auto_review(
    db: aiosqlite.Connection,
):
    task_id = await repo.create_task(
        db,
        title="Pair CI",
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
    await repo.update_task(db, task_id, branch="task-100/test")
    await db.commit()

    await services.add_update(
        db,
        task_id,
        TaskUpdateCreate(agent="dev", kind="done", content="Ready for CI"),
    )
    row = await repo.get_task(db, task_id)
    assert row["status"] == "ci_check"


async def test_done_from_open_without_pair_start_keeps_open(db: aiosqlite.Connection):
    body = TaskCreate(title="Open pair legacy")
    tv = await services.create_task(db, body)
    await services.add_update(
        db,
        tv.id,
        TaskUpdateCreate(agent="dev", kind="done", content="Report without start"),
    )
    row = await repo.get_task(db, tv.id)
    assert row["status"] == "open"


async def test_start_task_dispatch_failure_keeps_task_recoverable(
    db: aiosqlite.Connection,
):
    from hub.integrations.noop import NoopDispatch, NoopGitOps
    from hub.integrations.registry import plugins

    plugins.dispatch = NoopDispatch()
    plugins.git_ops = NoopGitOps()

    body = TaskCreate(title="Unavailable dispatch")
    tv = await services.create_task(db, body)
    await repo.add_task_update(db, tv.id, "human", "status", "Plan: hand off to dev")
    await db.commit()

    started = await services.start_task(db, tv.id)

    assert started.status.value == "open"
    assert started.result_text
    assert "dispatch plugin not configured" in started.result_text
    assert started.updates
    assert any(
        "dispatch plugin not configured" in update.content for update in started.updates
    )


async def test_ask_question(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Running task",
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
    await db.commit()

    q_body = TaskQuestion(agent="dev", question="Need clarification on X")
    tv = await services.ask_question(db, task_id, q_body)
    assert tv.status.value == "needs_info"
    assert tv.updates
    assert any(u.kind == "question" for u in tv.updates)


async def test_ask_question_from_open_pair(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Open pair task",
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
    await db.commit()

    q_body = TaskQuestion(agent="dev", question="Clarify scope before pair-start?")
    tv = await services.ask_question(db, task_id, q_body)
    assert tv.status.value == "needs_info"
    assert any(u.kind == "question" for u in tv.updates)


async def test_ask_question_running_with_job_id_regression(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Headless running",
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
    await repo.update_task(db, task_id, job_id="job-123")
    await db.commit()

    q_body = TaskQuestion(agent="dev", question="Need clarification on X")
    tv = await services.ask_question(db, task_id, q_body)
    assert tv.status.value == "needs_info"


async def test_ask_question_on_non_running_fails(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Open headless with job",
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
    await repo.update_task(db, task_id, job_id="job-456")
    await db.commit()

    q_body = TaskQuestion(agent="dev", question="?")
    with pytest.raises(HTTPException) as exc_info:
        await services.ask_question(db, task_id, q_body)
    assert exc_info.value.status_code == 400


async def test_answer_question_no_resume(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Info task",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="needs_info",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    a_body = TaskAnswer(answer="Here is the answer", resume=False)
    tv = await services.answer_question(db, task_id, a_body)
    assert tv.status.value == "open"


async def test_answer_question_with_resume_headless(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Resume task",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="needs_info",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(db, task_id, job_id="headless-job-1")
    await db.commit()

    a_body = TaskAnswer(answer="Here is the answer", resume=True)
    tv = await services.answer_question(db, task_id, a_body)
    assert tv.status.value == "running"


async def test_answer_question_pair_resume_to_open_without_dispatch(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
):
    dispatch_called = False

    async def _fake_dispatch(*_args, **_kwargs):
        nonlocal dispatch_called
        dispatch_called = True

    monkeypatch.setattr(services, "dispatch_task", _fake_dispatch)

    task_id = await repo.create_task(
        db,
        title="Pair pre-start",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="needs_info",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    tv = await services.answer_question(
        db, task_id, TaskAnswer(answer="Clarified", resume=True)
    )
    assert tv.status.value == "open"
    assert tv.job_id is None
    assert not dispatch_called


async def test_answer_question_pair_resume_to_running_without_dispatch(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
):
    dispatch_called = False

    async def _fake_dispatch(*_args, **_kwargs):
        nonlocal dispatch_called
        dispatch_called = True

    monkeypatch.setattr(services, "dispatch_task", _fake_dispatch)

    task_id = await repo.create_task(
        db,
        title="Pair running",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="needs_info",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(db, task_id, branch="task-99/pair-slug")
    await db.commit()

    tv = await services.answer_question(
        db, task_id, TaskAnswer(answer="Continue", resume=True)
    )
    assert tv.status.value == "running"
    assert tv.job_id is None
    assert not dispatch_called


async def test_answer_non_needs_info_fails(db: aiosqlite.Connection):
    body = TaskCreate(title="Open task")
    tv = await services.create_task(db, body)

    a_body = TaskAnswer(answer="answer")
    with pytest.raises(HTTPException) as exc_info:
        await services.answer_question(db, tv.id, a_body)
    assert exc_info.value.status_code == 400


async def test_add_update(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Update task",
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
    await db.commit()

    body = TaskUpdateCreate(agent="dev", kind="status", content="Progress update")
    uv = await services.add_update(db, task_id, body)
    assert uv.kind == "status"
    assert uv.content == "Progress update"


async def test_add_done_update_completes_pending_report(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Pending report",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="pending_report",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    body = TaskUpdateCreate(agent="dev", kind="done", content="Report: all done")
    await services.add_update(db, task_id, body)

    row = await repo.get_task(db, task_id)
    assert dict(row)["status"] == "completed"


async def test_lifecycle_approve_from_running_fails(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Running",
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
    await db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await services.approve_task(db, task_id)
    assert exc_info.value.status_code == 400


async def test_lifecycle_reject_from_running_fails(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Running",
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
    await db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await services.reject_task(db, task_id)
    assert exc_info.value.status_code == 400


async def test_approve_nonexistent_task(db: aiosqlite.Connection):
    with pytest.raises(HTTPException) as exc_info:
        await services.approve_task(db, 99999)
    assert exc_info.value.status_code == 404


async def test_list_tasks_service(db: aiosqlite.Connection):
    await repo.create_task(
        db,
        title="S1",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.create_task(
        db,
        title="S2",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="running",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="high",
    )
    await db.commit()

    tasks = await services.list_tasks(db, status="open")
    assert len(tasks) == 1
    assert tasks[0].title == "S1"

    all_tasks = await services.list_tasks(db)
    assert len(all_tasks) == 2


async def test_row_to_task_conversion(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Conversion test",
        description="desc",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="why",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="high",
    )
    await db.commit()

    row = await repo.get_task(db, task_id)
    tv = services.row_to_task(row)
    assert tv.title == "Conversion test"
    assert tv.priority.value == "high"
    assert tv.source.value == "human"


async def test_force_complete_task(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="PR done",
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
    await repo.update_task(db, task_id, status="pending_report")
    await db.commit()

    tv = await services.force_complete_task(db, task_id)
    assert tv.status.value == "completed"
    assert tv.updates
    assert any(
        update.kind == "done" and "Force-completed by human" in update.content
        for update in tv.updates
    )


async def test_force_complete_wrong_status(db: aiosqlite.Connection):
    body = TaskCreate(title="Still open")
    tv = await services.create_task(db, body)
    assert tv.status.value == "open"

    with pytest.raises(HTTPException) as exc_info:
        await services.force_complete_task(db, tv.id)
    assert exc_info.value.status_code == 400
    assert "pending_report" in str(exc_info.value.detail)


async def test_reorder_task(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Reorderable",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    reorder_body = TaskReorder(position=5)
    tv = await services.reorder_task(db, task_id, reorder_body)
    assert tv.position == 5


async def test_add_update_done_pending_report(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Awaiting report",
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
    await repo.update_task(db, task_id, status="pending_report")
    await db.commit()

    body = TaskUpdateCreate(agent="dev", kind="done", content="Final report")
    await services.add_update(db, task_id, body)

    row = await repo.get_task(db, task_id)
    assert dict(row)["status"] == "completed"


async def test_add_update_nonexistent_task(db: aiosqlite.Connection):
    body = TaskUpdateCreate(agent="dev", kind="status", content="Hello")
    with pytest.raises(HTTPException) as exc_info:
        await services.add_update(db, 99999, body)
    assert exc_info.value.status_code == 404


async def test_refresh_task_no_job(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="No job",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    tv = await services.refresh_task(db, task_id)
    assert tv.status.value == "open"
    assert tv.job_id is None


async def test_scan_text_for_verdict_approved():
    assert services.scan_text_for_verdict("Review: APPROVED") == "approved"


async def test_scan_text_for_verdict_changes():
    assert (
        services.scan_text_for_verdict("changes_requested — needs rework")
        == "changes_requested"
    )


async def test_scan_text_for_verdict_empty():
    assert services.scan_text_for_verdict("") is None


async def test_get_dashboard_data(db: aiosqlite.Connection):
    await repo.create_task(
        db,
        title="Active one",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    dd = await services.get_dashboard_data(db)

    from hub.models import DashboardData

    assert isinstance(dd, DashboardData)
    assert len(dd.active_tasks) >= 1


async def test_get_inbox_data(db: aiosqlite.Connection):
    await repo.create_task(
        db,
        title="Draft item",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="bot",
        rationale="",
        status="draft",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.create_task(
        db,
        title="Question item",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="needs_info",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    inbox = await services.get_inbox_data(db)
    assert len(inbox["drafts"]) >= 1
    assert len(inbox["questions"]) >= 1
    assert "decisions" in inbox
    assert "pending_reports" in inbox


async def test_list_tasks_with_filters(db: aiosqlite.Connection):
    await repo.create_task(
        db,
        title="Epic one",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=False,
        task_type="epic",
        parent_id=None,
        priority="high",
    )
    await repo.create_task(
        db,
        title="Task one",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.create_task(
        db,
        title="Task running",
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
    await db.commit()

    epics = await services.list_tasks(db, task_type="epic")
    assert len(epics) == 1
    assert epics[0].title == "Epic one"

    open_tasks = await services.list_tasks(db, status="open", task_type="task")
    assert len(open_tasks) == 1
    assert open_tasks[0].title == "Task one"


async def test_decide_task_accept(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Needs decision",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="needs_decision",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    body = TaskDecide(action="accept")
    tv = await services.decide_task(db, task_id, body)
    assert tv.status.value == "completed"
    assert tv.updates
    assert any(u.kind == "decision" for u in tv.updates)


async def test_decide_task_accept_with_summary(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Decision with summary",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="needs_decision",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    body = TaskDecide(
        action="accept",
        decision_summary="Code quality is acceptable after review.",
    )
    tv = await services.decide_task(db, task_id, body)
    assert tv.status.value == "completed"
    assert tv.updates
    decision_updates = [u for u in tv.updates if u.kind == "decision"]
    assert len(decision_updates) == 1
    assert "Code quality is acceptable" in decision_updates[0].content


async def test_decide_task_rework_with_summary(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Rework decision",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="needs_decision",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    body = TaskDecide(
        action="rework",
        instructions="Fix the edge case in auth module.",
        decision_summary="Edge case not handled for expired tokens.",
    )
    tv = await services.decide_task(db, task_id, body)
    assert tv.status.value in ("fix_requested", "open")
    decision_updates = [u for u in tv.updates if u.kind == "decision"]
    assert len(decision_updates) == 1
    assert "Edge case not handled" in decision_updates[0].content
    assert "Fix the edge case" in decision_updates[0].content


async def test_decide_task_record_decision_noop_does_not_break(
    db: aiosqlite.Connection,
):
    """record_decision=True with noop notes adapter must not fail."""
    task_id = await repo.create_task(
        db,
        title="Record noop",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="needs_decision",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    body = TaskDecide(
        action="accept",
        decision_summary="Accepted despite review noise.",
        record_decision=True,
    )
    tv = await services.decide_task(db, task_id, body)
    assert tv.status.value == "completed"


async def test_decide_task_wrong_status(db: aiosqlite.Connection):
    body = TaskCreate(title="Open task")
    tv = await services.create_task(db, body)
    assert tv.status.value == "open"

    with pytest.raises(HTTPException) as exc_info:
        await services.decide_task(db, tv.id, TaskDecide(action="accept"))
    assert exc_info.value.status_code == 400
    assert "needs_decision" in str(exc_info.value.detail)


async def test_noop_plugins_start_clean(db: aiosqlite.Connection):
    """Verify Hub works with pure noop plugins (no integrations configured)."""
    from hub.integrations.noop import NoopDispatch, NoopGitOps
    from hub.integrations.registry import plugins

    plugins.dispatch = NoopDispatch()
    plugins.git_ops = NoopGitOps()

    body = TaskCreate(title="Noop test", description="created with noop plugins")
    tv = await services.create_task(db, body)
    assert tv.id > 0
    assert tv.status.value == "open"

    inbox = await services.get_inbox_data(db)
    assert isinstance(inbox, dict)
