from __future__ import annotations

import json

import aiosqlite
import pytest
from fastapi import HTTPException

from hub import repository as repo
from hub import services
from hub.models import (
    BulkChildTaskItem,
    BulkChildTasksCreate,
    ReviewVerdict,
    TaskAnswer,
    TaskApprove,
    TaskClaim,
    TaskCreate,
    TaskDecide,
    TaskPairStart,
    TaskPriority,
    TaskQuestion,
    TaskRelease,
    TaskReorder,
    TaskReviewVerdict,
    TaskSource,
    TaskStart,
    TaskSubmitReview,
    TaskType,
    TaskUpdateCreate,
)


async def test_create_task(db: aiosqlite.Connection):
    body = TaskCreate(title="Service test", description="desc")
    tv = await services.create_task(db, body)
    assert tv.id > 0
    assert tv.title == "Service test"
    assert tv.status.value == "open"


async def test_create_subtasks_bulk(db: aiosqlite.Connection):
    parent = TaskCreate(title="Parent", source=TaskSource.human)
    parent_view = await services.create_task(db, parent)
    body = BulkChildTasksCreate(
        items=[
            BulkChildTaskItem(title="Child 1"),
            BulkChildTaskItem(title="Child 2", priority=TaskPriority.high),
        ],
        source=TaskSource.agent,
        agent="bot",
    )
    created = await services.create_subtasks_bulk(db, parent_view.id, body)
    assert len(created) == 2
    assert all(t.task_type.value == "subtask" for t in created)
    assert all(t.status.value == "draft" for t in created)
    assert created[1].priority.value == "high"


async def test_create_subtasks_bulk_invalid_hierarchy(db: aiosqlite.Connection):
    parent = TaskCreate(title="Parent", source=TaskSource.human)
    parent_view = await services.create_task(db, parent)
    sub = TaskCreate(
        title="Sub",
        task_type=TaskType.subtask,
        parent_id=parent_view.id,
        source=TaskSource.human,
    )
    sub_view = await services.create_task(db, sub)
    body = BulkChildTasksCreate(items=[BulkChildTaskItem(title="Nope")])
    with pytest.raises(HTTPException) as exc:
        await services.create_subtasks_bulk(db, sub_view.id, body)
    assert exc.value.status_code == 400


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


async def test_create_task_invalid_hierarchy_actionable(db: aiosqlite.Connection):
    epic = await services.create_task(db, TaskCreate(title="E", task_type="epic"))
    feature = await services.create_task(
        db,
        TaskCreate(title="F", task_type="feature", parent_id=epic.id),
    )
    task_parent = await services.create_task(
        db,
        TaskCreate(title="T", task_type="task", parent_id=feature.id),
    )
    with pytest.raises(HTTPException) as exc_info:
        await services.create_task(
            db,
            TaskCreate(
                title="Bad",
                task_type="task",
                parent_id=task_parent.id,
            ),
        )
    detail = exc_info.value.detail
    assert detail["reason"] == "invalid_hierarchy"
    assert detail["hint"]
    assert detail["suggested_tool"] == "hub_create_task"


async def test_approve_task(db: aiosqlite.Connection):
    body = TaskCreate(title="Draft task", source="agent", agent="bot")
    tv = await services.create_task(db, body)
    assert tv.status.value == "draft"

    # force=True bypasses the DoR gate so this test keeps focus on
    # lifecycle mechanics; gate behavior is covered in test_api_approve_gate.
    approved = await services.approve_task(db, tv.id, TaskApprove(force=True))
    assert approved.status.value == "open"
    rows = await db.execute_fetchall(
        "SELECT detail FROM activity_log WHERE kind='task_approved' ORDER BY id DESC LIMIT 1"
    )
    assert rows
    detail = json.loads(rows[0][0])
    assert detail["instance"] in ("prod", "local")
    assert detail["base_url"]


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
    assert started.branch == f"task-{tv.id}/pair-task"
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


async def test_done_from_open_without_pair_start_rejects_done(db: aiosqlite.Connection):
    body = TaskCreate(title="Open pair legacy")
    tv = await services.create_task(db, body)
    with pytest.raises(HTTPException) as exc_info:
        await services.add_update(
            db,
            tv.id,
            TaskUpdateCreate(agent="dev", kind="done", content="Report without start"),
        )
    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert detail["reason"] == "pair_start_required"
    assert detail["suggested_tool"] == "hub_pair_start"
    updates = await repo.get_task_updates(db, tv.id)
    assert not any(u["kind"] == "done" for u in updates)


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
    rows = await db.execute_fetchall(
        "SELECT detail FROM activity_log WHERE kind='task_update' ORDER BY id DESC LIMIT 1"
    )
    assert rows
    detail = json.loads(rows[0][0])
    assert detail["instance"] in ("prod", "local")
    assert detail["base_url"]


async def test_add_done_update_pending_report_routes_to_review_gate(
    db: aiosqlite.Connection,
):
    # Universal Review Gate (#306): pending_report + auto_review without an
    # APPROVED review no longer completes — the report becomes a submission.
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

    d = dict(await repo.get_task(db, task_id))
    assert d["status"] == "review"
    assert d["submission_generation"] == 1
    assert d["review_job_id"] is None


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


async def test_add_update_done_pending_report_completes_when_opted_out(
    db: aiosqlite.Connection,
):
    # auto_review=False is the explicit review opt-out: pending_report done
    # still completes directly (#306).
    task_id = await repo.create_task(
        db,
        title="Awaiting report",
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


async def test_get_inbox_data_filters_by_mine(db: aiosqlite.Connection):
    alice_draft = await repo.create_task(
        db,
        title="Alice draft",
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
    await repo.update_task(db, alice_draft, human_owner="alice")

    bob_draft = await repo.create_task(
        db,
        title="Bob draft",
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
    await repo.update_task(db, bob_draft, human_owner="bob")

    claimed = await repo.create_task(
        db,
        title="Claimed by alice",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="bot",
        rationale="",
        status="needs_decision",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(
        db,
        claimed,
        human_owner="bob",
        claimed_by="alice",
    )
    await db.commit()

    all_inbox = await services.get_inbox_data(db)
    assert len(all_inbox["drafts"]) >= 2

    mine = await services.get_inbox_data(db, mine="alice")
    draft_ids = {t.id for t in mine["drafts"]}
    decision_ids = {t.id for t in mine["decisions"]}
    assert alice_draft in draft_ids
    assert bob_draft not in draft_ids
    assert claimed in decision_ids


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


async def test_claim_task_from_open(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Claim me",
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

    tv = await services.claim_task(
        db, task_id, TaskClaim(agent="composer", session_id="sess-1")
    )
    assert tv.status.value == "claimed"
    assert tv.claimed_by == "composer"
    assert tv.claim_session_id == "sess-1"
    assert tv.assigned_agent == "composer"


async def _make_claimed(db: aiosqlite.Connection, *, auto_review: bool) -> int:
    task_id = await repo.create_task(
        db,
        title="Reserved",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=auto_review,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()
    await services.claim_task(db, task_id, TaskClaim(agent="composer", session_id="s1"))
    return task_id


async def test_done_report_on_claimed_completes_when_no_auto_review(
    db: aiosqlite.Connection,
):
    task_id = await _make_claimed(db, auto_review=False)
    await services.add_update(
        db,
        task_id,
        TaskUpdateCreate(agent="composer", kind="done", content="Work landed in main"),
    )
    row = await repo.get_task(db, task_id)
    assert row["status"] == "completed"
    # Claim is released so the task no longer looks reserved.
    assert row["claimed_by"] in (None, "")
    assert row["claim_session_id"] in (None, "")


async def test_done_report_on_claimed_auto_review_routes_to_review_gate(
    db: aiosqlite.Connection,
):
    # Universal Review Gate (#306): a claimed task with auto_review and no
    # branch no longer completes on done — it enters client-driven review.
    # The claim is still cleared.
    task_id = await _make_claimed(db, auto_review=True)
    await services.add_update(
        db,
        task_id,
        TaskUpdateCreate(agent="composer", kind="done", content="Done"),
    )
    row = await repo.get_task(db, task_id)
    assert row["status"] == "review"
    assert row["submission_generation"] == 1
    assert row["claimed_by"] in (None, "")


async def test_done_report_on_claimed_with_blocker_needs_decision(
    db: aiosqlite.Connection,
):
    task_id = await _make_claimed(db, auto_review=False)
    await services.add_update(
        db,
        task_id,
        TaskUpdateCreate(agent="composer", kind="blocker", content="Stuck on infra"),
    )
    await services.add_update(
        db,
        task_id,
        TaskUpdateCreate(agent="composer", kind="done", content="Partial"),
    )
    row = await repo.get_task(db, task_id)
    assert row["status"] == "needs_decision"


async def test_force_complete_from_claimed(db: aiosqlite.Connection):
    task_id = await _make_claimed(db, auto_review=True)
    tv = await services.force_complete_task(db, task_id)
    assert tv.status.value == "completed"
    assert tv.claimed_by in (None, "")
    assert any(
        update.kind == "done" and "Force-completed by human" in update.content
        for update in tv.updates
    )


async def test_force_complete_from_pair_running(db: aiosqlite.Connection):
    """Pair running (no job_id) is in the allowed force-complete set."""
    task_id = await repo.create_task(
        db,
        title="Pair running",
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
    await db.commit()  # job_id stays NULL → pair task
    tv = await services.force_complete_task(db, task_id)
    assert tv.status.value == "completed"


async def test_force_complete_rejected_for_headless_running(db: aiosqlite.Connection):
    """Headless running (job_id set) is poller-owned and must NOT force-complete."""
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
    await repo.update_task(db, task_id, job_id="job-xyz")
    await db.commit()
    with pytest.raises(HTTPException) as exc_info:
        await services.force_complete_task(db, task_id)
    assert exc_info.value.status_code == 400
    row = await repo.get_task(db, task_id)
    assert row["status"] == "running"


async def test_claim_task_conflict(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Taken",
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
    await services.claim_task(db, task_id, TaskClaim(agent="agent-a", session_id="s1"))

    with pytest.raises(HTTPException) as exc_info:
        await services.claim_task(
            db, task_id, TaskClaim(agent="agent-b", session_id="s2")
        )
    assert exc_info.value.status_code == 409


async def test_release_task(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Release me",
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
    await services.claim_task(db, task_id, TaskClaim(agent="composer", session_id="s1"))

    tv = await services.release_task(
        db, task_id, TaskRelease(agent="composer", session_id="s1")
    )
    assert tv.status.value == "open"
    assert tv.claimed_by is None


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


async def test_parent_rollup_cascade_task_feature_epic(db: aiosqlite.Connection):
    epic_id = await repo.create_task(
        db,
        title="Epic rollup",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=False,
        task_type="epic",
        parent_id=None,
        priority="medium",
    )
    feature_id = await repo.create_task(
        db,
        title="Feature rollup",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=False,
        task_type="feature",
        parent_id=epic_id,
        priority="medium",
    )
    await repo.create_task(
        db,
        title="Task A",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="completed",
        auto_review=False,
        task_type="task",
        parent_id=feature_id,
        priority="medium",
    )
    task_b = await repo.create_task(
        db,
        title="Task B",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="running",
        auto_review=False,
        task_type="task",
        parent_id=feature_id,
        priority="medium",
    )
    await db.commit()

    await services.add_update(
        db,
        task_b,
        TaskUpdateCreate(agent="dev", kind="done", content="Finished B"),
    )
    feature_row = await repo.get_task(db, feature_id)
    epic_row = await repo.get_task(db, epic_id)
    assert feature_row["status"] == "completed"
    assert epic_row["status"] == "completed"


async def test_done_report_idempotent_from_completed_rejected(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Already done",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="completed",
        auto_review=False,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()
    with pytest.raises(HTTPException) as exc_info:
        await services.add_update(
            db,
            task_id,
            TaskUpdateCreate(agent="dev", kind="done", content="Again"),
        )
    assert exc_info.value.status_code == 409
    updates = await repo.get_task_updates(db, task_id)
    assert not any(u["kind"] == "done" for u in updates)


# ---- Universal Review Gate (#305): submission generations and verdicts ----


async def _pair_running_task(
    db: aiosqlite.Connection, title: str = "Review generation task"
) -> int:
    tv = await services.create_task(db, TaskCreate(title=title))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: do the work")
    await db.commit()
    started = await services.pair_start_task(db, tv.id, caller="dev-agent")
    assert started.status.value == "running"
    return tv.id


async def test_submit_for_review_enters_review_with_generation(
    db: aiosqlite.Connection,
):
    task_id = await _pair_running_task(db)

    view = await services.submit_for_review(
        db, task_id, TaskSubmitReview(agent="dev-agent", summary="first pass")
    )

    assert view.status.value == "review"
    assert view.submission_generation == 1
    assert view.review_job_id is None  # client-driven review, no dispatch job
    assert view.review_verdict is None
    assert view.review_approved_current is False
    assert any(
        u.kind == "status" and "Submitted for review (submission #1)" in u.content
        for u in view.updates or []
    )


async def test_submit_for_review_rejected_from_open(db: aiosqlite.Connection):
    tv = await services.create_task(db, TaskCreate(title="Not started"))

    with pytest.raises(HTTPException) as exc_info:
        await services.submit_for_review(db, tv.id)
    assert exc_info.value.status_code == 400


async def test_submit_for_review_rejected_for_headless_task(
    db: aiosqlite.Connection,
):
    task_id = await repo.create_task(
        db,
        title="Headless",
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

    with pytest.raises(HTTPException) as exc_info:
        await services.submit_for_review(db, task_id)
    assert exc_info.value.status_code == 400
    assert "headless" in str(exc_info.value.detail)


async def test_resubmission_invalidates_prior_approval(db: aiosqlite.Connection):
    task_id = await _pair_running_task(db)
    await services.submit_for_review(db, task_id)

    approved = await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
    )
    assert approved.review_verdict == ReviewVerdict.approved
    assert approved.review_verdict_generation == 1
    assert approved.review_approved_current is True

    # Developer returns to work and submits changed work again.
    await repo.update_task(db, task_id, status="running")
    await db.commit()
    resubmitted = await services.submit_for_review(db, task_id)

    assert resubmitted.submission_generation == 2
    # The old verdict is still recorded but no longer applies.
    assert resubmitted.review_verdict == ReviewVerdict.approved
    assert resubmitted.review_verdict_generation == 1
    assert resubmitted.review_approved_current is False

    # A fresh APPROVED verdict re-validates the current submission.
    reapproved = await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
    )
    assert reapproved.review_verdict_generation == 2
    assert reapproved.review_approved_current is True


async def test_changes_requested_verdict_never_counts_as_approved(
    db: aiosqlite.Connection,
):
    task_id = await _pair_running_task(db)
    await services.submit_for_review(db, task_id)

    view = await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(
            verdict=ReviewVerdict.changes_requested,
            agent="reviewer",
            comments="1. Fix the tests",
        ),
    )
    assert view.review_verdict == ReviewVerdict.changes_requested
    assert view.review_approved_current is False
    assert any(
        u.kind == "review" and "CHANGES_REQUESTED" in u.content
        for u in view.updates or []
    )


async def test_record_verdict_requires_a_submission(db: aiosqlite.Connection):
    task_id = await _pair_running_task(db)

    with pytest.raises(HTTPException) as exc_info:
        await services.record_review_verdict(
            db,
            task_id,
            TaskReviewVerdict(verdict=ReviewVerdict.approved),
        )
    assert exc_info.value.status_code == 400


async def test_pair_done_report_bumps_submission_generation(
    db: aiosqlite.Connection,
):
    task_id = await repo.create_task(
        db,
        title="Pair done gen",
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
    await repo.update_task(db, task_id, branch="task-101/test")
    await db.commit()

    await services.add_update(
        db,
        task_id,
        TaskUpdateCreate(agent="dev", kind="done", content="Ready for review"),
    )

    d = dict(await repo.get_task(db, task_id))
    assert d["status"] == "ci_check"
    assert d["submission_generation"] == 1


async def test_record_verdict_with_findings_persists_structured_data(
    db: aiosqlite.Connection,
):
    from hub.models import ReviewFinding, ReviewSeverity

    task_id = await _pair_running_task(db, title="Findings task")
    await services.submit_for_review(db, task_id)

    view = await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(
            verdict=ReviewVerdict.changes_requested,
            agent="reviewer",
            findings=[
                ReviewFinding(id=1, severity=ReviewSeverity.high, message="Bug"),
            ],
        ),
    )
    assert view.latest_review is not None
    assert view.latest_review.findings[0].message == "Bug"

    # A later verdict without findings clears the list: findings belong
    # to the verdict that produced them.
    await repo.update_task(db, task_id, status="running")
    await db.commit()
    await services.submit_for_review(db, task_id)
    view = await services.record_review_verdict(
        db, task_id, TaskReviewVerdict(verdict=ReviewVerdict.approved)
    )
    assert view.latest_review is not None
    assert view.latest_review.findings == []
    assert view.review_approved_current is True


def test_parse_review_findings_malformed_json_is_ignored():
    assert services.parse_review_findings("not-json") == []
    assert services.parse_review_findings('{"id": 1}') == []
    assert services.parse_review_findings(None) == []
    assert services.parse_review_findings("[]") == []


def test_review_finding_model_rejects_invalid_payloads():
    from pydantic import ValidationError

    from hub.models import ReviewFinding

    with pytest.raises(ValidationError):
        ReviewFinding(id=0, severity="high", message="bad id")
    with pytest.raises(ValidationError):
        ReviewFinding(id=1, severity="catastrophic", message="bad severity")
    with pytest.raises(ValidationError):
        ReviewFinding(id=1, severity="low", message="")


# ---- Universal Review Gate (#306): completion enforcement ----


async def test_pair_done_without_review_routes_to_review_not_completed(
    db: aiosqlite.Connection,
):
    # AC-1: no branch, auto_review on, no APPROVED review → review, not done.
    task_id = await repo.create_task(
        db,
        title="Gate blocks",
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

    await services.add_update(
        db,
        task_id,
        TaskUpdateCreate(agent="dev", kind="done", content="First attempt"),
    )
    d = dict(await repo.get_task(db, task_id))
    assert d["status"] == "review"
    assert d["submission_generation"] == 1
    assert d["review_job_id"] is None
    # No duplicate done rows and a gate hint update is present.
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert sum(1 for u in updates if u["kind"] == "done") == 1
    assert any("Universal Review Gate" in u["content"] for u in updates)


async def test_pair_done_with_current_approval_completes_and_rolls_up(
    db: aiosqlite.Connection,
):
    # AC-2: APPROVED for the current submission → completed; parent feature
    # rolls up only after the actual completed state.
    epic = await services.create_task(
        db, TaskCreate(title="Gate epic", task_type=TaskType.epic)
    )
    feature = await services.create_task(
        db,
        TaskCreate(title="Gate feature", task_type=TaskType.feature, parent_id=epic.id),
    )
    tv = await services.create_task(
        db, TaskCreate(title="Gate child", parent_id=feature.id)
    )
    task_id = tv.id
    await repo.add_task_update(db, task_id, "dev", "status", "Plan: work")
    await db.commit()
    await services.pair_start_task(db, task_id, caller="dev")
    # Erase the branch so the no-branch gate path is exercised.
    await repo.update_task(db, task_id, branch=None)
    await db.commit()

    await services.submit_for_review(db, task_id)
    view = await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
    )
    assert view.status.value == "running"
    assert view.review_approved_current is True

    await services.add_update(
        db,
        task_id,
        TaskUpdateCreate(agent="dev", kind="done", content="Approved work"),
    )
    d = dict(await repo.get_task(db, task_id))
    assert d["status"] == "completed"
    # Completing approved work must NOT bump the generation (#306): the
    # verdict stays bound to the completed submission.
    assert d["submission_generation"] == 1
    assert d["review_verdict_generation"] == 1

    feature_row = dict(await repo.get_task(db, feature.id))
    assert feature_row["status"] == "completed"
    epic_row = dict(await repo.get_task(db, epic.id))
    assert epic_row["status"] == "completed"


async def test_gate_full_cycle_resubmission_then_approval(
    db: aiosqlite.Connection,
):
    # Full loop: done → review → CHANGES_REQUESTED → fix → done → review →
    # APPROVED → done → completed.
    task_id = await _pair_running_task(db, title="Gate full cycle")
    await repo.update_task(db, task_id, branch=None)
    await db.commit()

    await services.add_update(
        db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="v1")
    )
    assert dict(await repo.get_task(db, task_id))["status"] == "review"

    view = await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(
            verdict=ReviewVerdict.changes_requested,
            agent="reviewer",
            findings=[],
            comments="fix it",
        ),
    )
    assert view.status.value == "running"
    assert view.review_cycle == 1

    await services.add_update(
        db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="v2")
    )
    d = dict(await repo.get_task(db, task_id))
    assert d["status"] == "review"
    assert d["submission_generation"] == 2

    view = await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
    )
    assert view.review_approved_current is True

    await services.add_update(
        db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="ship")
    )
    assert dict(await repo.get_task(db, task_id))["status"] == "completed"


async def test_gate_review_cycle_limit_escalates_to_decision(
    db: aiosqlite.Connection,
):
    from hub import config

    task_id = await _pair_running_task(db, title="Gate cycle limit")
    await repo.update_task(
        db, task_id, branch=None, review_cycle=config.MAX_REVIEW_CYCLES
    )
    await db.commit()

    await services.add_update(
        db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="vN")
    )
    d = dict(await repo.get_task(db, task_id))
    assert d["status"] == "needs_decision"


async def test_force_complete_bypasses_gate_as_audited_override(
    db: aiosqlite.Connection,
):
    from hub.models import TaskForceComplete

    task_id = await _pair_running_task(db, title="Gate force override")
    await repo.update_task(db, task_id, branch=None)
    await db.commit()

    view = await services.force_complete_task(
        db,
        task_id,
        TaskForceComplete(comment="Human override: inspected manually"),
    )
    assert view.status.value == "completed"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert any("override" in u["content"].lower() for u in updates)


# ---- Universal Review Gate unification (#309) ----


async def test_dispatch_review_failure_escalates_not_completes(
    db: aiosqlite.Connection,
):
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins
    from hub.services.orchestration import dispatch_review

    task_id = await repo.create_task(
        db,
        title="Dispatch fail",
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
    await db.commit()

    plugins.dispatch.submit_task = AsyncMock(return_value={"error": "no runtime"})
    task = dict(await repo.get_task(db, task_id))
    await dispatch_review(db, task)

    d = dict(await repo.get_task(db, task_id))
    assert d["status"] == "needs_decision"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert any("Reviewer dispatch failed" in u["content"] for u in updates)


async def test_dispatch_fix_failure_escalates_not_completes(
    db: aiosqlite.Connection,
):
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins
    from hub.services.orchestration import dispatch_fix

    task_id = await repo.create_task(
        db,
        title="Fix dispatch fail",
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

    plugins.dispatch.submit_task = AsyncMock(return_value={"error": "no runtime"})
    task = dict(await repo.get_task(db, task_id))
    await dispatch_fix(db, task, "fix the findings")

    d = dict(await repo.get_task(db, task_id))
    assert d["status"] == "needs_decision"
    assert d["review_cycle"] == 1


async def test_refresh_task_completed_job_goes_through_gate(
    db: aiosqlite.Connection,
):
    from unittest.mock import MagicMock

    from hub.integrations.registry import plugins

    task_id = await repo.create_task(
        db,
        title="Refresh gate",
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
    await repo.update_task(db, task_id, job_id="job-refresh-1")
    await repo.add_task_update(db, task_id, "dev", "done", "Job finished")
    await db.commit()

    plugins.dispatch.get_job = MagicMock(
        return_value={"status": "completed", "exit_code": 0, "result_text": "ok"}
    )

    view = await services.refresh_task(db, task_id)
    # auto_review on, no approval, no branch → the review gate routes the
    # finished job to client-driven review instead of completed.
    assert view.status.value == "review"
    assert view.submission_generation == 1


async def test_refresh_task_failed_job_still_marks_failed(
    db: aiosqlite.Connection,
):
    from unittest.mock import MagicMock

    from hub.integrations.registry import plugins

    task_id = await repo.create_task(
        db,
        title="Refresh failed",
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
    await repo.update_task(db, task_id, job_id="job-refresh-2")
    await db.commit()

    plugins.dispatch.get_job = MagicMock(
        return_value={"status": "failed", "exit_code": 3, "result_text": "boom"}
    )

    view = await services.refresh_task(db, task_id)
    assert view.status.value == "failed"


# ---- Regression coverage for Universal Review Gate (#311) ----


async def test_stale_approval_does_not_complete_after_resubmission(
    db: aiosqlite.Connection,
):
    # Review checklist (#311): an APPROVED verdict for an earlier submission
    # must not let a later, unreviewed submission complete.
    task_id = await _pair_running_task(db, title="Stale approval gate")
    await repo.update_task(db, task_id, branch=None)
    await db.commit()

    await services.submit_for_review(db, task_id)
    await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
    )
    # Rework: resubmit changed work — the old approval goes stale.
    resubmitted = await services.submit_for_review(db, task_id)
    assert resubmitted.review_approved_current is False

    # Verdict for gen 2 arrives as CHANGES_REQUESTED; task returns to running.
    await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.changes_requested, agent="reviewer"),
    )
    await services.add_update(
        db,
        task_id,
        TaskUpdateCreate(agent="dev", kind="done", content="Still unapproved"),
    )
    d = dict(await repo.get_task(db, task_id))
    assert d["status"] == "review"  # blocked again, not completed
    assert d["submission_generation"] == 3


async def test_claimed_done_with_current_approval_completes_and_clears_claim(
    db: aiosqlite.Connection,
):
    task_id = await _pair_running_task(db, title="Claimed approved done")
    await repo.update_task(db, task_id, branch=None)
    await db.commit()
    await services.submit_for_review(db, task_id)
    await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
    )
    # Move the approved task into claimed state (claim survives rework paths).
    await repo.update_task(db, task_id, status="claimed", claimed_by="dev-agent")
    await db.commit()

    await services.add_update(
        db,
        task_id,
        TaskUpdateCreate(agent="dev-agent", kind="done", content="Approved work"),
    )
    d = dict(await repo.get_task(db, task_id))
    assert d["status"] == "completed"
    assert d["claimed_by"] in (None, "")


async def test_force_complete_from_pending_report_bypasses_gate(
    db: aiosqlite.Connection,
):
    from hub.models import TaskForceComplete

    task_id = await repo.create_task(
        db,
        title="Pending force override",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="pending_report",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    view = await services.force_complete_task(
        db, task_id, TaskForceComplete(comment="Human inspected the result")
    )
    assert view.status.value == "completed"


# ---- Agent-proposed features and epics (#323) ----


async def test_agent_proposed_feature_is_draft_then_approvable(
    db: aiosqlite.Connection,
):
    epic = await services.create_task(
        db, TaskCreate(title="Decomp epic", task_type="epic")
    )
    feature = await services.create_task(
        db,
        TaskCreate(
            title="Proposed feature",
            task_type="feature",
            parent_id=epic.id,
            source=TaskSource.agent,
            agent="analyst-bot",
        ),
    )
    assert feature.status.value == "draft"  # AC-1: draft, not open

    approved = await services.approve_task(
        db, feature.id, TaskApprove(comment="ok", force=True)
    )
    assert approved.status.value == "open"


async def test_human_created_feature_stays_open(db: aiosqlite.Connection):
    epic = await services.create_task(
        db, TaskCreate(title="Human epic", task_type="epic")
    )
    feature = await services.create_task(
        db,
        TaskCreate(
            title="Human feature",
            task_type="feature",
            parent_id=epic.id,
            source=TaskSource.human,
        ),
    )
    assert feature.status.value == "open"  # AC-2: unchanged


async def test_agent_proposed_epic_is_draft(db: aiosqlite.Connection):
    epic = await services.create_task(
        db,
        TaskCreate(title="Proposed epic", task_type="epic", source=TaskSource.agent),
    )
    assert epic.status.value == "draft"
