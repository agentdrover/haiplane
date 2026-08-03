from __future__ import annotations

import json
import os

import aiosqlite
import pytest
from fastapi import HTTPException

from hub import config
from hub.models import TaskRefine
from hub import repository as repo
from hub import services
from hub.models import (
    BulkChildTaskItem,
    BulkChildTasksCreate,
    FindingScope,
    ReviewFinding,
    ReviewSeverity,
    ReviewVerdict,
    TaskAnswer,
    TaskApprove,
    TaskClaim,
    TaskCreate,
    TaskDecide,
    TaskForceComplete,
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


async def test_start_task_escalates_when_git_refuses_the_workspace(
    db: aiosqlite.Connection,
):
    """#361: a refused workspace must stop dispatch, not be dispatched anyway.

    The mutation that removes the guard survives every other test in the suite:
    the refusal path and the dispatch path only meet here.
    """
    from hub.integrations.git_ops import WorkspaceNotReadyError
    from hub.integrations.noop import NoopDispatch, NoopGitOps
    from hub.integrations.registry import plugins

    submitted: list[str] = []

    class RefusingGitOps(NoopGitOps):
        async def create_branch(self, task_id, title, repo=None, base_branch=None):
            raise WorkspaceNotReadyError("dirty workspace: app.py")

    class RecordingDispatch(NoopDispatch):
        def is_available(self) -> bool:
            return True

        async def submit_task(self, *args, **kwargs):
            submitted.append("called")
            return {"job_id": "j1"}

    plugins.dispatch = RecordingDispatch()
    plugins.git_ops = RefusingGitOps()

    tv = await services.create_task(db, TaskCreate(title="Headless work"))
    await repo.add_task_update(db, tv.id, "human", "status", "Plan: do the work")
    await db.commit()

    started = await services.start_task(db, tv.id)

    assert not submitted, "dispatch must not run on a workspace git refused"
    assert started.status.value == "needs_decision"
    assert any(
        "dirty workspace: app.py" in update.content for update in started.updates
    ), "the human deciding must see why the workspace was refused"


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


async def test_force_complete_rejects_terminal_status(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Already done",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="completed",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await services.force_complete_task(db, task_id)
    assert exc_info.value.status_code == 400
    assert "terminal" in str(exc_info.value.detail)
    assert "completed" in str(exc_info.value.detail)


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


async def test_get_inbox_data_includes_ci_check_and_fix_requested(
    db: aiosqlite.Connection,
):
    # AC-4 (#393): ci_check and fix_requested get their own inbox buckets,
    # respect person filters, and pending_report keeps its existing bucket.
    ci_id = await repo.create_task(
        db,
        title="CI item",
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
    await repo.update_task(db, ci_id, human_owner="alice")
    fix_id = await repo.create_task(
        db,
        title="Fix item",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="fix_requested",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(db, fix_id, human_owner="bob")
    await db.commit()

    inbox = await services.get_inbox_data(db)
    assert ci_id in [t.id for t in inbox["ci_check_tasks"]]
    assert fix_id in [t.id for t in inbox["fix_requested_tasks"]]
    assert "pending_reports" in inbox

    mine = await services.get_inbox_data(db, human_owner="alice")
    assert [t.id for t in mine["ci_check_tasks"]] == [ci_id]
    assert [t.id for t in mine["fix_requested_tasks"]] == []


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
    # #370 T5: the assertion used to accept ("fix_requested", "open"), which is
    # both branches of the dispatch outcome — inverting the branch in
    # lifecycle.py left every decide test green. Name the branch this run took.
    assert tv.status.value == "fix_requested"
    assert tv.job_id, "a dispatched rework must carry the job it was dispatched as"
    decision_updates = [u for u in tv.updates if u.kind == "decision"]
    assert len(decision_updates) == 1
    assert "Edge case not handled" in decision_updates[0].content
    assert "Fix the edge case" in decision_updates[0].content


async def test_decide_task_rework_without_dispatch_returns_to_open(
    db: aiosqlite.Connection, monkeypatch
):
    """#370 T5, the other branch. Dispatch that hands back no job means nobody
    is working on it, so the task goes back to open rather than sitting in
    fix_requested waiting for an agent that was never started."""
    from hub.integrations.registry import plugins

    async def _no_job(*args, **kwargs):
        return {}

    monkeypatch.setattr(plugins.dispatch, "submit_task", _no_job)

    task_id = await repo.create_task(
        db,
        title="Rework without dispatch",
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

    tv = await services.decide_task(
        db, task_id, TaskDecide(action="rework", instructions="fix it")
    )

    assert tv.status.value == "open"
    assert not tv.job_id


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
    tv = await services.force_complete_task(
        db,
        task_id,
        TaskForceComplete(comment="Pair running human override"),
    )
    assert tv.status.value == "completed"


async def test_force_complete_rejects_active_dispatch_job(
    db: aiosqlite.Connection,
):
    """Active dispatch job blocks force-complete with 409 (AC-2)."""
    from unittest.mock import MagicMock

    from hub.integrations.registry import plugins

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
    await repo.update_task(
        db,
        task_id,
        job_id="job-xyz",
        claimed_by="dev",
        claim_session_id="sess-1",
        claimed_at="2026-07-17T12:00:00+00:00",
    )
    await db.commit()

    plugins.dispatch.get_job = MagicMock(
        return_value={"status": "running", "exit_code": None}
    )

    with pytest.raises(HTTPException) as exc_info:
        await services.force_complete_task(db, task_id)
    assert exc_info.value.status_code == 409
    assert "job-xyz" in str(exc_info.value.detail)
    assert "running" in str(exc_info.value.detail)
    row = await repo.get_task(db, task_id)
    assert row["status"] == "running"
    assert row["claimed_by"] == "dev"


async def test_force_complete_rejects_active_review_job(
    db: aiosqlite.Connection,
):
    from unittest.mock import MagicMock

    from hub.integrations.registry import plugins

    task_id = await repo.create_task(
        db,
        title="Review dispatch running",
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
    await repo.update_task(
        db,
        task_id,
        review_job_id="review-job-active",
        claimed_by="dev",
        claim_session_id="sess-r",
        claimed_at="2026-07-17T12:00:00+00:00",
    )
    await db.commit()

    plugins.dispatch.get_job = MagicMock(
        return_value={"status": "running", "exit_code": None}
    )

    with pytest.raises(HTTPException) as exc_info:
        await services.force_complete_task(
            db,
            task_id,
            TaskForceComplete(comment="Should not apply"),
        )
    assert exc_info.value.status_code == 409
    assert "review-job-active" in str(exc_info.value.detail)
    assert "running" in str(exc_info.value.detail)
    row = await repo.get_task(db, task_id)
    assert row["status"] == "review"
    assert row["claimed_by"] == "dev"


async def test_force_complete_allows_missing_review_job(
    db: aiosqlite.Connection,
):
    from unittest.mock import MagicMock

    from hub.integrations.registry import plugins

    task_id = await repo.create_task(
        db,
        title="Missing review job",
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
    await repo.update_task(db, task_id, review_job_id="review-job-missing")
    await db.commit()

    plugins.dispatch.get_job = MagicMock(return_value=None)

    view = await services.force_complete_task(
        db,
        task_id,
        TaskForceComplete(comment="Recover stale review dispatch"),
    )
    assert view.status.value == "completed"
    done = next(u for u in view.updates if u.kind == "done")
    assert "review_job_id=review-job-missing" in done.content
    assert "missing from registry" in done.content


async def test_force_complete_allows_terminal_review_job(
    db: aiosqlite.Connection,
):
    from unittest.mock import MagicMock

    from hub.integrations.registry import plugins

    task_id = await repo.create_task(
        db,
        title="Terminal review job",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="fix_requested",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(db, task_id, review_job_id="review-job-done")
    await db.commit()

    plugins.dispatch.get_job = MagicMock(
        return_value={"status": "completed", "exit_code": 0}
    )

    view = await services.force_complete_task(
        db,
        task_id,
        TaskForceComplete(comment="Close over finished review job"),
    )
    assert view.status.value == "completed"
    done = next(u for u in view.updates if u.kind == "done")
    assert "review_job_id=review-job-done" in done.content
    assert "terminal status='completed'" in done.content


async def test_force_complete_allows_missing_dispatch_job(
    db: aiosqlite.Connection,
):
    """Missing dispatch registry entry does not block recovery (AC-3)."""
    from unittest.mock import MagicMock

    from hub.integrations.registry import plugins

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
    await repo.update_task(db, task_id, job_id="job-missing")
    await db.commit()

    plugins.dispatch.get_job = MagicMock(return_value=None)

    view = await services.force_complete_task(
        db,
        task_id,
        TaskForceComplete(comment="Recover stale headless task"),
    )
    assert view.status.value == "completed"
    done = next(u for u in view.updates if u.kind == "done")
    assert "from_status=running" in done.content
    assert "job_id=job-missing" in done.content
    assert "missing from registry" in done.content


async def test_force_complete_allows_terminal_dispatch_job(
    db: aiosqlite.Connection,
):
    """Terminal dispatch job reference is allowed and audited (AC-3)."""
    from unittest.mock import MagicMock

    from hub.integrations.registry import plugins

    task_id = await repo.create_task(
        db,
        title="Stale ci_check",
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
    await repo.update_task(db, task_id, job_id="job-failed")
    await db.commit()

    plugins.dispatch.get_job = MagicMock(
        return_value={"status": "failed", "exit_code": 1}
    )

    view = await services.force_complete_task(
        db,
        task_id,
        TaskForceComplete(comment="Recover after terminal dispatch job"),
    )
    assert view.status.value == "completed"
    done = next(u for u in view.updates if u.kind == "done")
    assert "from_status=ci_check" in done.content
    assert "terminal status='failed'" in done.content


async def test_force_complete_from_open(db: aiosqlite.Connection):
    body = TaskCreate(title="Still open")
    tv = await services.create_task(db, body)
    assert tv.status.value == "open"

    with pytest.raises(HTTPException) as exc_info:
        await services.force_complete_task(db, tv.id)
    assert exc_info.value.status_code == 400
    assert "comment" in str(exc_info.value.detail)
    assert "open" in str(exc_info.value.detail)

    completed = await services.force_complete_task(
        db, tv.id, TaskForceComplete(comment="Human shutdown from open")
    )
    assert completed.status.value == "completed"


async def test_force_complete_requires_comment_from_active_ci_check(
    db: aiosqlite.Connection,
):
    task_id = await repo.create_task(
        db,
        title="Stuck ci",
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

    with pytest.raises(HTTPException) as exc_info:
        await services.force_complete_task(db, task_id)
    assert exc_info.value.status_code == 400
    assert "ci_check" in str(exc_info.value.detail)


async def test_force_complete_default_comment_from_pending_report(
    db: aiosqlite.Connection,
):
    task_id = await repo.create_task(
        db,
        title="Awaiting report",
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

    view = await services.force_complete_task(db, task_id)
    assert view.status.value == "completed"
    done = next(u for u in view.updates if u.kind == "done")
    assert "Force-completed by human without agent report." in done.content


async def test_force_complete_default_comment_from_draft(
    db: aiosqlite.Connection,
):
    """draft is not an ACTIVE_STATUS, so the default comment path applies."""
    task_id = await repo.create_task(
        db,
        title="Abandoned draft",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="draft",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    view = await services.force_complete_task(db, task_id)
    assert view.status.value == "completed"
    done = next(u for u in view.updates if u.kind == "done")
    assert "Force-completed by human without agent report." in done.content
    assert "from_status=draft" in done.content


async def test_force_complete_clears_stale_claim_metadata(
    db: aiosqlite.Connection,
):
    task_id = await repo.create_task(
        db,
        title="Stuck ci_check",
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
    await repo.update_task(
        db,
        task_id,
        claimed_by="dev",
        claim_session_id="sess-stale",
        claimed_at="2026-07-17T12:00:00+00:00",
    )
    await db.commit()

    view = await services.force_complete_task(
        db,
        task_id,
        TaskForceComplete(comment="Clear stale claim after ci_check stuck"),
    )
    assert view.status.value == "completed"
    assert view.claimed_by in (None, "")
    assert view.claim_session_id in (None, "")
    assert view.claimed_at in (None, "")
    row = await repo.get_task(db, task_id)
    assert row["claimed_at"] in (None, "")


async def test_force_complete_rejects_epic_with_incomplete_descendants(
    db: aiosqlite.Connection,
):
    epic_id = await repo.create_task(
        db,
        title="Epic",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="epic",
        parent_id=None,
        priority="medium",
    )
    await repo.create_task(
        db,
        title="Child feature",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="feature",
        parent_id=epic_id,
        priority="medium",
    )
    await db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await services.force_complete_task(db, epic_id)
    assert exc_info.value.status_code == 400
    assert "incomplete descendants" in str(exc_info.value.detail)


async def test_force_complete_feature_all_terminal_children_succeeds(
    db: aiosqlite.Connection,
):
    feature_id = await repo.create_task(
        db,
        title="Feature rollup",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="feature",
        parent_id=None,
        priority="medium",
    )
    await repo.create_task(
        db,
        title="Done child",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="completed",
        auto_review=True,
        task_type="task",
        parent_id=feature_id,
        priority="medium",
    )
    await repo.create_task(
        db,
        title="Failed child",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="failed",
        auto_review=True,
        task_type="task",
        parent_id=feature_id,
        priority="medium",
    )
    await db.commit()

    view = await services.force_complete_task(
        db,
        feature_id,
        TaskForceComplete(comment="Human closes feature after terminal children"),
    )
    assert view.status.value == "completed"


async def test_force_complete_epic_nested_descendants_cte(
    db: aiosqlite.Connection,
):
    epic_id = await repo.create_task(
        db,
        title="Epic nested",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="epic",
        parent_id=None,
        priority="medium",
    )
    feature_id = await repo.create_task(
        db,
        title="Feature under epic",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="completed",
        auto_review=True,
        task_type="feature",
        parent_id=epic_id,
        priority="medium",
    )
    await repo.create_task(
        db,
        title="Open task under feature",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=feature_id,
        priority="medium",
    )
    await db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await services.force_complete_task(
            db,
            epic_id,
            TaskForceComplete(comment="Should block on open grandchild"),
        )
    assert exc_info.value.status_code == 400
    assert "incomplete descendants" in str(exc_info.value.detail)

    await repo.update_task(db, feature_id, status="completed")
    grandchild = await db.execute_fetchall(
        "SELECT id FROM tasks WHERE parent_id=? LIMIT 1", (feature_id,)
    )
    await repo.update_task(db, grandchild[0]["id"], status="completed")
    await db.commit()

    view = await services.force_complete_task(
        db,
        epic_id,
        TaskForceComplete(comment="All nested descendants terminal"),
    )
    assert view.status.value == "completed"


@pytest.mark.parametrize(
    "status",
    [
        "draft",
        "open",
        "claimed",
        "running",
        "needs_info",
        "review",
        "fix_requested",
        "ci_check",
        "needs_decision",
        "pending_report",
    ],
)
async def test_force_complete_from_all_non_terminal_statuses(
    db: aiosqlite.Connection,
    status: str,
):

    task_id = await repo.create_task(
        db,
        title=f"Force from {status}",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status=status,
        auto_review=True,
        task_type="subtask" if status == "draft" else "task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    view = await services.force_complete_task(
        db,
        task_id,
        TaskForceComplete(comment=f"override from {status}"),
    )
    assert view.status.value == "completed"
    done = next(u for u in view.updates if u.kind == "done")
    assert f"from_status={status}" in done.content
    assert f"override from {status}" in done.content


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


# ---- Audited solo mode (#434) ----


async def test_solo_self_approved_verdict_is_marked(db: aiosqlite.Connection, caplog):
    task_id = await _pair_running_task(db, title="Solo verdict")
    await services.submit_for_review(db, task_id)

    with caplog.at_level("WARNING"):
        view = await services.record_review_verdict(
            db,
            task_id,
            TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="dev-agent"),
            self_approved=True,
        )

    assert view.latest_review is not None
    assert view.latest_review.self_approved is True
    assert view.review_approved_current is True
    assert any(
        u.kind == "review" and "[self-approved: solo mode" in u.content
        for u in view.updates or []
    )
    assert any(
        "OPENCLAW_REVIEW_SELF_APPROVE=allow" in r.getMessage() for r in caplog.records
    )


async def test_independent_verdict_is_not_marked_self_approved(
    db: aiosqlite.Connection,
):
    task_id = await _pair_running_task(db, title="Independent verdict")
    await services.submit_for_review(db, task_id)

    view = await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
    )

    assert view.latest_review is not None
    assert view.latest_review.self_approved is False
    assert all("self-approved" not in u.content for u in view.updates or [])


async def test_new_independent_verdict_clears_self_approved_mark(
    db: aiosqlite.Connection,
):
    task_id = await _pair_running_task(db, title="Solo then independent")
    await services.submit_for_review(db, task_id)
    await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.changes_requested, agent="dev-agent"),
        self_approved=True,
    )

    # Client-driven verdict returned the task to running; resubmit and let
    # an independent reviewer approve — the mark belongs to the verdict.
    await services.submit_for_review(db, task_id)
    view = await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
    )

    assert view.latest_review is not None
    assert view.latest_review.self_approved is False


def test_ensure_reviewer_independence_flags_solo_opt_out(monkeypatch):
    from hub import config

    task = {"assigned_agent": "impl-bot", "claimed_by": ""}

    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "allow")
    # The implementer passes only because of the opt-out — flagged.
    assert (
        services.ensure_reviewer_independence(
            task, is_agent=True, principal_id=None, username="impl-bot"
        )
        is True
    )
    # An independent agent reviewer is never flagged, even in solo mode.
    assert (
        services.ensure_reviewer_independence(
            task, is_agent=True, principal_id=None, username="reviewer-bot"
        )
        is False
    )
    # Humans always pass unflagged.
    assert (
        services.ensure_reviewer_independence(
            task, is_agent=False, principal_id=None, username="impl-bot"
        )
        is False
    )
    # Principal-based implementer match is flagged too (#320 identity wins).
    ptask = {
        "implementer_principal_id": 7,
        "assigned_agent": "other-name",
        "claimed_by": "",
    }
    assert (
        services.ensure_reviewer_independence(
            ptask, is_agent=True, principal_id=7, username="display-x"
        )
        is True
    )

    # With the flag off the implementer is still rejected — unchanged (#318).
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "forbid")
    with pytest.raises(HTTPException) as exc_info:
        services.ensure_reviewer_independence(
            task, is_agent=True, principal_id=None, username="impl-bot"
        )
    assert exc_info.value.status_code == 403


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


def test_review_finding_scope_defaults_and_validation():
    from pydantic import ValidationError

    finding = ReviewFinding(id=1, severity="low", message="legacy payload")
    assert finding.scope == FindingScope.in_scope
    assert finding.linked_task_id is None

    linked = ReviewFinding(
        id=2,
        severity="medium",
        message="belongs elsewhere",
        scope="out_of_scope",
        linked_task_id=436,
    )
    assert linked.scope == FindingScope.out_of_scope
    assert linked.linked_task_id == 436

    with pytest.raises(ValidationError):
        ReviewFinding(id=1, severity="low", message="bad scope", scope="elsewhere")
    with pytest.raises(ValidationError):
        ReviewFinding(id=1, severity="low", message="bad link", linked_task_id=0)


def test_parse_review_findings_legacy_json_defaults_to_in_scope():
    # Rows persisted before #435 have no scope key: they must parse as
    # in_scope so old verdicts keep their meaning.
    legacy = '[{"id": 1, "severity": "high", "message": "old finding"}]'
    findings = services.parse_review_findings(legacy)
    assert findings[0].scope == FindingScope.in_scope
    assert findings[0].linked_task_id is None


async def test_changes_requested_all_out_of_scope_findings_rejected(
    db: aiosqlite.Connection,
):
    # #435 (incident #392): if every finding is out of scope there is
    # nothing to fix in this task — the verdict must be approved instead.
    task_id = await _pair_running_task(db, title="All out of scope")
    await services.submit_for_review(db, task_id)

    with pytest.raises(HTTPException) as exc_info:
        await services.record_review_verdict(
            db,
            task_id,
            TaskReviewVerdict(
                verdict=ReviewVerdict.changes_requested,
                agent="reviewer",
                findings=[
                    ReviewFinding(
                        id=1,
                        severity=ReviewSeverity.high,
                        message="Refactor another module",
                        scope=FindingScope.out_of_scope,
                        linked_task_id=436,
                    ),
                ],
            ),
        )
    assert exc_info.value.status_code == 422
    detail = exc_info.value.detail
    assert detail["reason"] == "changes_requested_requires_in_scope_finding"
    assert "approved" in detail["hint"]

    # The rejected verdict must not have been recorded.
    task = dict(await repo.get_task(db, task_id))
    assert task["review_verdict"] is None
    assert task["status"] == "review"


async def test_out_of_scope_finding_without_link_warns_not_blocks(
    db: aiosqlite.Connection,
):
    task_id = await _pair_running_task(db, title="Unlinked out of scope")
    await services.submit_for_review(db, task_id)

    view = await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(
            verdict=ReviewVerdict.changes_requested,
            agent="reviewer",
            findings=[
                ReviewFinding(
                    id=1, severity=ReviewSeverity.high, message="Fix the race"
                ),
                ReviewFinding(
                    id=2,
                    severity=ReviewSeverity.low,
                    message="Cleanup elsewhere",
                    scope=FindingScope.out_of_scope,
                ),
            ],
        ),
    )
    # Non-blocking: the verdict is recorded, task returns to running.
    assert view.status.value == "running"
    review_update = next(u for u in view.updates if u.kind == "review")
    assert "Warning: out-of-scope finding(s) 2 have no linked_task_id" in (
        review_update.content
    )
    assert "[out-of-scope]" in review_update.content


async def test_finding_scope_and_linked_task_persist_roundtrip(
    db: aiosqlite.Connection,
):
    task_id = await _pair_running_task(db, title="Scope roundtrip")
    await services.submit_for_review(db, task_id)

    view = await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(
            verdict=ReviewVerdict.changes_requested,
            agent="reviewer",
            findings=[
                ReviewFinding(
                    id=1, severity=ReviewSeverity.high, message="In-task bug"
                ),
                ReviewFinding(
                    id=2,
                    severity=ReviewSeverity.medium,
                    message="Follow-up work",
                    scope=FindingScope.out_of_scope,
                    linked_task_id=436,
                ),
            ],
        ),
    )
    findings = view.latest_review.findings
    assert findings[0].scope == FindingScope.in_scope
    assert findings[0].linked_task_id is None
    assert findings[1].scope == FindingScope.out_of_scope
    assert findings[1].linked_task_id == 436
    review_update = next(u for u in view.updates if u.kind == "review")
    assert "[out-of-scope → #436]" in review_update.content
    assert "Warning:" not in review_update.content


# ---- Auto-draft follow-ups for out-of-scope findings (#436) ----


async def _count_tasks(db: aiosqlite.Connection) -> int:
    cur = await db.execute("SELECT COUNT(*) FROM tasks")
    row = await cur.fetchone()
    return int(row[0])


def _mixed_scope_verdict(
    *,
    create_tasks: bool,
    linked_task_id: int | None = None,
) -> TaskReviewVerdict:
    return TaskReviewVerdict(
        verdict=ReviewVerdict.changes_requested,
        agent="reviewer",
        create_tasks_for_out_of_scope=create_tasks,
        findings=[
            ReviewFinding(id=1, severity=ReviewSeverity.high, message="In-task bug"),
            ReviewFinding(
                id=2,
                severity=ReviewSeverity.medium,
                message="Race in the poller retry loop",
                file="hub/poller.py",
                line=42,
                recommendation="Serialize the retry path",
                scope=FindingScope.out_of_scope,
                linked_task_id=linked_task_id,
            ),
        ],
    )


async def test_out_of_scope_auto_draft_flag_off_creates_nothing(
    db: aiosqlite.Connection,
):
    task_id = await _pair_running_task(db, title="Flag off")
    await services.submit_for_review(db, task_id)
    before = await _count_tasks(db)

    view = await services.record_review_verdict(
        db, task_id, _mixed_scope_verdict(create_tasks=False)
    )

    assert await _count_tasks(db) == before
    assert view.latest_review.findings[1].linked_task_id is None
    review_update = next(u for u in view.updates if u.kind == "review")
    assert "Auto-created" not in review_update.content
    assert "Warning: out-of-scope finding(s) 2" in review_update.content


async def test_out_of_scope_auto_draft_creates_draft_and_links(
    db: aiosqlite.Connection,
):
    task_id = await _pair_running_task(db, title="Auto draft")
    await services.submit_for_review(db, task_id)
    before = await _count_tasks(db)

    view = await services.record_review_verdict(
        db, task_id, _mixed_scope_verdict(create_tasks=True)
    )

    assert await _count_tasks(db) == before + 1
    linked_id = view.latest_review.findings[1].linked_task_id
    assert linked_id is not None
    assert view.latest_review.findings[0].linked_task_id is None

    draft = dict(await repo.get_task(db, linked_id))
    assert draft["status"] == "draft"
    assert draft["task_type"] == "task"
    assert draft["source"] == "agent"
    assert draft["parent_id"] is None  # reviewed task has no feature parent
    assert draft["title"] == "Review follow-up: Race in the poller retry loop"
    marker = services.out_of_scope_draft_marker(task_id, 2)
    assert marker in draft["description"]
    assert f"from review of task #{task_id} (submission #1)" in draft["description"]
    assert "Severity: medium" in draft["description"]
    assert "Location: hub/poller.py:42" in draft["description"]
    assert "Recommendation: Serialize the retry path" in draft["description"]

    review_update = next(u for u in view.updates if u.kind == "review")
    assert f"Auto-created draft task(s) for out-of-scope findings: #{linked_id}" in (
        review_update.content
    )
    assert "Warning:" not in review_update.content


async def test_out_of_scope_auto_draft_inherits_feature_parent(
    db: aiosqlite.Connection,
):
    epic = await services.create_task(
        db, TaskCreate(title="Parent epic", task_type=TaskType.epic)
    )
    feature = await services.create_task(
        db,
        TaskCreate(
            title="Parent feature",
            task_type=TaskType.feature,
            parent_id=epic.id,
        ),
    )
    tv = await services.create_task(
        db, TaskCreate(title="Child under feature", parent_id=feature.id)
    )
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: do the work")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev-agent")
    await services.submit_for_review(db, tv.id)

    view = await services.record_review_verdict(
        db, tv.id, _mixed_scope_verdict(create_tasks=True)
    )

    linked_id = view.latest_review.findings[1].linked_task_id
    draft = dict(await repo.get_task(db, linked_id))
    assert draft["parent_id"] == feature.id
    assert draft["status"] == "draft"


async def test_out_of_scope_auto_draft_skips_already_linked_findings(
    db: aiosqlite.Connection,
):
    task_id = await _pair_running_task(db, title="Already linked")
    await services.submit_for_review(db, task_id)
    before = await _count_tasks(db)

    view = await services.record_review_verdict(
        db, task_id, _mixed_scope_verdict(create_tasks=True, linked_task_id=424)
    )

    assert await _count_tasks(db) == before
    assert view.latest_review.findings[1].linked_task_id == 424


async def test_out_of_scope_auto_draft_resubmit_does_not_duplicate(
    db: aiosqlite.Connection,
):
    task_id = await _pair_running_task(db, title="Idempotent resubmit")
    await services.submit_for_review(db, task_id)

    first = await services.record_review_verdict(
        db, task_id, _mixed_scope_verdict(create_tasks=True)
    )
    linked_id = first.latest_review.findings[1].linked_task_id
    assert linked_id is not None
    after_first = await _count_tasks(db)

    # Developer fixes finding 1 and resubmits; the reviewer sends the same
    # out-of-scope finding again WITHOUT a link — the existing draft must
    # be reused, not duplicated.
    await services.submit_for_review(db, task_id)
    second = await services.record_review_verdict(
        db, task_id, _mixed_scope_verdict(create_tasks=True)
    )

    assert await _count_tasks(db) == after_first
    assert second.latest_review.findings[1].linked_task_id == linked_id


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


# ---- Project git context (#337) ----


async def test_pair_start_uses_project_workspace_and_base(
    db: aiosqlite.Connection,
):
    # AC-1 (#337): pair branch is prepared in the project's workspace from
    # its base branch.
    from unittest.mock import AsyncMock

    from hub.db import seed_default_project
    from hub.integrations.registry import plugins

    await seed_default_project(db)
    pid = await repo.create_project(
        db,
        slug="prod-x",
        name="Prod X",
        repo_name="mrPDA/prod-x",
        workspace_path="/srv/prod-x",
        default_branch="trunk",
    )
    epic = await services.create_task(db, TaskCreate(title="X epic", task_type="epic"))
    await repo.update_task(db, epic.id, project_id=pid)
    feat = await services.create_task(
        db, TaskCreate(title="X feat", task_type="feature", parent_id=epic.id)
    )
    tv = await services.create_task(
        db, TaskCreate(title="X task", task_type="task", parent_id=feat.id)
    )
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: x")
    await db.commit()

    prep = AsyncMock(return_value=f"task-{tv.id}/x-task")
    plugins.git_ops.pair_prepare_branch = prep
    started = await services.pair_start_task(db, tv.id, caller="dev")
    assert started.status.value == "running"
    kwargs = prep.await_args.kwargs
    assert kwargs["repo"] == "/srv/prod-x"
    assert kwargs["base_branch"] == "trunk"


async def test_default_project_passes_its_seeded_values(db: aiosqlite.Connection):
    """Deliberately updated for #604 — this used to assert the OPPOSITE.

    #337's AC-2 pinned the plumbing: the default project passed no overrides,
    and an unconditional special case emptied its context so git_ops re-read
    the same values from env. Once the owner configured REAL fields (#602),
    that special case silently threw them away and three brief mechanisms
    stayed blind next to a live clone. The context now carries whatever the
    row holds; for a fresh seed those values mirror env, so the effective
    behavior of this scenario is unchanged — only the plumbing is honest.
    """
    from unittest.mock import AsyncMock

    from hub import config as cfg
    from hub.db import seed_default_project
    from hub.integrations.registry import plugins

    await seed_default_project(db)
    tv = await services.create_task(db, TaskCreate(title="Legacy task"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: y")
    await db.commit()

    prep = AsyncMock(return_value=f"task-{tv.id}/legacy-task")
    plugins.git_ops.pair_prepare_branch = prep
    await services.pair_start_task(db, tv.id, caller="dev")
    kwargs = prep.await_args.kwargs
    assert kwargs.get("repo") == str(cfg.WORKSPACE_REPO_LINK), (
        "the seeded workspace must reach git_ops explicitly, not by env luck"
    )
    assert kwargs.get("base_branch") == cfg.PAIR_BASE_BRANCH


# --- pair workspace restore (#451) ---


async def test_submit_for_review_restores_pair_workspace(db: aiosqlite.Connection):
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins

    task_id = await _pair_running_task(db)
    restore = AsyncMock(return_value=True)
    plugins.git_ops.pair_restore_workspace_base = restore

    await services.submit_for_review(db, task_id)

    restore.assert_awaited_once()
    assert restore.await_args.args[0] == task_id


async def test_report_done_restores_pair_workspace(db: aiosqlite.Connection):
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins

    tv = await services.create_task(
        db,
        TaskCreate(title="Done restore", auto_review=False),
    )
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: finish")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")

    restore = AsyncMock(return_value=True)
    plugins.git_ops.pair_restore_workspace_base = restore

    await services.add_update(
        db,
        tv.id,
        TaskUpdateCreate(agent="dev", kind="done", content="All done"),
    )

    restore.assert_awaited_once()
    assert restore.await_args.args[0] == tv.id


async def test_release_task_restores_pair_workspace(db: aiosqlite.Connection):
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins

    tv = await services.create_task(db, TaskCreate(title="Release restore"))
    claimed = await services.claim_task(
        db, tv.id, TaskClaim(agent="dev-agent", session_id="sess-1")
    )
    assert claimed.status.value == "claimed"

    restore = AsyncMock(return_value=True)
    plugins.git_ops.pair_restore_workspace_base = restore

    await services.release_task(
        db, tv.id, TaskRelease(agent="dev-agent", session_id="sess-1")
    )

    restore.assert_awaited_once()
    assert restore.await_args.args[0] == tv.id


async def test_pair_start_conflict_returns_structured_detail(db: aiosqlite.Connection):
    from hub.integrations.git_ops import PairBranchConflictError
    from hub.integrations.registry import plugins

    tv = await services.create_task(db, TaskCreate(title="Conflict task"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: go")
    await db.commit()

    async def boom(*args, **kwargs):
        raise PairBranchConflictError(
            "Uncommitted changes in workspace; commit or stash before pair-start",
            reason="pair_branch_dirty",
            hint="Commit or stash, then hub_pair_start.",
            workspace_path="/var/lib/openclaw-hub/workspaces/_default",
            hostname="agenthai",
        )

    plugins.git_ops.pair_prepare_branch = boom

    with pytest.raises(HTTPException) as exc_info:
        await services.pair_start_task(db, tv.id, caller="dev")

    assert exc_info.value.status_code == 422
    detail = exc_info.value.detail
    assert detail["reason"] == "pair_branch_dirty"
    assert detail["workspace_path"] == "/var/lib/openclaw-hub/workspaces/_default"
    assert detail["hostname"] == "agenthai"
    assert "hub_pair_start" in detail["hint"]


# --- project binding at epic creation (#346) ---


async def _make_project(
    db: aiosqlite.Connection, slug: str = "calc", status: str = "active"
) -> int:
    from hub.db import seed_default_project

    await seed_default_project(db)
    return await repo.create_project(
        db, slug=slug, name=slug.title(), repo_name=f"mrPDA/{slug}", status=status
    )


async def test_create_epic_with_project_slug(db: aiosqlite.Connection):
    pid = await _make_project(db)
    tv = await services.create_task(
        db, TaskCreate(title="Calc epic", task_type="epic", project="calc")
    )
    row = await repo.get_task(db, tv.id)
    assert row["project_id"] == pid
    resolved = await repo.resolve_project_for_task(db, tv.id)
    assert resolved["slug"] == "calc"


async def test_create_epic_agent_draft_keeps_project(db: aiosqlite.Connection):
    # Agent proposes an epic (#323 draft gate) — project binds immediately,
    # so approval does not lose the routing.
    pid = await _make_project(db)
    tv = await services.create_task(
        db,
        TaskCreate(
            title="Agent epic",
            task_type="epic",
            project="calc",
            source="agent",
            agent="bot",
        ),
    )
    assert tv.status.value == "draft"
    row = await repo.get_task(db, tv.id)
    assert row["project_id"] == pid


async def test_create_non_epic_with_project_rejected(db: aiosqlite.Connection):
    await _make_project(db)
    epic = await services.create_task(db, TaskCreate(title="E", task_type="epic"))
    with pytest.raises(HTTPException) as exc:
        await services.create_task(
            db,
            TaskCreate(
                title="F", task_type="feature", parent_id=epic.id, project="calc"
            ),
        )
    assert exc.value.status_code == 422


async def test_create_epic_unknown_project_rejected(db: aiosqlite.Connection):
    await _make_project(db)
    with pytest.raises(HTTPException) as exc:
        await services.create_task(
            db, TaskCreate(title="E", task_type="epic", project="nope")
        )
    assert exc.value.status_code == 422
    assert "unknown project" in str(exc.value.detail)


async def test_create_epic_pending_project_rejected(db: aiosqlite.Connection):
    # Pending proposals (#345) cannot take epics until a human activates them.
    await _make_project(db, slug="pend", status="pending")
    with pytest.raises(HTTPException) as exc:
        await services.create_task(
            db, TaskCreate(title="E", task_type="epic", project="pend")
        )
    assert exc.value.status_code == 422
    assert "not active" in str(exc.value.detail)


async def test_create_epic_archived_project_rejected(db: aiosqlite.Connection):
    pid = await _make_project(db, slug="old")
    await repo.update_project(db, pid, archived=1)
    with pytest.raises(HTTPException) as exc:
        await services.create_task(
            db, TaskCreate(title="E", task_type="epic", project="old")
        )
    assert exc.value.status_code == 422


# --- events feed (#349) ---


async def _all_events(db: aiosqlite.Connection) -> list[dict]:
    return [dict(r) for r in await repo.list_events(db, since=0)]


async def test_event_emitted_on_verdict(db: aiosqlite.Connection):
    # AC-1: verdict emits review_verdict_recorded in the same transaction.
    task_id = await _pair_running_task(db, title="Verdict event task")
    await services.submit_for_review(db, task_id)
    await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
    )
    evs = [e for e in await _all_events(db) if e["kind"] == "review_verdict_recorded"]
    assert len(evs) == 1
    assert evs[0]["task_id"] == task_id
    assert evs[0]["actor"] == "reviewer"
    payload = json.loads(evs[0]["payload"])
    assert payload["verdict"] == "approved"
    assert payload["submission_generation"] == 1


async def test_event_rolls_back_with_transaction(db: aiosqlite.Connection):
    # AC-1: insert_event does not commit — a rollback removes the event.
    await repo.insert_event(db, kind="task_approved", task_id=123)
    await db.rollback()
    assert await _all_events(db) == []


async def test_event_kinds_on_human_gates(db: aiosqlite.Connection):
    # AC-2: approve / reject / answer / decide-accept emit typed events.
    d1 = await services.create_task(
        db, TaskCreate(title="Draft A", source="agent", agent="bot")
    )
    await services.approve_task(db, d1.id, TaskApprove(force=True))

    d2 = await services.create_task(
        db, TaskCreate(title="Draft B", source="agent", agent="bot")
    )
    await services.reject_task(db, d2.id)

    t3 = await services.create_task(db, TaskCreate(title="Q task"))
    await repo.update_task(db, t3.id, status="needs_info")
    await db.commit()
    await services.answer_question(db, t3.id, TaskAnswer(answer="42", resume=False))

    t4 = await services.create_task(db, TaskCreate(title="Decide task"))
    await repo.update_task(db, t4.id, status="needs_decision")
    await db.commit()
    await services.decide_task(db, t4.id, TaskDecide(action="accept"))

    events = await _all_events(db)
    by_kind = {e["kind"]: e for e in events}
    assert by_kind["task_approved"]["task_id"] == d1.id
    assert by_kind["task_rejected"]["task_id"] == d2.id
    assert by_kind["question_answered"]["task_id"] == t3.id
    completed = by_kind["task_completed"]
    assert completed["task_id"] == t4.id
    assert json.loads(completed["payload"])["via"] == "decide_accept"


async def test_event_completed_on_report_done(db: aiosqlite.Connection):
    # AC-2: the report_done completion path emits task_completed.
    tv = await services.create_task(
        db, TaskCreate(title="Done path", auto_review=False)
    )
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: quick")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")
    await services.add_update(
        db, tv.id, TaskUpdateCreate(agent="dev", kind="done", content="Done")
    )
    evs = [e for e in await _all_events(db) if e["kind"] == "task_completed"]
    assert len(evs) == 1
    assert json.loads(evs[0]["payload"])["via"] == "report_done"


# --- workspace provisioning (#347) ---


async def _provision_target(db: aiosqlite.Connection, **overrides) -> int:
    from hub.db import seed_default_project

    await seed_default_project(db)
    fields = {
        "slug": "prov",
        "name": "Prov",
        "repo_name": "mrPDA/prov",
        "workspace_path": "/srv/prov",
        "default_branch": "trunk",
    }
    fields.update(overrides)
    pid = await repo.create_project(db, **fields)
    await db.commit()
    return pid


async def test_provision_project_success_and_repeat(db: aiosqlite.Connection):
    # AC-1: clone called with repo/workspace/base; repeat is the fetch path.
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins

    pid = await _provision_target(db)
    clone = AsyncMock(side_effect=[(True, "cloned"), (True, "existing clone verified")])
    plugins.git_ops.clone_repo = clone

    result = await services.provision_project(db, pid, actor="denis")
    assert result["provision_status"] == "ok"
    clone.assert_awaited_with("mrPDA/prov", "/srv/prov", "trunk")
    row = await repo.get_project(db, pid)
    assert row["provision_status"] == "ok"
    assert row["provision_detail"] == "cloned"

    again = await services.provision_project(db, pid)
    assert again["provision_status"] == "ok"
    assert "existing clone" in again["provision_detail"]

    events = [
        dict(e)
        for e in await repo.list_events(db, since=0, kinds=["project_provisioned"])
    ]
    assert len(events) == 2
    assert events[0]["project_id"] == pid


async def test_provision_project_remote_inaccessible(db: aiosqlite.Connection):
    # AC-2: readable error detail, project row otherwise intact.
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins

    pid = await _provision_target(db, slug="broken", workspace_path="/srv/broken")
    plugins.git_ops.clone_repo = AsyncMock(
        return_value=(False, "remote not accessible: no deploy key")
    )
    result = await services.provision_project(db, pid)
    assert result["provision_status"] == "error"
    assert "remote not accessible" in result["provision_detail"]
    row = await repo.get_project(db, pid)
    assert row["provision_status"] == "error"
    assert row["slug"] == "broken" and row["archived"] == 0


async def test_provision_project_missing_repo_or_workspace(db: aiosqlite.Connection):
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins

    clone = AsyncMock()
    plugins.git_ops.clone_repo = clone
    pid = await _provision_target(db, slug="norepo", repo_name="")
    result = await services.provision_project(db, pid)
    assert result["provision_status"] == "error"
    assert "no repo" in result["provision_detail"]

    pid2 = await _provision_target(db, slug="nows", workspace_path="")
    result2 = await services.provision_project(db, pid2)
    assert result2["provision_status"] == "error"
    assert "workspace_path" in result2["provision_detail"]
    clone.assert_not_awaited()


async def test_provision_project_noop_gitops_is_readable_error(
    db: aiosqlite.Connection,
):
    # Noop integration is a valid outcome: error + WHY, never a crash.
    pid = await _provision_target(db, slug="noop")
    result = await services.provision_project(db, pid)
    assert result["provision_status"] == "error"
    assert "git ops disabled" in result["provision_detail"]


# --- machine-review policy (#382) ---


def _mr_task(**overrides) -> dict:
    base = {
        "id": 1,
        "machine_review_override": None,
        "work_type": "feature",
        "size": "M",
        "risks": "[]",
    }
    base.update(overrides)
    return base


def test_machine_review_required_matrix():
    # AC-1: параметризованная матрица автоправил.
    from hub.services.orchestration import machine_review_required as req

    cases = [
        (_mr_task(work_type="docs"), "auto", False),
        (_mr_task(work_type="chore"), "auto", False),
        (_mr_task(work_type="spike"), "auto", False),
        (_mr_task(work_type="feature", size="S"), "auto", False),
        (_mr_task(work_type="feature", size="M"), "auto", True),
        (_mr_task(work_type="bug", size="L"), "auto", True),
        (_mr_task(work_type="bug", size=None), "auto", True),  # unsized → review
        (_mr_task(work_type="refactor", size="XS"), "auto", True),
        (
            _mr_task(
                work_type="docs",
                risks='[{"kind": "security", "severity": "low"}]',
            ),
            "auto",
            True,
        ),  # security risk beats work_type
        (
            _mr_task(
                work_type="chore",
                risks='[{"kind": "performance", "severity": "high"}]',
            ),
            "auto",
            True,
        ),  # high severity beats work_type
    ]
    for task, policy, expected in cases:
        assert req(task, policy) is expected, (task["work_type"], task.get("size"))


def test_machine_review_cascade():
    # AC-2: override задачи > политика проекта > автоправила.
    from hub.services.orchestration import machine_review_required as req

    assert req(_mr_task(work_type="docs"), "always") is True
    assert req(_mr_task(work_type="feature", size="L"), "off") is False
    assert (
        req(_mr_task(work_type="docs", machine_review_override="require"), "off")
        is True
    )
    assert (
        req(
            _mr_task(work_type="feature", size="L", machine_review_override="skip"),
            "always",
        )
        is False
    )


async def test_verdict_blocked_in_require_mode(db: aiosqlite.Connection, monkeypatch):
    # AC-3: require блокирует вердикт без актуального отчёта; warn — нет.
    from hub import config as config_module

    task_id = await _pair_running_task(db, title="MR policy task")
    await repo.update_task(db, task_id, size="M", work_type="feature")
    await db.commit()
    await services.submit_for_review(db, task_id)

    monkeypatch.setattr(config_module, "MACHINE_REVIEW_MODE", "require")
    with pytest.raises(HTTPException) as exc:
        await services.record_review_verdict(
            db,
            task_id,
            TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
        )
    assert exc.value.status_code == 422
    assert "machine-review" in str(exc.value.detail)

    # Гейт касается только аппрува: отклонить работу ревьюер может всегда,
    # даже в require-режиме без отчёта (#383 review finding).
    rejected = await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.changes_requested, agent="reviewer"),
    )
    assert rejected.review_verdict == ReviewVerdict.changes_requested
    # возвращаем задачу в review для продолжения сценария
    await services.submit_for_review(db, task_id)

    # отчёт для текущего сабмишена снимает блок
    row = await repo.get_task(db, task_id)
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=dict(row)["submission_generation"],
        raw_count=1,
    )
    await db.commit()
    approved = await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
    )
    assert approved.review_verdict == ReviewVerdict.approved

    # в warn-режиме блока нет даже без отчёта
    monkeypatch.setattr(config_module, "MACHINE_REVIEW_MODE", "warn")
    task_id2 = await _pair_running_task(db, title="MR warn task")
    await repo.update_task(db, task_id2, size="M", work_type="feature")
    await db.commit()
    await services.submit_for_review(db, task_id2)
    ok = await services.record_review_verdict(
        db,
        task_id2,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
    )
    assert ok.review_verdict == ReviewVerdict.approved


async def test_submit_for_review_hints_machine_review(db: aiosqlite.Connection):
    task_id = await _pair_running_task(db, title="Hint task")
    await repo.update_task(db, task_id, size="L", work_type="feature")
    await db.commit()
    view = await services.submit_for_review(db, task_id)
    assert view.lifecycle_hint and "Machine-review" in view.lifecycle_hint


# ---- Ownership/deadline matrix coverage (#418) ----


def test_matrix_policies_are_well_formed():
    # AC-3 (#418): human/agent_queue instances name a surface and next actor
    # and never auto-transition; machine instances carry a finite deadline
    # config, an escalation and a reason. A malformed/missing policy fails here.
    from hub import config
    from hub.lifecycle_matrix import (
        LIFECYCLE_MATRIX,
        OWNER_MACHINE,
        VALID_OWNERS,
    )

    assert LIFECYCLE_MATRIX  # non-empty
    for key, p in LIFECYCLE_MATRIX.items():
        assert p.owner in VALID_OWNERS, key
        assert p.next_actor, key
        assert p.surface, key
        if p.owner == OWNER_MACHINE:
            assert p.deadline_config is not None, key
            assert hasattr(config, p.deadline_config), key
            assert p.escalation is not None, key
            assert p.reason is not None, key
        else:
            assert p.deadline_config is None, key
            assert p.escalation is None, key


def test_matrix_covers_every_non_terminal_status_and_discriminator():
    # AC-4 (#418): a new TaskStatus enum member or a new running/review
    # discriminator that lacks a policy breaks this test until one is added.
    from hub.lifecycle_matrix import (
        LIFECYCLE_MATRIX,
        non_terminal_statuses,
        resolve_instance,
    )

    covered = {p.status for p in LIFECYCLE_MATRIX.values()}
    for s in non_terminal_statuses():
        assert s.value in covered, f"no lifecycle policy covers status {s.value}"

    for job_id, review_job_id in ((("j"), None), (None, None)):
        assert (
            resolve_instance("running", job_id=job_id, review_job_id=None)
            in LIFECYCLE_MATRIX
        )
    for review_job_id in ("r", None):
        assert (
            resolve_instance("review", job_id=None, review_job_id=review_job_id)
            in LIFECYCLE_MATRIX
        )


# ---- Noop GitOps accepts project context keywords (#420) ----


async def test_noop_gitops_accepts_project_context_keywords():
    # AC-4 (#420): the noop (and any protocol implementation) accepts the
    # project-context keywords, so the core lifecycle keeps working.
    from hub.integrations.noop import NoopGitOps

    g = NoopGitOps()
    probe = await g.check_pr_ci(1, repo="/ws", gh_repo="owner/repo")
    assert probe.outcome.value == "pending"
    assert await g.merge_pr(1, 2, "t", repo="/ws", gh_repo="owner/repo") is False
    assert await g.get_ci_failure_logs(1, "b", repo="/ws", gh_repo="owner/repo") == {}


# ---- Rework closes the arbiter/verdict window (#422) ----


async def test_decide_rework_closes_arbiter_marker(db: aiosqlite.Connection):
    # AC-3 (#422): a human rework decision resets the cycle and clears the
    # arbiter marker, so the reworked submission starts clean.
    from unittest.mock import AsyncMock

    from hub.integrations.noop import NoopDispatch
    from hub.integrations.registry import plugins
    from hub.models import TaskDecide

    task_id = await repo.create_task(
        db,
        title="Reworked task",
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
    await repo.bump_submission_generation(db, task_id)  # generation 1
    await repo.record_review_verdict(db, task_id, "changes_requested")
    await repo.claim_arbiter_dispatch(db, task_id, 1)
    await repo.mark_arbiter_finished(db, task_id)
    await repo.update_task(db, task_id, review_cycle=3)
    await db.commit()

    mock_dispatch = NoopDispatch()
    mock_dispatch.submit_task = AsyncMock(return_value={"job_id": "fix-1"})
    plugins.dispatch = mock_dispatch

    await services.decide_task(
        db, task_id, TaskDecide(action="rework", instructions="fix it")
    )

    row = dict(await repo.get_task(db, task_id))
    assert row["arbiter_state"] is None
    assert row["arbiter_generation"] is None
    assert row["arbiter_job_id"] is None
    assert row["review_cycle"] == 0


# ---- Unified review budget semantics (#423) ----


@pytest.mark.parametrize(
    "review_cycle, max_cycles, expected",
    [
        (0, 3, False),
        (1, 3, False),
        (2, 3, False),
        (3, 3, True),  # AC-2: budget spent at MAX
        (4, 3, True),
        (5, 3, True),  # review_cycle > MAX
        (0, 0, True),  # AC-3: MAX<=0 exhausted immediately
        (0, 1, False),
        (1, 1, True),
    ],
)
def test_review_budget_exhausted_boundaries(review_cycle, max_cycles, expected):
    # AC-2/AC-3 (#423): one helper, one documented boundary — exhausted once
    # review_cycle reaches max_cycles. Same result for any caller (pair or
    # headless) since it is a single pure function.
    from hub.services import review_budget_exhausted

    assert review_budget_exhausted(review_cycle, max_cycles) is expected


def test_review_budget_is_single_source_of_truth():
    # AC-4 (#423): no flow compares review_cycle to MAX_REVIEW_CYCLES on its
    # own — only review_budget_exhausted may.
    import pathlib
    import re

    pat = re.compile(r"review_cycle.{0,40}(<|>=|\+ 1).{0,20}MAX_REVIEW_CYCLES")
    for rel in ("hub/poller.py", "hub/services/lifecycle.py"):
        src = pathlib.Path(rel).read_text()
        assert not pat.search(src), f"{rel} compares review_cycle to MAX directly"

    orch = pathlib.Path("hub/services/orchestration.py").read_text()
    helper_start = orch.index("def review_budget_exhausted")
    helper_end = orch.index("async def dispatch_review")
    outside = orch[:helper_start] + orch[helper_end:]
    assert not pat.search(outside), "orchestration compares outside the helper"


# --- pair-start claim mismatch: actionable hint + same-principal (#453) ---


async def _make_claimed_for_pair(
    db: aiosqlite.Connection, *, holder: str, principal_id: int | None
) -> int:
    task_id = await repo.create_task(
        db,
        title="Claimed pair",
        description="",
        runtime="auto",
        source="human",
        assigned_agent=holder,
        rationale="",
        status="claimed",
        auto_review=False,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(
        db, task_id, claimed_by=holder, implementer_principal_id=principal_id
    )
    await repo.add_task_update(db, task_id, holder, "status", "Plan: fix it")
    await db.commit()
    return task_id


async def test_pair_start_claim_mismatch_structured_error(db: aiosqlite.Connection):
    # AC-1 (#453): different holder/caller and different principal → structured 409.
    task_id = await _make_claimed_for_pair(db, holder="alice", principal_id=1)
    with pytest.raises(HTTPException) as ei:
        await services.pair_start_task(
            db,
            task_id,
            TaskPairStart(assigned_agent="bob"),
            caller="bob",
            implementer_principal_id=2,
        )
    exc = ei.value
    assert exc.status_code == 409
    detail = exc.detail
    assert isinstance(detail, dict)
    assert detail["reason"] == "pair_start_claim_mismatch"
    assert detail["claimed_by"] == "alice"
    assert detail["caller_identity"] == "bob"
    assert "alice" in detail["hint"]
    assert detail["suggested_tool"] == "hub_pair_start"


async def test_pair_start_same_principal_different_name_allowed(
    db: aiosqlite.Connection,
):
    # AC-2 (#453): same authenticated principal, different presentational name → allowed.
    task_id = await _make_claimed_for_pair(
        db, holder="cursor-orchestrator", principal_id=7
    )
    started = await services.pair_start_task(
        db,
        task_id,
        TaskPairStart(assigned_agent="cursor"),
        caller="cursor",
        implementer_principal_id=7,
    )
    assert started.status.value == "running"


async def test_pair_start_matching_name_still_allowed(db: aiosqlite.Connection):
    # Guard: the happy path (name matches holder) is unaffected by the new check.
    task_id = await _make_claimed_for_pair(db, holder="alice", principal_id=None)
    started = await services.pair_start_task(
        db, task_id, TaskPairStart(assigned_agent="alice"), caller="alice"
    )
    assert started.status.value == "running"


# --- pair workspace switch back to task branch on rework (#457) ---


async def test_changes_requested_switches_pair_workspace_to_task(
    db: aiosqlite.Connection,
):
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins

    # AC-1 (#457): CHANGES_REQUESTED (review→running) switches workspace to branch.
    task_id = await _pair_running_task(db)
    await services.submit_for_review(db, task_id)
    switch = AsyncMock(return_value=True)
    plugins.git_ops.pair_switch_to_task_branch = switch

    await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(
            verdict=ReviewVerdict.changes_requested,
            agent="reviewer",
            comments="1. fix the tests",
        ),
    )

    switch.assert_awaited_once()
    assert switch.await_args.args[0] == task_id


async def test_approved_verdict_does_not_switch_pair_workspace(
    db: aiosqlite.Connection,
):
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins

    # Symmetry check: APPROVED needs no switch (task moves on to report_done).
    task_id = await _pair_running_task(db)
    await services.submit_for_review(db, task_id)
    switch = AsyncMock(return_value=True)
    plugins.git_ops.pair_switch_to_task_branch = switch

    await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
    )

    switch.assert_not_awaited()


# --- worktree-per-task mode routing (#459) ---


async def test_worktree_mode_prepare_and_cleanup(db: aiosqlite.Connection, monkeypatch):
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins

    monkeypatch.setenv("OPENCLAW_WORKTREE_PER_TASK", "1")
    prep = AsyncMock(return_value="task-1/worktree-task")
    remove = AsyncMock(return_value=True)
    plugins.git_ops.pair_prepare_worktree = prep
    plugins.git_ops.pair_remove_worktree = remove

    tv = await services.create_task(db, TaskCreate(title="Worktree task"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: x")
    await db.commit()

    started = await services.pair_start_task(db, tv.id, caller="dev")
    assert started.status.value == "running"
    prep.assert_awaited()  # worktree created, main clone not switched

    await services.submit_for_review(db, tv.id)
    remove.assert_awaited()  # worktree removed on submit
    assert remove.await_args.args[0] == tv.id


async def test_worktree_mode_rework_recreates_worktree(
    db: aiosqlite.Connection, monkeypatch
):
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins

    monkeypatch.setenv("OPENCLAW_WORKTREE_PER_TASK", "1")
    prep = AsyncMock(return_value="task-1/worktree-task")
    plugins.git_ops.pair_prepare_worktree = prep
    plugins.git_ops.pair_remove_worktree = AsyncMock(return_value=True)

    task_id = await _pair_running_task(db)
    await services.submit_for_review(db, task_id)
    prep.reset_mock()

    await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(
            verdict=ReviewVerdict.changes_requested, agent="reviewer", comments="1. fix"
        ),
    )
    prep.assert_awaited()  # worktree re-created for rework


async def test_worktree_mode_off_uses_legacy(db: aiosqlite.Connection, monkeypatch):
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins

    monkeypatch.delenv("OPENCLAW_WORKTREE_PER_TASK", raising=False)
    prep_wt = AsyncMock()
    plugins.git_ops.pair_prepare_worktree = prep_wt

    tv = await services.create_task(db, TaskCreate(title="Legacy path"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: x")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")

    prep_wt.assert_not_awaited()  # legacy branch-switching path, not worktrees


async def _wt_done_task(db, *, job_id):
    task_id = await repo.create_task(
        db,
        title="WT done",
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
    await repo.update_task(db, task_id, branch=f"task-{task_id}/wt", job_id=job_id)
    await db.commit()
    return task_id


def _mock_git_ops(plugins, wt):
    from unittest.mock import AsyncMock, MagicMock

    plugins.git_ops.worktree_path = MagicMock(return_value=wt)
    plugins.git_ops.checkout = AsyncMock(return_value=True)
    plugins.git_ops.dirty_paths = AsyncMock(return_value=[])
    plugins.git_ops.auto_commit = AsyncMock(return_value=True)
    plugins.git_ops.squash_branch = AsyncMock(return_value=True)
    plugins.git_ops.push_branch = AsyncMock(return_value=True)
    plugins.git_ops.create_pr = AsyncMock(return_value=None)


async def test_worktree_mode_done_pipeline_targets_worktree(
    db: aiosqlite.Connection, monkeypatch, tmp_path
):
    # #459 review HIGH: in worktree mode the done→squash/push pipeline must run
    # in the task worktree, NOT the main clone (else squash resets the base branch).
    from hub.integrations.registry import plugins
    from hub.services import orchestration

    monkeypatch.setenv("OPENCLAW_WORKTREE_PER_TASK", "1")
    task_id = await _wt_done_task(db, job_id=None)  # pair task
    wt = str(tmp_path / f".main-worktrees/task-{task_id}")
    os.makedirs(wt, exist_ok=True)  # worktree exists → redirect applies
    _mock_git_ops(plugins, wt)

    row = await repo.get_task(db, task_id)
    status = await orchestration.transition_after_agent_done(
        db, dict(row), has_done=True
    )

    assert status == "ci_check"
    assert plugins.git_ops.squash_branch.await_args.kwargs["repo"] == wt
    assert plugins.git_ops.push_branch.await_args.kwargs["repo"] == wt
    assert plugins.git_ops.checkout.await_args.kwargs["repo"] == wt
    assert plugins.git_ops.create_pr.await_args.kwargs["repo"] == wt


async def test_worktree_mode_headless_uses_main_clone(
    db: aiosqlite.Connection, monkeypatch, tmp_path
):
    # #459 review re-run HIGH: a headless task (job_id set) has no worktree, so
    # even with the flag on the done-pipeline must stay on the main clone — never
    # redirect to a nonexistent worktree path (which would crash the poller).
    from hub.integrations.registry import plugins
    from hub.services import orchestration

    monkeypatch.setenv("OPENCLAW_WORKTREE_PER_TASK", "1")
    task_id = await _wt_done_task(db, job_id="dispatch-42")  # headless task
    ctx = await orchestration.project_git_context(db, task_id)
    workspace = ctx.get("repo")
    # worktree_path returns a real-looking but nonexistent sibling path
    _mock_git_ops(plugins, str(tmp_path / f".main-worktrees/task-{task_id}"))

    row = await repo.get_task(db, task_id)
    status = await orchestration.transition_after_agent_done(
        db, dict(row), has_done=True
    )

    assert status == "ci_check"
    # None of the git ops targeted the (nonexistent) worktree — they used workspace.
    assert plugins.git_ops.squash_branch.await_args.kwargs["repo"] == workspace
    assert plugins.git_ops.checkout.await_args.kwargs["repo"] == workspace


# --- pair-start surfaces worktree path/mode to the agent (#530) ---


async def test_pair_start_surfaces_worktree_path(db: aiosqlite.Connection, monkeypatch):
    # AC-1 (#530): worktree mode → TaskView carries workspace_mode + worktree_path.
    from unittest.mock import AsyncMock, MagicMock

    from hub.integrations.registry import plugins

    monkeypatch.setenv("OPENCLAW_WORKTREE_PER_TASK", "1")
    plugins.git_ops.pair_prepare_worktree = AsyncMock(return_value="task-1/wt")
    plugins.git_ops.worktree_path = MagicMock(return_value="/srv/.ws-worktrees/task-1")

    tv = await services.create_task(db, TaskCreate(title="WT surface"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: x")
    await db.commit()

    started = await services.pair_start_task(db, tv.id, caller="dev")
    assert started.status.value == "running"
    assert started.workspace_mode == "worktree"
    assert started.worktree_path == "/srv/.ws-worktrees/task-1"


async def test_pair_start_legacy_no_worktree_path(
    db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#530): legacy mode → workspace_mode=legacy, no worktree path.
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins

    monkeypatch.delenv("OPENCLAW_WORKTREE_PER_TASK", raising=False)
    plugins.git_ops.pair_prepare_branch = AsyncMock(return_value="task-1/leg")

    tv = await services.create_task(db, TaskCreate(title="Legacy surface"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: x")
    await db.commit()

    started = await services.pair_start_task(db, tv.id, caller="dev")
    assert started.workspace_mode == "legacy"
    assert started.worktree_path == ""


# ---- Verifiable SDD: AC-tests verdict gate (#508) ----


async def _review_task_with_ac_result(db, *, status_result: str):
    from hub.models import AcceptanceCriterion

    task_id = await _pair_running_task(db)
    await services.submit_for_review(db, task_id)  # generation 1, status review
    await repo.replace_acceptance_criteria(
        db,
        task_id,
        [
            AcceptanceCriterion(
                id="AC-1",
                given="g",
                when="w",
                then="t",
                verifiable_by="test",
                test_ref="tests/test_x.py::test_a",
            )
        ],
    )
    await repo.upsert_ac_test_result(db, task_id, "AC-1", 1, status_result)
    await db.commit()
    return task_id


async def test_ac_tests_gate_require_blocks_approved_when_red(db, monkeypatch):
    # AC-1 (#508): require + a red current test-AC blocks APPROVED with 422.
    monkeypatch.setattr("hub.config.SDD_AC_TESTS", "require")
    task_id = await _review_task_with_ac_result(db, status_result="fail")
    with pytest.raises(HTTPException) as exc:
        await services.record_review_verdict(
            db,
            task_id,
            TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
        )
    assert exc.value.status_code == 422
    assert "ac_tests_not_green" in str(exc.value.detail)


async def test_ac_tests_gate_warn_allows_approved_when_red(db, monkeypatch):
    # AC-2 (#508): warn never blocks — the red status is only shown in the brief.
    monkeypatch.setattr("hub.config.SDD_AC_TESTS", "warn")
    task_id = await _review_task_with_ac_result(db, status_result="fail")
    view = await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
    )
    assert view.review_verdict == ReviewVerdict.approved


async def test_ac_tests_gate_require_allows_changes_requested_when_red(db, monkeypatch):
    # AC-3 (#508): the gate never blocks a rejection — reviewer can always reject.
    monkeypatch.setattr("hub.config.SDD_AC_TESTS", "require")
    task_id = await _review_task_with_ac_result(db, status_result="fail")
    view = await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.changes_requested, agent="reviewer"),
    )
    assert view.review_verdict == ReviewVerdict.changes_requested


async def test_ac_tests_gate_require_allows_approved_when_green(db, monkeypatch):
    # require + all current test-AC green → APPROVED passes.
    monkeypatch.setattr("hub.config.SDD_AC_TESTS", "require")
    task_id = await _review_task_with_ac_result(db, status_result="pass")
    view = await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
    )
    assert view.review_verdict == ReviewVerdict.approved


async def _review_task_with_test_ac(db, *, test_ref):
    from hub.models import AcceptanceCriterion

    task_id = await _pair_running_task(db)
    await services.submit_for_review(db, task_id)
    await repo.replace_acceptance_criteria(
        db,
        task_id,
        [
            AcceptanceCriterion(
                id="AC-1",
                given="g",
                when="w",
                then="t",
                verifiable_by="test",
                test_ref=test_ref,
            )
        ],
    )
    await db.commit()
    return task_id


# ---- Verifiable SDD: validation_commands completion gate (#510) ----


async def _completing_task_with_validation(
    db, *, validation_status=None, commands=None
):
    task_id = await repo.create_task(
        db,
        title="v",
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
    await repo.bump_submission_generation(db, task_id)  # generation 1
    if commands is not None:
        await repo.update_task(db, task_id, validation_commands=json.dumps(commands))
    if validation_status is not None:
        await repo.update_task(
            db,
            task_id,
            validation_generation=1,
            validation_status=validation_status,
            validation_log="",
        )
    await db.commit()
    return task_id


async def test_ac_tests_gate_require_blocks_unlocatable_test_ac(db, monkeypatch):
    # An AC declared verifiable_by=test whose locator no runner can resolve is a
    # gap, not an exemption: #507 never runs it, so silently passing it here let
    # APPROVED through with zero test evidence — and SDD_AC_LOCATOR is off by
    # default, so refine accepts such a test_ref.
    monkeypatch.setattr("hub.config.SDD_AC_TESTS", "require")
    task_id = await _review_task_with_test_ac(db, test_ref="см. ручной QA")
    with pytest.raises(HTTPException) as exc:
        await services.record_review_verdict(
            db,
            task_id,
            TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
        )
    assert exc.value.status_code == 422
    assert "локатор теста не разрешается" in str(exc.value.detail)


async def test_ac_tests_gate_warn_allows_unlocatable_test_ac(db, monkeypatch):
    # warn still only warns — the gap is reported, never enforced.
    monkeypatch.setattr("hub.config.SDD_AC_TESTS", "warn")
    task_id = await _review_task_with_test_ac(db, test_ref="см. ручной QA")
    view = await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
    )
    assert view.review_verdict == ReviewVerdict.approved


async def test_ac_tests_gate_ignores_non_test_ac_without_locator(db, monkeypatch):
    # The new gap must not spill onto AC that never claimed to be machine-verified.
    from hub.models import AcceptanceCriterion

    monkeypatch.setattr("hub.config.SDD_AC_TESTS", "require")
    task_id = await _pair_running_task(db)
    await services.submit_for_review(db, task_id)
    await repo.replace_acceptance_criteria(
        db,
        task_id,
        [
            AcceptanceCriterion(
                id="AC-1",
                given="g",
                when="w",
                then="t",
                verifiable_by="manual",
                test_ref=None,
            )
        ],
    )
    await db.commit()
    view = await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
    )
    assert view.review_verdict == ReviewVerdict.approved


async def test_validation_gate_require_blocks_completion_when_failed(db, monkeypatch):
    # AC-1 (#510): require + a failed current validation run blocks completion.
    monkeypatch.setattr("hub.config.SDD_VALIDATION", "require")
    task_id = await _completing_task_with_validation(
        db, commands=["uv run pytest -q"], validation_status="fail"
    )
    with pytest.raises(HTTPException) as exc:
        await services.add_update(
            db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="done")
        )
    assert exc.value.status_code == 422
    assert "validation_failed" in str(exc.value.detail)
    assert dict(await repo.get_task(db, task_id))["status"] == "running"


async def test_validation_gate_warn_allows_completion_when_failed(db, monkeypatch):
    # AC-2 (#510): warn never blocks completion.
    monkeypatch.setattr("hub.config.SDD_VALIDATION", "warn")
    task_id = await _completing_task_with_validation(
        db, commands=["uv run pytest -q"], validation_status="fail"
    )
    await services.add_update(
        db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="done")
    )
    assert dict(await repo.get_task(db, task_id))["status"] == "completed"


async def test_validation_gate_require_allows_completion_when_pass(db, monkeypatch):
    # AC-3 (#510): a green current validation run passes the gate.
    monkeypatch.setattr("hub.config.SDD_VALIDATION", "require")
    task_id = await _completing_task_with_validation(
        db, commands=["uv run pytest -q"], validation_status="pass"
    )
    await services.add_update(
        db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="done")
    )
    assert dict(await repo.get_task(db, task_id))["status"] == "completed"


async def test_validation_gate_require_allows_completion_without_commands(
    db, monkeypatch
):
    # No validation_commands → no gap → completion passes even under require.
    monkeypatch.setattr("hub.config.SDD_VALIDATION", "require")
    task_id = await _completing_task_with_validation(db)
    await services.add_update(
        db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="done")
    )
    assert dict(await repo.get_task(db, task_id))["status"] == "completed"


async def test_commit_scope_gate_escalates_on_foreign_files(
    db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#361 AC-1: files outside the task's declared scope must not be committed.

    create_branch's dirty-refusal only proves the tree was clean at t=0, and a
    headless task then shares the main clone for its whole run. affected_areas
    is the only attribution the hub has, so 'require' hands the call to a human
    rather than dropping files or committing them silently.
    """
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins
    from hub.services import orchestration

    monkeypatch.setattr(config, "COMMIT_SCOPE_GATE", "require")
    task_id = await _wt_done_task(db, job_id="dispatch-77")
    await repo.update_task_structured(
        db, task_id, TaskRefine(affected_areas=["hub/services"])
    )
    await db.commit()
    _mock_git_ops(plugins, str(tmp_path / "unused"))
    plugins.git_ops.dirty_paths = AsyncMock(
        return_value=["hub/services/orchestration.py", "notes.txt"]
    )

    row = await repo.get_task(db, task_id)
    status = await orchestration.transition_after_agent_done(
        db, dict(row), has_done=True
    )

    assert status == "needs_decision"
    plugins.git_ops.auto_commit.assert_not_awaited()
    plugins.git_ops.push_branch.assert_not_awaited()
    updates = await repo.get_task_updates(db, task_id)
    assert any("notes.txt" in u["content"] for u in updates), (
        "the human deciding must be told which file is out of scope"
    )
    assert not any("orchestration.py" in u["content"] for u in updates), (
        "in-scope files are the task's own work and must not be reported"
    )


async def test_commit_scope_gate_warns_but_commits_by_default(
    db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Default is warn: the same finding is recorded, the pipeline continues."""
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins
    from hub.services import orchestration

    monkeypatch.setattr(config, "COMMIT_SCOPE_GATE", "warn")
    task_id = await _wt_done_task(db, job_id="dispatch-78")
    await repo.update_task_structured(
        db, task_id, TaskRefine(affected_areas=["hub/services"])
    )
    await db.commit()
    _mock_git_ops(plugins, str(tmp_path / "unused"))
    plugins.git_ops.dirty_paths = AsyncMock(return_value=["notes.txt"])

    row = await repo.get_task(db, task_id)
    status = await orchestration.transition_after_agent_done(
        db, dict(row), has_done=True
    )

    assert status != "needs_decision"
    plugins.git_ops.auto_commit.assert_awaited()
    updates = await repo.get_task_updates(db, task_id)
    assert any("notes.txt" in u["content"] for u in updates)


async def test_commit_scope_gate_says_so_when_it_cannot_check(
    db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """No affected_areas means the check did not run — silence would read as
    'checked and clean'."""
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins
    from hub.services import orchestration

    monkeypatch.setattr(config, "COMMIT_SCOPE_GATE", "require")
    task_id = await _wt_done_task(db, job_id="dispatch-79")  # no affected_areas
    _mock_git_ops(plugins, str(tmp_path / "unused"))
    plugins.git_ops.dirty_paths = AsyncMock(return_value=["notes.txt"])

    row = await repo.get_task(db, task_id)
    status = await orchestration.transition_after_agent_done(
        db, dict(row), has_done=True
    )

    assert status != "needs_decision", "cannot check is not the same as violated"
    updates = await repo.get_task_updates(db, task_id)
    assert any("не выполнялась" in u["content"] for u in updates)


async def test_done_pipeline_stops_when_checkout_fails(
    db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#361 I1: a failed checkout must stop the tail, not run it elsewhere.

    checkout() has always returned a bool and nobody read it, while the comment
    directly above the call already named the consequence — squash_branch
    resetting the wrong branch. Gating auto_commit alone was not enough: its
    result was ignored too, so squash_branch (reset --soft) and push_branch ran
    regardless. Per docs/workspace-safety-policy.md invariant 4 the task
    escalates instead of reporting readiness it does not have.
    """
    from hub.integrations.registry import plugins
    from hub.services import orchestration

    task_id = await _wt_done_task(db, job_id="dispatch-99")
    _mock_git_ops(plugins, str(tmp_path / "unused"))
    from unittest.mock import AsyncMock

    plugins.git_ops.checkout = AsyncMock(return_value=False)  # checkout fails

    row = await repo.get_task(db, task_id)
    status = await orchestration.transition_after_agent_done(
        db, dict(row), has_done=True
    )

    assert status == "needs_decision", "must not claim ci_check with nothing pushed"
    plugins.git_ops.auto_commit.assert_not_awaited()
    plugins.git_ops.squash_branch.assert_not_awaited()
    plugins.git_ops.push_branch.assert_not_awaited()
    plugins.git_ops.create_pr.assert_not_awaited()

    updated = dict(await repo.get_task(db, task_id))
    assert updated["status"] == "needs_decision"


# --- #365 K4: pair-start must not overwrite a status that moved under it ---


async def test_pair_start_refuses_when_the_task_left_its_status(
    db: aiosqlite.Connection, monkeypatch
):
    """The check and the write were not atomic, and the gap is not theoretical:
    branch preparation between them talks to git and can take seconds.

    Simulated by moving the task while pair-start is mid-flight — the same
    thing a competing claim, a human decision or the poller would do.
    """
    tv = await services.create_task(db, TaskCreate(title="Raced pair start"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: do the work")
    await db.commit()

    from hub.integrations.noop import NoopGitOps
    from hub.integrations.registry import plugins

    class _MovesTheTask(NoopGitOps):
        async def pair_prepare_branch(self, *args, **kwargs):
            # Somebody else moves the task while we are preparing the branch.
            await repo.update_task(db, tv.id, status="needs_decision")
            await db.commit()
            return ""

    plugins.git_ops = _MovesTheTask()

    with pytest.raises(HTTPException) as excinfo:
        await services.pair_start_task(db, tv.id, caller="dev-agent")

    assert excinfo.value.status_code == 409
    assert dict(await repo.get_task(db, tv.id))["status"] == "needs_decision", (
        "the other move must survive; pair-start must not overwrite it"
    )


async def test_pair_start_still_works_from_open_and_from_claimed(
    db: aiosqlite.Connection,
):
    """Both legitimate starting states keep working.

    transition_status_if takes a single expected_from, so the conditional
    transition is driven by the status actually read rather than a literal —
    a literal would have silently broken the claimed path.
    """
    from hub.integrations.noop import NoopGitOps
    from hub.integrations.registry import plugins

    plugins.git_ops = NoopGitOps()

    first = await services.create_task(db, TaskCreate(title="From open"))
    await repo.add_task_update(db, first.id, "dev", "status", "Plan: work")
    await db.commit()
    assert (
        await services.pair_start_task(db, first.id, caller="dev-agent")
    ).status.value == "running"

    second = await services.create_task(db, TaskCreate(title="From claimed"))
    await repo.add_task_update(db, second.id, "dev", "status", "Plan: work")
    await db.commit()
    await services.claim_task(db, second.id, TaskClaim(agent="dev-agent"))
    assert dict(await repo.get_task(db, second.id))["status"] == "claimed"
    assert (
        await services.pair_start_task(db, second.id, caller="dev-agent")
    ).status.value == "running"


# --- #364 K3: a failing git tail must not leave a done report behind ---


async def _task_ready_for_done_with_git_tail(db: aiosqlite.Connection) -> int:
    """A task whose done report reaches the git steps.

    auto_review matters: without it the tail is skipped entirely. A first
    attempt at reproducing this defect set auto_review=False and concluded it
    did not reproduce — the tail simply never ran.
    """
    tv = await services.create_task(db, TaskCreate(title="Done with git tail"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: work")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")
    await repo.update_task(db, tv.id, auto_review=True, branch="task-x/b")
    await db.commit()
    return tv.id


async def test_failing_git_tail_leaves_no_done_report(
    db: aiosqlite.Connection, monkeypatch
):
    """#364 AC-1. Reproduced before the fix: one done row and generation 1
    survived a git adapter that raised."""
    from hub.integrations.noop import NoopGitOps
    from hub.integrations.registry import plugins

    monkeypatch.setenv("OPENCLAW_WORKSPACE_REPO", "/tmp")
    task_id = await _task_ready_for_done_with_git_tail(db)

    class _Exploding(NoopGitOps):
        async def checkout(self, *args, **kwargs):
            raise RuntimeError("git adapter exploded mid-flight")

    plugins.git_ops = _Exploding()

    with pytest.raises(RuntimeError):
        await services.add_update(
            db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="готово")
        )

    updates = await repo.get_task_updates(db, task_id)
    assert not [u for u in updates if u["kind"] == "done"], (
        "a done report that did not survive its own git steps must not remain"
    )
    assert dict(await repo.get_task(db, task_id))["submission_generation"] == 0, (
        "the generation bump belongs to the same failed submission"
    )


async def test_retry_after_a_failing_git_tail_does_not_pile_up_done_rows(
    db: aiosqlite.Connection, monkeypatch
):
    """#364 AC-2. Before the fix each attempt added its own row: two rows and
    generation 2 after a single retry."""
    from hub.integrations.noop import NoopGitOps
    from hub.integrations.registry import plugins

    monkeypatch.setenv("OPENCLAW_WORKSPACE_REPO", "/tmp")
    task_id = await _task_ready_for_done_with_git_tail(db)

    class _Exploding(NoopGitOps):
        async def checkout(self, *args, **kwargs):
            raise RuntimeError("still broken")

    plugins.git_ops = _Exploding()

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await services.add_update(
                db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="x")
            )

    updates = await repo.get_task_updates(db, task_id)
    assert not [u for u in updates if u["kind"] == "done"]
    assert dict(await repo.get_task(db, task_id))["submission_generation"] == 0


async def test_successful_update_still_commits(db: aiosqlite.Connection):
    """The savepoint must not swallow the ordinary path: a plain status update
    is still there after the call returns."""
    tv = await services.create_task(db, TaskCreate(title="Ordinary"))
    await db.commit()

    await services.add_update(
        db, tv.id, TaskUpdateCreate(agent="dev", kind="status", content="just a note")
    )

    updates = await repo.get_task_updates(db, tv.id)
    assert any(u["content"] == "just a note" for u in updates)


# ---- epic liveness (#569): judged by descendants, not by the epic itself ----


async def _epic_with_child(db, *, epic_status: str, child_status: str | None) -> int:
    tv = await services.create_task(
        db, TaskCreate(title=f"Epic {epic_status}/{child_status}", task_type="epic")
    )
    await repo.update_task(db, tv.id, status=epic_status)
    if child_status is not None:
        child = await services.create_task(
            db,
            TaskCreate(title="child", task_type="feature", parent_id=tv.id),
        )
        await repo.update_task(db, child.id, status=child_status)
    await db.commit()
    return tv.id


async def test_completed_epic_with_open_child_is_still_live(db: aiosqlite.Connection):
    """#501 and #449 in miniature: the epic closed, a tail of work did not.
    The old criterion judged the epic by its own status and silently dropped
    exactly these."""
    epic_id = await _epic_with_child(db, epic_status="completed", child_status="open")

    live = {r["id"] for r in await repo.list_live_epics(db)}

    assert epic_id in live


async def test_open_epic_without_live_children_is_done(db: aiosqlite.Connection):
    """All children final — including FAILED, which the task prose forgot but
    the lifecycle model counts as terminal (spec review, finding 1)."""
    done_id = await _epic_with_child(db, epic_status="open", child_status="completed")
    failed_id = await _epic_with_child(db, epic_status="open", child_status="failed")

    live = {r["id"] for r in await repo.list_live_epics(db)}

    assert done_id not in live, "an open shell over finished work is noise"
    assert failed_id not in live, (
        "failed is final everywhere else in the model; liveness must agree"
    )


async def test_childless_epic_is_visible_until_it_is_closed(
    db: aiosqlite.Connection,
):
    fresh_id = await _epic_with_child(db, epic_status="open", child_status=None)
    closed_id = await _epic_with_child(db, epic_status="completed", child_status=None)

    live = {r["id"] for r in await repo.list_live_epics(db)}

    assert fresh_id in live, "a just-approved epic must not vanish"
    assert closed_id not in live


async def test_live_epics_are_not_silently_truncated(db: aiosqlite.Connection):
    """The old query carried LIMIT 20; the 21st live epic disappeared with no
    trace. The list is bounded by reality, not by a constant."""
    ids = set()
    for i in range(25):
        ids.add(await _epic_with_child(db, epic_status="open", child_status="open"))

    live = {r["id"] for r in await repo.list_live_epics(db)}

    assert ids <= live, f"lost: {sorted(ids - live)}"


async def test_liveness_sees_deep_descendants(db: aiosqlite.Connection):
    """An open task under a completed FEATURE under a completed epic: only a
    recursive walk sees it — direct children alone would call this epic done."""
    epic = await services.create_task(
        db, TaskCreate(title="Deep epic", task_type="epic")
    )
    feature = await services.create_task(
        db, TaskCreate(title="f", task_type="feature", parent_id=epic.id)
    )
    task = await services.create_task(
        db, TaskCreate(title="t", task_type="task", parent_id=feature.id)
    )
    await repo.update_task(db, epic.id, status="completed")
    await repo.update_task(db, feature.id, status="completed")
    await repo.update_task(db, task.id, status="open")
    await db.commit()

    live = {r["id"] for r in await repo.list_live_epics(db)}

    assert epic.id in live, "depth matters: the tail is two levels down"
