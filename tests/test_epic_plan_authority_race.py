"""A stop committed by another connection takes precedence in live admission."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from hub import config, repository as repo
from hub.epic_plan_view import PlanTerminate
from hub.services import admin, epic_plan
from hub.services import epic_plan_dispatch as dispatch
from hub.services import epic_plan_gates as gates
from tests.test_epic_plan_dispatch import (
    HEAD,
    _new_connection,
    _put_submission,
    _rows,
    keys,  # noqa: F401 - fixture dependency
    pilot,  # noqa: F401 - fixture dependency
    prepared,  # noqa: F401 - fixture dependency
)


@pytest.mark.parametrize("terminal", ["revoked", "stopped"])
@pytest.mark.parametrize("read", ["snapshot", "permissions"])
@pytest.mark.parametrize("route", ["start", "review"])
async def test_live_admission_uses_stop_committed_during_reads(
    db, db_dsn, prepared, monkeypatch, terminal, read, route
):
    owner, _, _, grant = prepared
    active = await epic_plan.active_decision(db)
    assert active.state == "active"
    spy = AsyncMock(wraps=grant)
    monkeypatch.setattr(dispatch, "get_grant", spy)
    reached, resume = asyncio.Event(), asyncio.Event()
    module, method = (
        (epic_plan, "snapshot")
        if read == "snapshot"
        else (admin, "get_principal_permissions")
    )
    original_read = getattr(module, method)

    async def interrupted_read(connection, *args):
        result = await original_read(connection, *args)
        if connection is db and not reached.is_set():
            reached.set()
            await resume.wait()
        return result

    monkeypatch.setattr(module, method, interrupted_read)
    if route == "review":
        task = await _put_submission(db, 2377, 1, HEAD)
        project = await repo.get_project_by_slug(db, epic_plan.PROJECT)
        call = gates.review_blocker(db, task, project)
    else:
        await repo.update_task(
            db, 2377, status="running", review_verdict="changes_requested"
        )
        await db.commit()
        call = dispatch.start_next(db)
    identity = config.TokenIdentity(
        "plan-owner",
        principal_id=owner,
        permissions=frozenset({"tasks.human_gate"}),
        auth_source="db_api_key",
    )
    request = PlanTerminate(
        state=terminal, reason="Owner ended the test pilot", version=active.version
    )
    async with _new_connection(db_dsn) as second, asyncio.TaskGroup() as group:
        reader = group.create_task(call)
        await asyncio.wait_for(reached.wait(), timeout=5)
        assert not db.in_transaction
        ended = await epic_plan.terminate(second, epic_plan.EPIC_ID, request, identity)
        assert ended.state == terminal
        resume.set()

    result = reader.result()
    spy.assert_not_awaited()
    if route == "review":
        assert (result.reason, result.recipient, result.next_action) == (
            f"plan_{terminal}",
            "plan_owner",
            "check_plan_authorization",
        )
    else:
        assert result == (None, f"plan_{terminal}")
    assert not await _rows(db, "epic_plan_work")
    assert not await _rows(db, "executor_orders")
    assert not await _rows(db, "review_dispatches")
