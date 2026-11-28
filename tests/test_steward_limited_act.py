"""A pilot must narrow both verdict paths without weakening the global gate."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from hub import config
from hub import repository as repo
from hub.models import ReviewVerdict, TaskReviewVerdict
from hub.services import steward_limited_act as limited
from hub.services import steward_shadow
from hub.services.lifecycle import record_review_verdict
from hub.services.statement_generation import statement_fingerprint
from hub.services.steward_applied import APPLIED, apply_self_approval
from hub.services.steward_changes_apply import apply_judge_changes_requests
from tests.steward_double_clean_support import build_case, verdict_events
from tests.test_steward_applied import _client_task, _grant_act
from tests.test_steward_judgement_apply import _judge_cr
from tests.test_steward_shadow import _project


async def _pilot(db, monkeypatch, project_id, task_id):
    plan_id = await _client_task(db, project_id)
    await repo.update_task(db, plan_id, status="open")
    plan = await repo.get_task(db, plan_id)
    approval_id = await repo.insert_event(
        db,
        kind="task_approved",
        task_id=plan_id,
        actor="human",
        payload={
            "force": False,
            "statement_generation": plan["statement_generation"],
            "statement_fingerprint": await statement_fingerprint(db, plan_id),
        },
    )
    task = await repo.get_task(db, task_id)
    await db.commit()
    now = datetime.now(timezone.utc)
    policy = limited.Pilot(
        project="default",
        plan_id=plan_id,
        approval_event_id=approval_id,
        starts_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
        tasks=[
            limited.TaskPin(
                task_id=task_id,
                statement_generation=task["statement_generation"],
                submission_generation=task["submission_generation"],
                sha=task["submission_sha"],
            )
        ],
        active_task_id=task_id,
    )
    monkeypatch.setattr(config, "STEWARD_ACT_PILOT", policy.model_dump_json())
    return policy


async def test_limited_act_is_checked_for_every_verdict_write(db, monkeypatch):
    case = await build_case(db, monkeypatch, "default", risk="R1")
    await _pilot(db, monkeypatch, case.project_id, case.task_id)
    outsider = await _client_task(db, case.project_id, risk_class="R1")
    for verdict in (ReviewVerdict.approved, ReviewVerdict.changes_requested):
        with pytest.raises(HTTPException, match=limited.REFUSED):
            await record_review_verdict(
                db,
                outsider,
                TaskReviewVerdict(
                    agent="steward",
                    verdict=verdict,
                    comments="Исправить подтверждённое замечание",
                ),
                expected_generation=1,
                applied_by_steward=True,
            )
    assert await verdict_events(db, outsider) == []
    result = await apply_self_approval(db, case.task_id, 1)
    assert result and result[0] == APPLIED
    assert len(await verdict_events(db, case.task_id)) == 1


async def test_poller_only_returns_the_active_task(db, monkeypatch):
    project_id = await _project(db, "default")
    active = await _client_task(db, project_id, risk_class="R1")
    other = await _client_task(db, project_id, risk_class="R1")
    _grant_act(monkeypatch)
    await _judge_cr(db, active)
    await _judge_cr(db, other)
    await _pilot(db, monkeypatch, project_id, active)
    assert await apply_judge_changes_requests(db) == 1
    assert len(await verdict_events(db, active)) == 1
    assert await verdict_events(db, other) == []


@pytest.mark.parametrize(
    "change",
    [
        "expiry",
        "too_long",
        "unknown_class",
        "higher_risk",
        "plan_changed",
        "submission_moved",
        "task_statement_changed",
        "unpinned_sha",
        "closed",
    ],
)
async def test_invalid_or_expired_scope_refuses(db, monkeypatch, change):
    project_id = await _project(db, "default")
    task_id = await _client_task(db, project_id, risk_class="R1")
    pilot = await _pilot(db, monkeypatch, project_id, task_id)
    assert await limited.refusal_for(db, task_id, 1) is None

    if change == "expiry":
        pilot.expires_at = pilot.starts_at + timedelta(seconds=1)
    elif change == "too_long":
        pilot.expires_at = pilot.starts_at + timedelta(days=8)
    elif change == "unknown_class":
        await repo.update_task(db, task_id, risk_class=None)
    elif change == "higher_risk":
        await repo.update_task(db, task_id, risk_class="R2")
    elif change == "plan_changed":
        await repo.update_task(db, pilot.plan_id, description="a different plan")
    elif change == "submission_moved":
        await repo.update_task(db, task_id, submission_generation=2)
    elif change == "task_statement_changed":
        await repo.update_task(db, task_id, statement_generation=2)
    elif change == "unpinned_sha":
        await repo.update_task(db, task_id, submission_sha="f" * 40)
    monkeypatch.setattr(
        config,
        "STEWARD_ACT_PILOT",
        "" if change == "closed" else pilot.model_dump_json(),
    )
    await db.commit()
    refused = await limited.refusal_for(db, task_id, 1)
    assert refused and refused[0] == limited.REFUSED


async def test_pilot_cannot_replace_global_act_grant(db, monkeypatch):
    project_id = await _project(db, "default")
    task_id = await _client_task(db, project_id, risk_class="R1")
    await _pilot(db, monkeypatch, project_id, task_id)

    async def not_granted(_db):
        return False

    monkeypatch.setattr(steward_shadow, "act_granted_now", not_granted)
    with pytest.raises(HTTPException, match="act_revoked"):
        await record_review_verdict(
            db,
            task_id,
            TaskReviewVerdict(
                agent="steward",
                verdict=ReviewVerdict.changes_requested,
                comments="Исправить подтверждённое замечание",
            ),
            expected_generation=1,
            applied_by_steward=True,
        )
    assert await verdict_events(db, task_id) == []


async def test_scope_rechecks_project_and_excludes_other_delegated_projects(
    db, monkeypatch
):
    from hub.services.steward_act_readiness import delegated_projects

    project_id = await _project(db, "default")
    other_id = await _project(db, "outside-pilot")
    task_id = await _client_task(db, project_id, risk_class="R1")
    await _pilot(db, monkeypatch, project_id, task_id)
    assert [row["slug"] for row in await delegated_projects(db)] == ["default"]
    await repo.update_task(db, task_id, project_id=other_id)
    await db.commit()
    refusal = await limited.refusal_for(db, task_id, 1)
    assert refusal and "проект вне" in refusal[1]

    monkeypatch.setattr(config, "STEWARD_ACT_PILOT", "off")
    assert await delegated_projects(db) == []
