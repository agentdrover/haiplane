"""A change of the project's CI contract reaches every gate and every reader (#1848).

The core cases live in tests/test_ci_trust.py (AC-1..AC-4, AC-6); here are the
gates that decide inside a transaction and the readers that must pick the
project's contract, on the same fake GitHub.
"""

from __future__ import annotations

import pytest

from hub import config
from hub import repository as repo
from hub.services import ci_contract, ci_trust, steward_evidence
from tests import test_ci_trust as base
from tests.ci_trust_support import (
    SAT_CONTRACT_B,
    SAT_SHA,
    install_fake_github,
    satellite_task,
    set_contract,
)


@pytest.fixture(autouse=True)
def _github(monkeypatch):
    base.GH = install_fake_github(monkeypatch)


@pytest.mark.parametrize(
    ("agent", "steward", "ac_mode", "expected"),
    [
        ("reviewer", False, "require", "ac_tests_not_green"),
        ("policy", False, "off", "ci_not_green"),
        ("steward", True, "off", "ci_not_green"),
    ],
)
async def test_a_contract_change_between_the_decision_and_the_apply_refuses_the_approval(
    db, monkeypatch, agent, steward, ac_mode, expected
):
    from fastapi import HTTPException

    from hub import config
    from hub.models import ReviewVerdict, TaskReviewVerdict
    from hub.services import lifecycle

    monkeypatch.setattr(config, "SDD_AC_TESTS", ac_mode)
    pid, task_id = await satellite_task(db)
    await base._post(db, task_id, base._sat_attempt(task_id))
    await repo.update_task(db, task_id, status="review")
    await db.commit()

    real_apply = lifecycle._apply_verdict

    async def the_contract_changes_first(state):
        await set_contract(db, pid, SAT_CONTRACT_B)
        return await real_apply(state)

    monkeypatch.setattr(lifecycle, "_apply_verdict", the_contract_changes_first)
    with pytest.raises(HTTPException) as refused:
        await lifecycle.record_review_verdict(
            db,
            task_id,
            TaskReviewVerdict(verdict=ReviewVerdict.approved, agent=agent),
            self_approved=True,
            applied_by_steward=steward,
            expected_generation=1 if agent != "reviewer" else None,
        )
    assert refused.value.status_code == 422
    assert expected in str(refused.value.detail)
    assert dict(await repo.get_task(db, task_id))["review_verdict"] in (None, "")


async def test_a_contract_change_before_the_done_apply_refuses_the_completion(
    db, monkeypatch
):
    from fastapi import HTTPException

    from hub import config
    from hub.models import TaskUpdateCreate
    from hub.services import lifecycle

    monkeypatch.setattr(config, "SDD_VALIDATION", "require")
    pid, task_id = await satellite_task(db)
    await base._post(db, task_id, base._sat_attempt(task_id))
    await repo.update_task(
        db, task_id, status="running", auto_review=0, validation_commands='["x"]'
    )
    await db.commit()
    assert (await base._evidence(db, task_id))[1] == "pass"
    base._forbid_network_in_transaction(db)

    real_prepare = lifecycle.prepare_ci_standing

    async def the_contract_changes_after_the_question(*args, **kwargs):
        prepared = await real_prepare(*args, **kwargs)
        await set_contract(db, pid, SAT_CONTRACT_B)
        return prepared

    monkeypatch.setattr(
        lifecycle, "prepare_ci_standing", the_contract_changes_after_the_question
    )
    with pytest.raises(HTTPException) as refused:
        await lifecycle.add_update(
            db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="done")
        )
    assert refused.value.status_code == 422
    assert "validation_failed" in str(refused.value.detail)
    assert dict(await repo.get_task(db, task_id))["status"] != "completed"


async def test_every_reader_picks_the_projects_contract(db):
    """One verified green satellite report; each reader answers by ITS contract, not the hub's."""
    from types import SimpleNamespace

    from hub.services import (
        inbox_decisions,
        review_brief,
        review_ci_gate,
        review_economy,
        review_evidence,
        steward_ci_wait,
        verdict_evidence,
    )
    from hub.services.ci_report import ci_report_state

    pid, sat = await satellite_task(db)
    await base._post(db, sat, base._sat_attempt(sat))
    task = dict(await repo.get_task(db, sat))
    await db.execute(
        "INSERT INTO submissions (task_id, generation, sha) VALUES (?, 1, ?)",
        (sat, SAT_SHA),
    )
    await db.commit()

    assert (await inbox_decisions._ci_ground(db, task)).startswith("CI зелёный на")
    assert (await steward_evidence._ci_fact(db, sat, SAT_SHA)).value["passed"] is True
    assert (await verdict_evidence._ci_part(db, sat, SAT_SHA))["trust"] == "verified"
    assert await ci_report_state(db, task) == ("current", "")
    state = await review_brief._ci_run_report_state(
        db, sat, SimpleNamespace(submission_sha=SAT_SHA)
    )
    assert state.trust == "verified"
    assert (await review_evidence.prepass_state(db, task)).state == "covered"
    assert await review_ci_gate.ci_forbids_review(db, task) == []
    assert (await steward_ci_wait.ci_wait_view(db, task)).waiting is False
    economy = await review_economy._red_ci_section(
        db, [{"task_id": sat, "generation": 1, "cost_cents": 0}]
    )
    assert economy["runs_on_green_ci"] == 1, economy

    # The contract changes: every reader stops counting the old verdict, and the
    # steward's wait sees an attempt that is not final under the new contract.
    await set_contract(db, pid, SAT_CONTRACT_B)
    assert not (await inbox_decisions._ci_ground(db, task)).startswith("CI зелёный на")
    assert (await verdict_evidence._ci_part(db, sat, SAT_SHA))["trust"] == "unverified"
    assert (await ci_report_state(db, task))[0] == "unknown"
    state = await review_brief._ci_run_report_state(
        db, sat, SimpleNamespace(submission_sha=SAT_SHA)
    )
    assert state.trust == "unverified"
    assert (await review_evidence.prepass_state(db, task)).state == "unknown"
    wait = await steward_ci_wait.ci_wait_view(db, task)
    assert wait.waiting is True and "не окончательна" in wait.state
    economy = await review_economy._red_ci_section(
        db, [{"task_id": sat, "generation": 1, "cost_cents": 0}]
    )
    assert economy["runs_on_green_ci"] == 0 and economy["runs_ci_undetermined"] == 1

    # The projection itself never shows a verdict made under another contract
    # as verified, even if that row was not asked again.
    rows = list(await repo.list_ci_report_attempts(db, sat, SAT_SHA))
    contract_b = await ci_contract.contract_for_task(db, sat)
    gh_repo = config.REPO_NAME
    standing = await ci_trust._project(
        db, dict(await repo.get_task(db, sat)), SAT_SHA, rows, None, gh_repo, contract_b
    )
    assert not standing.verified
    assert [r["trust_reason"] for r in ci_trust._under_contract(rows, contract_b)] == [
        "contract_changed"
    ]
