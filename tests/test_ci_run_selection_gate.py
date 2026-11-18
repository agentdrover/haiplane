from unittest.mock import AsyncMock

import pytest

from hub import repository as repo
from hub import services
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
from hub.models import TaskCreate
from hub.services import delivery_ci
from tests.test_ci_run_selection import HEAD, REPO
from tests.test_pair_merge_gate import _approved_pair_task, _drain_pair_delivery, _git


@pytest.mark.parametrize("blocked_by", ["approval", "head", "human"])
async def test_ci_selection_preserves_review_sha_and_human_decision_gates(
    db, monkeypatch, blocked_by
):
    g = _git(CIProbeOutcome.passed, merged=True)
    gate = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.passed, "ci_equivalent_native_run")
    )
    monkeypatch.setattr(delivery_ci, "check_task", gate)
    if blocked_by == "approval":
        task_id = (await services.create_task(db, TaskCreate(title="Unapproved"))).id
        await repo.update_task(db, task_id, pr_number=229)
    else:
        task_id = await _approved_pair_task(db)
        if blocked_by == "head":
            await repo.update_task(db, task_id, submission_sha=HEAD)
            monkeypatch.setattr(
                "hub.services.lifecycle.resolve_branch_tip",
                AsyncMock(return_value=("b" * 40, "")),
            )
        else:
            await repo.update_task(db, task_id, status="needs_decision")
    await db.commit()
    before = dict(await repo.get_task(db, task_id))
    await _drain_pair_delivery(db)
    after = dict(await repo.get_task(db, task_id))
    g.merge_pr.assert_not_awaited()
    gate.assert_not_awaited()
    for field in ("submission_generation", "submission_sha", "review_verdict"):
        assert before[field] == after[field]
    if blocked_by == "human":
        assert after["status"] == "needs_decision"


async def test_policy_race_cannot_return_green(monkeypatch):
    read = AsyncMock(side_effect=["before", "after"])
    monkeypatch.setattr(delivery_ci, "_project_fingerprint", read)
    g = _git(CIProbeOutcome.passed)
    result = await delivery_ci.check_task(None, 2372, 229, {"gh_repo": REPO})
    assert result.outcome is CIProbeOutcome.unavailable
    assert result.reason == "ci_contract_changed"
    assert g.check_pr_ci.await_args.kwargs["contract_fingerprint"] == "before"
