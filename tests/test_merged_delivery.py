"""Reconcile only a proved merge of the current, approved, checked PR."""

import json
from unittest.mock import AsyncMock

import pytest

from hub import repository as repo
from hub.integrations.forge.github import GitHubForge
from hub.integrations.protocols import CIProbeOutcome
from hub.services import ci_contract, ci_trust, delivery_state, orchestration
from tests.test_pair_merge_gate import _approved_pair_task, _git, _report_done

HEAD = "a" * 40
MERGE = "b" * 40
BASE_TIP = "c" * 40
NAME = "agentdrover/home-infra"
ATTEMPT = "ci:44480044461:1"


@pytest.fixture
async def merged(db, monkeypatch):
    git = _git(CIProbeOutcome.unavailable)
    git.pr_state = AsyncMock(return_value="merged")
    task_id = await _approved_pair_task(db)
    project_id = await repo.create_project(
        db,
        slug="home-infra",
        name="Home infrastructure",
        repo_name=NAME,
        default_branch="main",
    )
    await repo.update_task(db, task_id, project_id=project_id, submission_sha=HEAD)
    task = dict(await repo.get_task(db, task_id))
    contract = await ci_contract.contract_for_task(db, task_id)
    await repo.upsert_ci_run_report(
        db,
        task_id=task_id,
        head_sha=HEAD,
        ac_results="[]",
        validation_status="pass",
        validation_log="",
        reason="",
        reported_by="ci",
        checks=json.dumps(dict.fromkeys(contract.ordered_checks(), "pass")),
        provenance=json.dumps({"run_conclusion": "success"}),
        attempt_key=ATTEMPT,
        trust="verified",
    )
    await ci_contract.stamp_projection(db, task_id, HEAD, contract.fingerprint)
    await db.commit()
    fresh = AsyncMock(
        return_value=ci_trust.Standing("verified", "", ATTEMPT, version=0)
    )
    monkeypatch.setattr(ci_trust, "refresh_quietly", fresh)
    pr = {
        "number": 77,
        "state": "closed",
        "merged": True,
        "merged_at": "2026-11-19T09:20:26Z",
        "merge_commit_sha": MERGE,
        "merged_by": {"login": "operator"},
        "head": {"sha": HEAD, "ref": task["branch"], "repo": {"full_name": NAME}},
        "base": {"sha": BASE_TIP, "ref": "main", "repo": {"full_name": NAME}},
    }
    comparison = {"status": "ahead", "merge_base_commit": {"sha": MERGE}}
    facts = {
        f"repos/{NAME}/pulls/77": pr,
        f"repos/{NAME}/branches/main": {"name": "main", "commit": {"sha": BASE_TIP}},
        f"repos/{NAME}/compare/{MERGE}...{BASE_TIP}": comparison,
    }

    async def get(_self, endpoint, *, repo):
        return facts[endpoint], False

    monkeypatch.setattr(GitHubForge, "_api_json", get)
    return task, project_id, git, pr, comparison, fresh


async def test_done_reconciles_exact_merged_pull_request(db, merged):
    task, project_id, git, _, _, _ = merged

    await _report_done(db, task["id"])
    completed = dict(await repo.get_task(db, task["id"]))
    assert completed["status"] == "completed"
    assert completed["submission_generation"] == task["submission_generation"]
    assert await orchestration.merge_before_completion(db, completed) == (
        True,
        f"reconciled_external_merge: {MERGE}",
    )
    git.check_pr_ci.assert_not_awaited()
    git.merge_pr.assert_not_awaited()
    git.pull_main.assert_not_awaited()
    records = await db.execute_fetchall("SELECT * FROM pipeline_merges")
    assert len(records) == 1
    row = records[0]
    assert row["merge_sha"] == MERGE
    assert row["merged_at"] == "2026-11-19 09:20:26"
    evidence = json.loads(row["reconciliation"])
    assert evidence["github"]["merged_by"] == "operator"
    assert evidence["github"]["base_tip"] == BASE_TIP
    assert evidence["pin"]["head"] == HEAD
    assert evidence["ci_attempt"] == ATTEMPT
    assert MERGE not in await repo.known_pipeline_shas(db, project_id, gate_only=True)
    assert not await repo.pipeline_merge_recorded(db, task["id"], 77, gate_only=True)
    delivered = await delivery_state.task_delivery(db, completed)
    assert delivered["delivery_path"] == "outside_gate"
    events = await db.execute_fetchall(
        "SELECT id FROM events WHERE kind='reconciled_external_merge'"
    )
    assert len(events) == 1


@pytest.mark.parametrize(
    ("problem", "reason"),
    [
        ("head", "approved_head_mismatch"),
        ("repository", "base_mismatch"),
        ("base", "base_mismatch"),
        ("sha", "merge_identity_unavailable"),
        ("ancestry", "merge_not_proven_in_base"),
        ("approval", "current_independent_approval_not_proven"),
        ("self_approval", "current_independent_approval_not_proven"),
        ("unknown_ci", "exact_ci_not_verified"),
        ("stale_attempt", "exact_ci_not_green"),
        ("missing_check", "exact_ci_not_green"),
        ("racing_attempt", "exact_ci_not_green"),
    ],
)
async def test_merged_without_required_evidence_cannot_deliver(
    db, merged, problem, reason
):
    task, _, git, pr, comparison, fresh = merged
    if problem == "head":
        pr["head"]["sha"] = "d" * 40
    elif problem == "repository":
        pr["base"]["repo"]["full_name"] = "other/home-infra"
    elif problem == "base":
        pr["base"]["ref"] = "other"
    elif problem == "sha":
        pr["merge_commit_sha"] = ""
    elif problem == "ancestry":
        comparison["status"] = "diverged"
    elif problem == "approval":
        task["review_verdict_generation"] = 0
    elif problem == "self_approval":
        task["review_self_approved"] = 1
    elif problem == "unknown_ci":
        fresh.return_value = None
    elif problem == "stale_attempt":
        fresh.return_value = ci_trust.Standing("verified", "", "ci:2:2", version=0)
    elif problem == "missing_check":
        await db.execute("UPDATE ci_run_reports SET checks='{}'")
    elif problem == "racing_attempt":
        await repo.bump_ci_state_version(db, task["id"], HEAD)
    await db.commit()

    ok, detail = await orchestration.merge_before_completion(db, task)

    assert not ok
    assert reason in detail
    assert not await db.execute_fetchall("SELECT * FROM pipeline_merges")
    git.merge_pr.assert_not_awaited()


async def test_reconciliation_rechecks_revision_after_network(db, merged):
    task, _, git, _, _, fresh = merged

    async def approve_different_revision(*_args):
        await repo.update_task(
            db, task["id"], review_verdict_revision=task["review_verdict_revision"] + 1
        )
        await db.commit()
        return fresh.return_value

    fresh.side_effect = approve_different_revision
    ok, detail = await orchestration.merge_before_completion(db, task)

    assert not ok
    assert "approval_or_project_changed" in detail
    assert not await db.execute_fetchall("SELECT * FROM pipeline_merges")
    git.merge_pr.assert_not_awaited()


async def test_open_pr_still_uses_existing_ci_gate(db, merged):
    task, _, git, _, _, fresh = merged
    git.pr_state.return_value = "open"

    ok, detail = await orchestration.merge_before_completion(db, task)

    assert not ok
    assert "ci_unavailable" in detail
    fresh.assert_not_awaited()
    git.check_pr_ci.assert_awaited_once()
    git.merge_pr.assert_not_awaited()
