"""Recovery for a completed delivery blocked by stale-base CI evidence (#2393)."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from hub import repository as repo
from hub.db import connect
from hub.integrations import protocols
from hub.integrations.forge.github import GitHubForge
from hub.integrations.forge.github_ci_selection import PILOT_CONTRACT
from hub.integrations.protocols import (
    BaseFreshness,
    BaseFreshnessState,
    BranchUpdateOutcome,
    CIProbeOutcome,
    CIProbeResult,
)
from hub.models import TaskDecide
from hub.services import delivery_ci, lifecycle, orchestration
from hub.services.delivery_state import scan_completed_deliveries
from tests.accepted_delivery_retry_support import (
    APPROVED,
    FAILED,
    PASSED,
    PENDING,
    TIP,
    accepted_task,
)
from tests.test_ci_run_selection import BASE, HEAD, HEAD_REF, NEW, REPO, Evidence

PR_BASE = "1" * 40
LIVE_BASE = "2" * 40


def moved_base_probe(branch):
    details = "base branch changed or is unreadable"
    # Keep the reproducer runnable on the old gate, before typed evidence exists.
    change_type = getattr(protocols, "CIBaseChange", None)
    if change_type is None:
        return CIProbeResult(
            CIProbeOutcome.unavailable, "ci_equivalence_unproved", details
        )
    return CIProbeResult(
        CIProbeOutcome.unavailable,
        "ci_equivalence_unproved",
        details,
        base_change=change_type(
            pr_number=77,
            head_ref=branch,
            head_sha=APPROVED,
            base_ref="develop",
            pinned_base_sha=PR_BASE,
            current_base_sha=LIVE_BASE,
        ),
    )


async def _deliver(rig, entry):
    if entry == "decide":
        await repo.update_task(rig.db, rig.task_id, status="needs_decision")
        await rig.db.commit()
        await lifecycle.decide_task(
            rig.db, rig.task_id, TaskDecide(action="accept", pr_disposition="deliver")
        )
    else:
        await repo.record_delivery_discrepancy(
            rig.db,
            task_id=rig.task_id,
            state="pr_open",
            reason="not delivered",
            pr_number=77,
            delivery_path="",
            disposition="deliver",
        )
        await rig.db.commit()
        with pytest.raises(lifecycle.RegistryDeliveryRefused) as refusal:
            await lifecycle.deliver_from_registry(rig.db, rig.task_id, by="owner")
        return refusal.value.reason
    return await rig.feed()


def _fresh(rig, sha, *, behind=False):
    state = BaseFreshnessState.behind if behind else BaseFreshnessState.current
    identity = (
        {"base_ref": "develop", "base_sha": LIVE_BASE, "head_ref": rig.delivery_branch}
        if hasattr(BaseFreshness, "head_ref")
        else {}
    )
    return BaseFreshness(state, sha, 2 if behind else 0, **identity)


def _move_head(rig, sha):
    rig.head_moved_to(sha)
    rig.git.pr_base_freshness.return_value = _fresh(rig, sha)


async def _rig(db, monkeypatch):
    rig = await accepted_task(db, monkeypatch)
    orchestration.project_git_context.return_value.update(gh_repo=REPO, forge="github")
    monkeypatch.setattr(
        delivery_ci, "_project_fingerprint", AsyncMock(return_value="fp")
    )
    rig.delivery_branch = (await rig.task())["branch"]
    rig.ci(moved_base_probe(rig.delivery_branch))
    rig.git.pr_base_freshness = AsyncMock(
        return_value=_fresh(rig, APPROVED, behind=True)
    )
    rig.git.request_ci_run = AsyncMock()
    rig.git.mark_pr_ready = AsyncMock()
    return rig


@pytest.mark.parametrize("entry", ["decide", "registry"])
async def test_proven_behind_arms_existing_delivery_retry(db, monkeypatch, entry):
    rig = await _rig(db, monkeypatch)
    reason = await _deliver(rig, entry)

    assert "base_updated" in reason
    rig.git.update_pr_branch.assert_awaited_once()
    assert rig.git.update_pr_branch.await_args.args == (77, APPROVED)
    assert (await rig.retry_row())["state"] == "armed"
    assert (await rig.task())["status"] == "completed"
    rig.git.merge_pr.assert_not_awaited()
    assert rig.posts == 0
    rig.git.request_ci_run.assert_not_awaited()
    rig.git.mark_pr_ready.assert_not_awaited()

    # A second human entry before GitHub publishes the new head cannot spend
    # another update against the old head or duplicate the durable retry.
    ok, reason, _ = await rig.deliver()
    assert (
        not ok
        and reason == f"ci_{CIProbeOutcome.pending.value}: waiting_for_updated_head"
    )
    rig.git.update_pr_branch.assert_awaited_once()
    assert len(await rig.retry_rows()) == 1


@pytest.mark.parametrize(
    "block,expected",
    [
        ("no_proof", "ci_unavailable"),
        ("compare_unknown", "ci_unavailable"),
        ("compare_current", "ci_unavailable"),
        ("compare_wrong_head", "ci_unavailable"),
        ("compare_wrong_base", "ci_unavailable"),
        ("stack_unknown", "stack_unknown"),
        ("stacked", "stacked_base"),
        ("red_ci", "ci_fail"),
        ("no_tip_observed", "ci_unavailable"),
        ("live_approval_lost", "ci_unavailable"),
        ("stale_approval", "no_approved_review"),
        ("conflict", "base_update_conflict"),
    ],
)
async def test_ambiguous_or_ineligible_base_recovery_fails_closed(
    db, monkeypatch, block, expected
):
    rig = await _rig(db, monkeypatch)
    observed = _fresh(rig, APPROVED, behind=True)
    if block == "no_proof":
        rig.ci(
            CIProbeResult(
                CIProbeOutcome.unavailable,
                "ci_equivalence_unproved",
                "base branch changed or is unreadable",
            )
        )
    elif block.startswith("compare_"):
        changes = {
            "compare_unknown": {"state": BaseFreshnessState.unknown},
            "compare_current": {"state": BaseFreshnessState.current},
            "compare_wrong_head": {"head_sha": "c" * 40},
            "compare_wrong_base": {"base_sha": "c" * 40},
        }[block]
        if "base_sha" not in changes or hasattr(BaseFreshness, "base_sha"):
            rig.git.pr_base_freshness.return_value = replace(observed, **changes)
    elif block.startswith("stack"):
        assessment = (
            orchestration.StackAssessment(
                outcome=orchestration.STACK_STACKED,
                relation=orchestration.git_ops_mod.STACK_ANCESTRY_HEAD_IS_DESCENDANT,
                base_task_id=99,
                base_task_branch="task-99/parent",
                base_can_deliver_itself=True,
            )
            if block == "stacked"
            else orchestration.StackAssessment(
                outcome=orchestration.STACK_UNKNOWN, reason="probe_unsupported"
            )
        )
        monkeypatch.setattr(
            orchestration, "assess_branch_stacking", AsyncMock(return_value=assessment)
        )
    elif block == "red_ci":
        rig.ci(FAILED)
    elif block == "no_tip_observed":
        rig.git.head_sha.return_value = ""
    elif block == "stale_approval":
        await repo.update_task(db, rig.task_id, submission_generation=9)
        await db.commit()
    elif block == "live_approval_lost":

        async def observe_then_revoke(*args, **kwargs):
            await repo.update_task(db, rig.task_id, submission_generation=9)
            await db.commit()
            return observed

        rig.git.pr_base_freshness.side_effect = observe_then_revoke
    elif block == "conflict":
        rig.git.update_pr_branch.return_value = (BranchUpdateOutcome.conflict, "app.py")

    assert (await _deliver(rig, "registry")).startswith(expected)
    if block == "conflict":
        rig.git.update_pr_branch.assert_awaited_once()
    else:
        rig.git.update_pr_branch.assert_not_awaited()
    assert await rig.retry_rows() == []
    rig.git.merge_pr.assert_not_awaited()
    rig.git.request_ci_run.assert_not_awaited()
    assert rig.posts == 0


@pytest.mark.parametrize("author_changed", [False, True])
async def test_delivery_requires_fresh_green_after_base_update(
    db, monkeypatch, author_changed
):
    rig = await _rig(db, monkeypatch)
    assert (await _deliver(rig, "registry")).startswith("base_updated")

    # CI from the old head is still unavailable for delivery, even though its
    # jobs were proved. The new head does not exist yet.
    await scan_completed_deliveries(db)
    assert (await rig.retry_row())["attempts"] == 0
    rig.git.merge_pr.assert_not_awaited()

    _move_head(rig, TIP)
    rig.ci(PENDING)
    await scan_completed_deliveries(db)
    assert (await rig.retry_row())["attempts"] == 0
    rig.git.merge_pr.assert_not_awaited()

    # A green result for the old head, and a legacy green without commit
    # evidence, both leave the retry armed on the new head.
    for result in (PASSED, _green_on(rig, APPROVED)):
        rig.ci(result)
        await scan_completed_deliveries(db)
        assert (await rig.retry_row())["attempts"] == 0
        rig.git.merge_pr.assert_not_awaited()

    if author_changed:

        async def changed_diff(workspace, base, sha):
            return "@@ -1,0 +2 @@\n+" + sha + "\n"

        rig.git.branch_diff.side_effect = changed_diff
    rig.ci(_green_on(rig, TIP))
    await scan_completed_deliveries(db)
    await scan_completed_deliveries(db)
    assert rig.git.check_pr_ci.await_args.kwargs["target"] == _target(rig, TIP)

    if author_changed:
        rig.git.merge_pr.assert_not_awaited()
        assert (await rig.retry_row())["outcome"] == "stale_approval"
    else:
        rig.git.merge_pr.assert_awaited_once()
        assert rig.git.merge_pr.await_args.kwargs["expected_head_sha"] == TIP
        assert (await rig.retry_row())["outcome"] == "delivered"
    rig.git.update_pr_branch.assert_awaited_once()
    rig.git.request_ci_run.assert_not_awaited()
    assert rig.posts == 0


@pytest.mark.parametrize(
    "scenario,outcome",
    [
        ("complete", CIProbeOutcome.unavailable),
        ("branch_unreadable", CIProbeOutcome.unavailable),
        ("branch_moved_twice", CIProbeOutcome.unavailable),
        ("jobs_unreadable", CIProbeOutcome.unavailable),
        ("real_failure", CIProbeOutcome.failed),
        ("independent_failure", CIProbeOutcome.failed),
    ],
)
async def test_equivalence_exposes_base_change_only_after_other_ci_evidence_passes(
    monkeypatch, scenario, outcome
):
    class StaleEvidence(Evidence):
        async def api(self, endpoint, **kwargs):
            if endpoint == f"repos/{REPO}/branches/main":
                sha = LIVE_BASE
                if scenario == "branch_unreadable":
                    sha = None
                elif scenario == "branch_moved_twice" and self.pr_reads > 1:
                    sha = "3" * 40
                return {"protected": False, "commit": {"sha": sha}}, False
            return await super().api(endpoint, **kwargs)

    evidence = StaleEvidence()
    if scenario == "jobs_unreadable":
        evidence.missing.add(
            (f"actions/runs/{evidence.native['id']}/attempts/1/jobs", "1")
        )
    elif scenario == "real_failure":
        evidence.native["conclusion"] = "failure"
    elif scenario == "independent_failure":
        evidence.statuses.append(
            {"context": "independent", "state": "failure", "sha": HEAD}
        )

    result = await evidence.probe(monkeypatch)
    assert result.outcome is outcome
    change = getattr(result, "base_change", None)
    if scenario == "complete":
        assert change is not None
        assert (change.pr_number, change.head_ref, change.head_sha) == (
            229,
            HEAD_REF,
            HEAD,
        )
        assert (change.base_ref, change.pinned_base_sha, change.current_base_sha) == (
            "main",
            BASE,
            LIVE_BASE,
        )
    else:
        assert change is None


async def test_github_freshness_reports_compared_base_not_merge_base(monkeypatch):
    replies = AsyncMock(
        side_effect=[
            (
                0,
                json.dumps(
                    {"baseRefName": "main", "headRefName": HEAD_REF, "headRefOid": HEAD}
                ),
                "",
            ),
            (0, json.dumps({"behind_by": 2, "base_sha": LIVE_BASE}), ""),
        ]
    )
    monkeypatch.setattr("hub.integrations.forge.github._gh", replies)

    observed = await GitHubForge().pr_base_freshness(229, gh_repo=REPO)

    assert observed.state is BaseFreshnessState.behind
    assert observed.head_sha == HEAD
    assert getattr(observed, "base_ref", None) == "main"
    assert getattr(observed, "head_ref", None) == HEAD_REF
    assert getattr(observed, "base_sha", None) == LIVE_BASE
    replies.assert_awaited()
    assert replies.await_args.args[1] == f"repos/{REPO}/compare/main...{HEAD}"
    assert ".base_commit.sha" in replies.await_args.args[3]


def _target(rig, sha):
    return protocols.CIPrTarget(sha, rig.delivery_branch, "develop")


def _bound(result, target):
    return replace(result, target=target)


def _green_on(rig, sha):
    if not hasattr(protocols, "CIPrTarget"):
        return PASSED
    return _bound(PASSED, _target(rig, sha))


@pytest.mark.parametrize(
    "head,state,expected",
    [
        (HEAD, "green", CIProbeOutcome.passed),
        (HEAD, "absent", CIProbeOutcome.pending),
        (HEAD, "pending", CIProbeOutcome.pending),
        (HEAD, "failed", CIProbeOutcome.failed),
        (HEAD, "wrong_red_sha", CIProbeOutcome.unavailable),
        (HEAD, "late_head", CIProbeOutcome.unavailable),
        (HEAD, "retargeted", CIProbeOutcome.unavailable),
        ("5" * 40, "green", CIProbeOutcome.unavailable),
    ],
)
async def test_scoped_exact_probe_never_uses_unbound_legacy_checks(
    monkeypatch, head, state, expected
):
    e = Evidence()
    # A first-attempt native pull_request run: normally legacy selection
    # handles this shape, but a recovered delivery needs the SHA in evidence.
    run = e.native
    run.update(event="pull_request", pull_requests=[{"number": 229}])
    if state == "pending":
        run.update(status="in_progress", conclusion=None)
    elif state in ("failed", "wrong_red_sha", "late_head"):
        run["conclusion"] = "failure"
        if state == "wrong_red_sha":
            run["head_sha"] = "5" * 40
        elif state == "late_head":
            e.switch_head = True
    elif state == "retargeted":
        e.pr["base"]["ref"] = "release-pilot"
    e.runs = [] if state == "absent" else [run]
    e.checks = [e._as_check(run, job) for job in e.jobs[NEW]]
    forge = GitHubForge()
    monkeypatch.setattr(forge, "_api_json", e.api)
    primary = AsyncMock(return_value=(0, '[{"name":"tests","state":"SUCCESS"}]', ""))
    monkeypatch.setattr("hub.integrations.forge.github._gh", primary)
    target_type = getattr(protocols, "CIPrTarget", None)
    target = target_type(head, HEAD_REF, "main") if target_type else None
    exact = {"target": target} if target is not None else {}
    result = await forge.check_pr_ci(
        229, gh_repo=REPO, contract_fingerprint=PILOT_CONTRACT, **exact
    )

    assert result.outcome is expected
    if expected in (CIProbeOutcome.passed, CIProbeOutcome.failed):
        assert target is not None and result.target == target
        assert e.pr_reads >= 2
    primary.assert_not_awaited()


async def test_concurrent_delivery_updates_the_proven_old_head_once(
    db, db_dsn, monkeypatch
):
    rig = await _rig(db, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def update(*args, **kwargs):
        assert not db.in_transaction, (
            "the forge call runs outside the SQLite write lock"
        )
        entered.set()
        await release.wait()
        return BranchUpdateOutcome.updated, "accepted"

    rig.git.update_pr_branch.side_effect = update
    other = await connect(db_dsn)
    first = asyncio.create_task(rig.deliver())
    signal = asyncio.create_task(entered.wait())
    try:
        # Merge-base would refuse before touching the forge; do not hang the
        # baseline waiting for an event that only the fixed code can signal.
        await asyncio.wait({first, signal}, return_when=asyncio.FIRST_COMPLETED)
        assert entered.is_set()
        ok, reason, _ = await lifecycle.deliver_on_disposition(
            other, rig.task_id, "deliver", via="registry_deliver"
        )
        assert not ok and reason.startswith("base_update_pending")
        assert await rig.retry_rows() == []
    finally:
        release.set()
        signal.cancel()
        await asyncio.gather(first, signal, return_exceptions=True)
        await other.close()

    ok, reason, _ = first.result()
    assert not ok and reason.startswith("base_updated")
    rig.git.update_pr_branch.assert_awaited_once()
    assert len(await rig.retry_rows()) == 1


async def test_lost_update_response_never_arms_or_updates_another_head(db, monkeypatch):
    rig = await _rig(db, monkeypatch)

    async def update(pr, expected_head, **kwargs):
        assert expected_head == APPROVED
        _move_head(rig, TIP)
        return BranchUpdateOutcome.unavailable, "connection lost after send"

    rig.git.update_pr_branch.side_effect = update
    assert (await _deliver(rig, "registry")).startswith("base_update_retry")
    assert await rig.retry_rows() == []
    await _deliver(rig, "registry")
    rig.git.update_pr_branch.assert_awaited_once()
    rig.git.merge_pr.assert_not_awaited()
    rig.git.request_ci_run.assert_not_awaited()


async def test_genuine_red_on_updated_head_keeps_existing_stop_policy(db, monkeypatch):
    rig = await _rig(db, monkeypatch)
    assert (await _deliver(rig, "registry")).startswith("base_updated")
    _move_head(rig, TIP)
    rig.ci(FAILED)
    await scan_completed_deliveries(db)
    assert (await rig.retry_row())["attempts"] == 0, "unbound red cannot stop the retry"
    assert (await rig.retry_row())["state"] == "armed"

    rig.ci(_bound(FAILED, _target(rig, APPROVED)))
    await scan_completed_deliveries(db)
    assert (await rig.retry_row())["attempts"] == 0, (
        "old head's red cannot stop the retry"
    )
    assert (await rig.retry_row())["state"] == "armed"

    rig.ci(_bound(FAILED, _target(rig, TIP)))
    await scan_completed_deliveries(db)

    saved = await rig.retry_row()
    assert (saved["state"], saved["outcome"]) == ("closed", "ci_failed")
    rig.git.merge_pr.assert_not_awaited()
    rig.git.request_ci_run.assert_not_awaited()
    assert rig.posts == 0


@pytest.mark.parametrize("phase", ["after_ci", "before_merge"])
async def test_retarget_at_same_commit_never_merges_recovered_pr(
    db, monkeypatch, phase
):
    rig = await _rig(db, monkeypatch)
    assert (await _deliver(rig, "registry")).startswith("base_updated")
    _move_head(rig, TIP)
    rig.ci(_green_on(rig, TIP))
    expected = _fresh(rig, TIP)
    retargeted = replace(expected, base_ref="release-pilot")
    observations = [expected, retargeted] if phase == "before_merge" else [retargeted]
    rig.git.pr_base_freshness.side_effect = observations

    await scan_completed_deliveries(db)

    assert (await rig.retry_row())["state"] == "armed"
    assert rig.git.pr_base_freshness.await_count == len(observations)
    rig.git.merge_pr.assert_not_awaited()
    rig.git.update_pr_branch.assert_awaited_once()


async def test_head_moved_after_exact_green_never_updates_the_unchecked_head(
    db, monkeypatch
):
    rig = await _rig(db, monkeypatch)
    assert (await _deliver(rig, "registry")).startswith("base_updated")
    _move_head(rig, TIP)
    rig.ci(_green_on(rig, TIP))
    rig.git.pr_base_freshness.return_value = _fresh(rig, "foreign-push", behind=True)

    await scan_completed_deliveries(db)

    assert (await rig.retry_row())["state"] == "armed"
    rig.git.pr_base_freshness.assert_awaited_once()
    rig.git.merge_pr.assert_not_awaited()
    rig.git.update_pr_branch.assert_awaited_once()
    assert rig.git.update_pr_branch.await_args.args == (77, APPROVED)
