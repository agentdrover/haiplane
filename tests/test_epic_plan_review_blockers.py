"""Pilot review blockers through submission, poller and retention."""

# ruff: noqa: F811 - imported pytest fixtures
import asyncio
import json

import pytest

from hub import db as hub_db, repository as repo
from hub.services import (
    epic_plan,
    epic_plan_dispatch as dispatch,
    epic_plan_transport as transport,
    executor_orders,
    review_dispatch,
)
from tests.test_epic_plan_dispatch import (
    HEAD,
    _new_connection,
    _put_submission,
    _rows,
    keys,  # noqa: F401 - fixture dependency
    pilot,  # noqa: F401 - fixture dependency
    prepared,  # noqa: F401 - pytest fixture
)


@pytest.mark.parametrize("policy_enabled", [False, True])
@pytest.mark.parametrize("entry", ["submission", "poller"])
async def test_review_blocker_names_same_action_for_submission_and_poller(
    db, prepared, monkeypatch, policy_enabled, entry
):
    from hub.models import TaskSubmitReview
    from hub.services import lifecycle
    from tests.test_submit_gates import _install_fixed_tip_git

    project = await repo.get_project_by_slug(db, epic_plan.PROJECT)
    await repo.update_project(
        db,
        project["id"],
        gate_policy=json.dumps(
            {
                "executor_launch": "manual",
                **({"review": "dispatch"} if policy_enabled else {}),
            }
        ),
    )
    monkeypatch.setattr(transport, "review_transport", lambda: None)
    await repo.update_task(
        db,
        2377,
        branch="task-2377/pilot",
        status="running",
        assigned_agent="pilot-agent",
    )
    await db.commit()
    _install_fixed_tip_git(monkeypatch, HEAD)
    if entry == "submission":
        view = await lifecycle.submit_for_review(
            db, 2377, TaskSubmitReview(agent="pilot-agent", branch="task-2377/pilot")
        )
        assert view.status == "review"
    else:
        await _put_submission(db, 2377, 1, HEAD)
        await dispatch._follow_review(db)
    await dispatch._follow_review(db)
    await dispatch._follow_review(db)
    events = await hub_db.fetchall(
        db,
        "SELECT payload FROM events WHERE task_id=2377 AND kind='epic_plan_review_blocked'",
    )
    [event] = events
    reason, recipient, action = (
        (
            "pilot_metered_transport_unavailable",
            "operator",
            "restore_metered_review_transport",
        )
        if policy_enabled
        else (
            "pilot_review_policy_disabled",
            "project_owner",
            "enable_independent_review",
        )
    )
    assert json.loads(event["payload"]) == {
        "generation": 1,
        "sha": HEAD,
        "reason": reason,
        "recipient": recipient,
        "next_action": action,
    }
    from hub.services.verdict_route import verdict_route

    route = await verdict_route(db, 2377)
    assert route.code == reason and route.decider == "none"
    assert route.reason == f"{recipient}: {action}"
    updates = await repo.get_task_updates(db, 2377)
    assert all(
        "к человеку" not in row["content"]
        and "Вердикт остаётся человеку" not in row["content"]
        for row in updates
    )
    assert not await _rows(db, "epic_plan_escalations")
    assert not await _rows(db, "review_dispatches")


async def test_review_waits_for_trusted_ci_without_owner_escalation(db, prepared):
    order, why = await dispatch.start_next(db)
    assert order is not None, why
    await _put_submission(db, 2377, 1, HEAD)
    await dispatch._follow_review(db)
    await dispatch._follow_review(db)
    [notice] = await hub_db.fetchall(
        db,
        "SELECT payload FROM events WHERE task_id=2377 AND kind='epic_plan_review_blocked'",
    )
    assert json.loads(notice["payload"])["next_action"] == "wait_for_trusted_ci_on_sha"
    assert (await repo.get_task(db, 2377))["status"] == "review"
    assert not await _rows(db, "epic_plan_escalations")


async def test_busy_slot_waits_without_owner_escalation(db, prepared):
    owner, _, _, _ = prepared
    project = await repo.get_project_by_slug(db, epic_plan.PROJECT)
    await db.execute(
        "INSERT INTO tasks(id,title,status,project_id) VALUES (2390,'Other work','open',?)",
        (project["id"],),
    )
    await db.commit()
    occupied = await executor_orders.launch_on_slot(
        db,
        project,
        host="k8",
        task_id=2390,
        issuer_principal_id=owner,
        issuer_human=True,
    )
    assert occupied.launched, occupied.reason
    for _ in range(2):
        order, reason = await dispatch.start_next(db)
        assert order is None and reason.startswith(executor_orders.REASON_SLOT_BUSY)
    assert not await _rows(db, "epic_plan_escalations")
    assert (await repo.get_task(db, 2377))["status"] == "open"


async def test_ordinary_project_keeps_review_policy_message(db, prepared):
    project = await repo.get_project_by_slug(db, "default")
    await db.execute(
        """INSERT INTO tasks(id,title,status,project_id,submission_generation,submission_sha,branch)
           VALUES (2390,'Other work','review',?,1,?,'task-2390/pilot')""",
        (project["id"], HEAD),
    )
    await db.commit()
    assert not await review_dispatch.maybe_dispatch_review(db, 2390)
    updates = await repo.get_task_updates(db, 2390)
    assert any("Вердикт остаётся человеку" in row["content"] for row in updates)
    assert not await hub_db.fetchall(
        db,
        "SELECT id FROM events WHERE task_id=2390 AND kind='epic_plan_review_blocked'",
    )


async def test_review_notice_survives_event_retention(
    db, db_dsn, prepared, monkeypatch
):
    monkeypatch.setattr(transport, "review_transport", lambda: None)
    await _put_submission(db, 2377, 1, HEAD)
    async with _new_connection(db_dsn) as other:
        await asyncio.gather(
            dispatch._follow_review(db), dispatch._follow_review(other)
        )
    sql = "SELECT id FROM events WHERE task_id=2377 AND kind='epic_plan_review_blocked'"
    [notice] = await hub_db.fetchall(db, sql)
    await db.execute(
        "UPDATE events SET created_at=datetime('now','-15 days') WHERE id=?",
        (notice["id"],),
    )
    await db.execute("UPDATE role_orders_cursor SET cursor_event_id=?", (notice["id"],))
    await db.execute(
        "UPDATE owner_minutes_state SET cursor_event_id=?", (notice["id"],)
    )
    assert await repo.prune_events(db) >= 1
    await db.commit()
    assert not await hub_db.fetchall(db, sql)
    before = await repo.get_task_updates(db, 2377)
    async with _new_connection(db_dsn) as restarted:
        await dispatch._follow_review(restarted)
    assert not await hub_db.fetchall(db, sql)
    assert await repo.get_task_updates(db, 2377) == before
    assert len(await _rows(db, "epic_plan_review_notice_keys")) == 1
