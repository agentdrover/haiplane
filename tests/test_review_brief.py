"""A brief may not look verified where nothing was verified (#725).

The brief for #643 (project spike-bo, 19.08.2026) offered

    git diff develop...task-643/memo-spike-impl

against a project whose base is ``main`` and which has no ``develop`` at all.
Every consumer of the diff then went quiet — ``call_sites`` reported "the diff
named no changed lines" over 67 changed files — while ``sha_check: match`` sat
beside them with an empty reason. Three unknowns and one green word, and the
green word is what a reviewer reads.

These tests hold the two halves of the fix: the base is the project's own and
is resolved before it is offered, and a base that does not resolve is stated
where the command would be, with the blocks it disabled saying so themselves.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins
from hub.services import review_evidence
from hub.services.finding_identity import finding_uids


def _git(repo_dir: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(repo_dir),
        },
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A real checkout whose base branch is ``main`` — spike-bo's shape."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "file.py").write_text("def f():\n    return 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    _git(root, "checkout", "-b", "task-42/work")
    (root / "file.py").write_text("def f():\n    return 2\n")
    _git(root, "commit", "-am", "work")
    return root


class _RealRefs(NoopGitOps):
    """Only ref resolution is real here — the rest stays inert on purpose."""

    async def resolve_ref(self, name: str, repo: str) -> tuple[str, str]:
        from hub.integrations.git_ops import GitOpsIntegration

        return await GitOpsIntegration().resolve_ref(name, repo)


async def _project_with(db, client: AsyncClient, workspace: Path, base: str) -> int:
    """A task on a project whose default_branch is ``base``."""
    project_id = await repo.create_project(
        db,
        slug=f"proj-{base}",
        name="Spike",
        repo_name="mrPDA/Spike_bo",
        workspace_path=str(workspace),
        default_branch=base,
    )
    epic = (
        await client.post("/api/tasks", json={"title": "Epic", "task_type": "epic"})
    ).json()["id"]
    await repo.update_task(db, epic, project_id=project_id)
    task_id = (
        await client.post(
            "/api/tasks",
            json={"title": "Feature work", "task_type": "task", "parent_id": epic},
        )
    ).json()["id"]
    await repo.update_task(db, task_id, branch="task-42/work")
    await db.commit()
    return task_id


async def test_diff_base_uses_project_default_branch(
    db, client: AsyncClient, workspace
):
    # AC-1: the base is the project's own default_branch, and it is resolved in
    # the project's workspace before the command is offered. The hardcoded
    # "develop" produced a command that could not run on any project that names
    # its base differently — and spike-bo is one.
    task_id = await _project_with(db, client, workspace, "main")
    plugins.git_ops = _RealRefs()

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()

    assert brief["diff_base"]["base"] == "main", "the project's base, not a constant"
    assert brief["diff_base"]["source"] == "project default_branch"
    assert brief["diff_base"]["state"] == review_evidence.BASE_RESOLVED
    assert brief["diff_base"]["sha"], "a resolved base names the commit it resolved to"
    assert brief["diff_command"] == "git diff main...task-42/work"
    assert "develop" not in brief["diff_command"]


async def test_unresolvable_base_is_reported_not_silent(
    db, client: AsyncClient, workspace
):
    # AC-2: a base that does not exist is stated where the diff command would
    # be — not left to be inferred from three downstream blocks that each say
    # "unknown" as if each had looked. And no check may show a bare green word
    # beside them: sha_check=match now says what it compared, and the coverage
    # verdict stays non-green.
    task_id = await _project_with(db, client, workspace, "develop")  # no such ref
    plugins.git_ops = _RealRefs()
    await repo.update_task(db, task_id, submission_sha="deadbeef" * 5)
    await db.commit()

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()

    base = brief["diff_base"]
    assert base["state"] == review_evidence.BASE_UNRESOLVED
    assert "develop" in base["reason"] and "does not exist" in base["reason"]
    assert brief["diff_command"] == "", (
        "a command that cannot run must not be offered — it reads as an offer to verify"
    )

    assert brief["call_sites"]["reason"].startswith(review_evidence.DISABLED_BY_BASE), (
        "the block that reads the diff must name the one cause, not report a "
        "bare unknown of its own"
    )

    coverage = brief["evidence_coverage"]
    assert coverage["state"] != review_evidence.COVERAGE_COMPLETE
    assert "diff base did not resolve" in coverage["headline"]
    assert "diff_base" in [c["check"] for c in coverage["checks_missing"]]
    assert brief["sha_check"] != "match" or brief["sha_check_reason"], (
        "no check reports a bare green beside blocks that produced nothing"
    )


async def test_sha_check_match_names_what_it_compared(
    db, client: AsyncClient, workspace
):
    # AC-2, the second half. "match" was true and empty: it compares a branch
    # POINTER against the submission SHA. Beside three unknowns, a lone green
    # word is read as evidence about the code.
    task_id = await _project_with(db, client, workspace, "main")

    class _Tip(_RealRefs):
        async def fetch_base(self, repo: str, base: str) -> tuple[bool, str]:
            return (True, "")

        async def head_sha(self, repo: str, base: str) -> str:
            return "c0ffee" * 6

    plugins.git_ops = _Tip()
    await repo.update_task(db, task_id, submission_sha="c0ffee" * 6)
    await db.commit()

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()

    assert brief["sha_check"] == "match"
    reason = brief["sha_check_reason"]
    assert reason, "a green verdict with an empty reason is what this fixes"
    assert "WHERE the branch points" in reason
    assert (
        "sha_check=match is not part of this count"
        in (brief["evidence_coverage"]["headline"])
    ), "the coverage verdict must refuse to be lifted by it"


async def test_a_workspace_free_project_says_unverified_not_missing(
    client: AsyncClient,
):
    # Three states, never two. With nothing to look in, the base is
    # `unverified` and the command still stands — the reviewer has a checkout
    # even when the hub does not. Reporting `unresolved` here would assert a
    # fact nobody observed, which is the same defect in the other direction.
    task_id = (await client.post("/api/tasks", json={"title": "Local"})).json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: go"},
    )
    branch = (
        await client.post(
            f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
        )
    ).json()["branch"]

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()

    assert brief["diff_base"]["state"] == review_evidence.BASE_UNVERIFIED
    assert brief["diff_base"]["reason"], "could-not-look always carries its cause"
    assert branch in brief["diff_command"], (
        "an unverifiable base is not a wrong one — the command still helps"
    )


async def test_a_task_without_test_acs_is_not_reported_as_lost_evidence():
    # A warning that inflates gets muted, and the real one is muted with it
    # (the noise lesson recorded on the drift guard, #534). Checks with nothing
    # to run over are listed apart from checks that could not run.
    coverage = review_evidence.evidence_coverage(
        diff_base={"state": review_evidence.BASE_RESOLVED, "base": "main"},
        branch="task-1/x",
        call_sites_status="analysed",
        has_test_acs=False,
        locator_resolution=[],
        ac_test_results=[],
        ci_state="current",
        freshness={"state": "no_overlap"},
        sha_check="unknown",
    )

    assert coverage["state"] == review_evidence.COVERAGE_COMPLETE
    assert [c["check"] for c in coverage["checks_not_applicable"]] == [
        "locator_resolution",
        "ac_test_results",
        # #814 joins the same list here, and for the same reason: nothing has
        # shipped yet, so a live check is not a check that failed to run.
        "live_check",
    ]


async def test_brief_carries_the_same_review_report(client: AsyncClient):
    # AC-4 (#808): the human at the gate and the reviewing agent read one
    # report, built by one function. Two renderings of the same facts drift.
    resp = await client.post("/api/tasks", json={"title": "Shared report task"})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review",
            "agent_count": 1,
            "model": "grok-4.6",
            "raw_count": 1,
            "findings_confirmed": [
                {"locator": "none", "title": "leak", "severity": "high"}
            ],
            "findings_rejected": [],
            "incomplete": False,
            "unresolved": [],
            "lost_dimensions": [],
            "agent": "cursor-cloud-reviewer",
        },
    )

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()

    report = brief["review_report"]
    assert report["state"] == "current"
    assert report["branch"].startswith(f"task-{task_id}/")
    assert report["machine_review"]["model"] == "grok-4.6"
    # And the same block on the card the human reads.
    card = (await client.get(f"/tasks/{task_id}")).text
    assert "Проверялось:" in card and "grok-4.6" in card


async def test_brief_carries_finding_uids(client: AsyncClient):
    # AC-7 (#1028): the derived id has to reach the reader, not just the POST
    # response. The brief is where a reviewing agent reads the findings, and it
    # is the place a disposition gets addressed from.
    resp = await client.post("/api/tasks", json={"title": "Brief uid task"})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review",
            "model": "grok-4.6",
            "raw_count": 2,
            "findings_confirmed": [
                {
                    "locator": "lines",
                    "file": "hub/app.py",
                    "start_line": 12,
                    "title": "leak",
                    "severity": "high",
                },
                {"locator": "none", "title": "smell", "severity": "low"},
            ],
            "findings_rejected": [],
            "incomplete": False,
            "unresolved": [],
            "lost_dimensions": [],
            "agent": "cursor-cloud-reviewer",
        },
    )

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    findings = brief["review_report"]["machine_review"]["findings_confirmed"]
    uids = [f["finding_uid"] for f in findings]
    # Not "an id is present" — THE id. A non-empty string proves the field was
    # filled; only equality with the id derived from the same content proves
    # the brief addresses the same finding a disposition will be filed against.
    assert uids == finding_uids(
        [
            {
                "locator": "lines",
                "file": "hub/app.py",
                "start_line": 12,
                "title": "leak",
                "severity": "high",
            },
            {"locator": "none", "title": "smell", "severity": "low"},
        ]
    )
    assert len(set(uids)) == 2


async def test_brief_report_says_when_no_review_happened(client: AsyncClient):
    # The absence travels too: an agent reading the brief must be able to see
    # that nothing has reviewed this submission yet.
    resp = await client.post("/api/tasks", json={"title": "Unreviewed task"})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()

    assert brief["review_report"]["state"] == "none"
    assert brief["review_report"]["machine_review"] is None


# ---- #823: one assembly, two readers ----


async def test_brief_and_card_share_one_evidence_builder(client: AsyncClient):
    """AC-3 (#823): the human's card and the agent's brief report the same
    evidence because they are built by the same function.

    Kept behavioural on purpose: two call sites producing equal text today can
    drift apart tomorrow, so the test asserts the agreement a reader would
    notice — the coverage verdict and the CI cause — over the same submission.
    """
    created = await client.post("/api/tasks", json={"title": "Shared evidence"})
    task_id = created.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    card = (await client.get(f"/tasks/{task_id}")).text

    assert brief["evidence_coverage"]["state"] in card
    assert brief["ci_run_report"]["reason"] in card, (
        "the cause the agent is given must be the cause the human is given"
    )


# --- The deterministic prepass (#875, feature #870) --------------------------
#
# The reviewer was paying model prices to rediscover defects ruff and mypy had
# proven absent minutes earlier: those steps ran in CI and stopped there. The
# grant "do not look at this class" is worth only as much as the fact behind
# it, so it is tied to a check that RAN and PASSED on the pinned commit.


_PINNED_SHA = "a" * 40


async def _submitted_task(client: AsyncClient, db, title: str) -> int:
    resp = await client.post("/api/tasks", json={"title": title})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    # There is no git behind these tests, so nothing pins a commit. The prepass
    # is keyed on the pinned sha, so one is written here — the fixture stands in
    # for the workspace, not for the rule.
    await repo.update_task(db, task_id, submission_sha=_PINNED_SHA)
    await db.commit()
    return task_id


async def _report_checks(
    client: AsyncClient, db, task_id: int, checks: dict, *, head_sha: str = ""
) -> None:
    """Report a CI run through the service the endpoint calls.

    Straight to the service on purpose: the HTTP route is guarded by the narrow
    tasks.ci_report permission, and wiring a ci_runner token here would test
    the auth layer rather than the prepass.
    """
    from hub.services.ci_report import accept_ci_run_report

    row = dict(await repo.get_task(db, task_id))
    await accept_ci_run_report(
        db,
        task_id,
        head_sha=head_sha or row["submission_sha"],
        ac_results={},
        checks=checks,
        reported_by="github-actions",
    )


async def test_prepass_block_present_and_passed_to_prompt(client: AsyncClient, db):
    # AC-1 (#875): checks that ran and passed on THIS commit reach the brief and
    # the reviewer's prompt, and the prompt names what each one covers.
    from hub.services.review_brief import build_review_brief

    task_id = await _submitted_task(client, db, "Prepass task")
    await _report_checks(
        client,
        db,
        task_id,
        {"lint": "pass", "types": "pass", "tests": "pass", "security": "skipped"},
    )

    brief = await build_review_brief(db, task_id)

    assert brief.prepass.state == "covered"
    assert brief.prepass.passed == ["lint", "tests", "types"]
    assert brief.prepass.skipped == ["security"]

    block = review_evidence.prepass_block(brief.prepass)
    assert "НЕ трать проход" in block
    assert "ruff" in block and "mypy" in block, "the grant names the tool, not a topic"
    # The grant must not read as "there are no defects left".
    assert "не что дефектов больше нет" in block
    # A skipped step proves nothing and says so.
    assert "Пропущены (ничего не доказывают): security" in block


async def test_missing_prepass_states_cause_and_grants_nothing(client: AsyncClient, db):
    # AC-2 (#875): no report for this commit is a NAMED absence, and it hands
    # out no silence. "Nobody checked" and "checked, nothing found" are the two
    # states this whole block exists to keep apart.
    from hub.services.review_brief import build_review_brief

    task_id = await _submitted_task(client, db, "No prepass task")

    brief = await build_review_brief(db, task_id)

    assert brief.prepass.state == "unknown"
    assert brief.prepass.passed == []
    assert "не присылал отчёт" in brief.prepass.reason

    block = review_evidence.prepass_block(brief.prepass)
    assert "данных нет" in block
    assert "Ничего не считай проверенным" in block
    assert "НЕ трать проход" not in block, "no report must never buy silence"


async def test_report_without_checks_grants_nothing_either(client: AsyncClient, db):
    # A report that names no checks is every report written before #875. It
    # must read as "nothing is proven", not as "everything passed".
    from hub.services.review_brief import build_review_brief

    task_id = await _submitted_task(client, db, "Checkless report task")
    await _report_checks(client, db, task_id, {})

    brief = await build_review_brief(db, task_id)

    assert brief.prepass.state == "unknown"
    assert "не назвал ни одной" in brief.prepass.reason
    assert "НЕ трать проход" not in review_evidence.prepass_block(brief.prepass)


async def test_failed_check_is_told_to_the_reviewer_as_a_fact(client: AsyncClient, db):
    # A check that RAN and FAILED is louder than one that passed: the code
    # under review is known-broken in a way a tool already proved, and that is
    # a fact for the report rather than the reviewer's own finding.
    from hub.services.review_brief import build_review_brief

    task_id = await _submitted_task(client, db, "Red prepass task")
    await _report_checks(client, db, task_id, {"lint": "pass", "types": "fail"})

    brief = await build_review_brief(db, task_id)

    assert brief.prepass.state == "failed"
    assert brief.prepass.failed == ["types"] and brief.prepass.passed == ["lint"]
    block = review_evidence.prepass_block(brief.prepass)
    assert "проверки УПАЛИ: types" in block
    assert "не твоя находка" in block
    # What DID pass still buys its silence — the two facts are independent.
    assert "НЕ трать проход" in block


async def test_report_for_another_commit_grants_nothing(client: AsyncClient, db):
    # The pinned commit is the whole basis of the grant. A run on other code
    # proves nothing about this submission (#572).
    from hub.services.review_brief import build_review_brief

    task_id = await _submitted_task(client, db, "Other commit task")
    await _report_checks(client, db, task_id, {"lint": "pass"}, head_sha="f" * 40)

    brief = await build_review_brief(db, task_id)

    assert brief.prepass.state == "unknown"
    assert brief.prepass.passed == []


async def test_unknown_check_outcome_is_refused(client: AsyncClient, db):
    # The same enumeration discipline the AC statuses get: an outcome the hub
    # cannot name is refused, not stored for the block to interpret later.
    task_id = await _submitted_task(client, db, "Weird outcome task")

    with pytest.raises(ValueError, match="unknown check outcome"):
        await _report_checks(client, db, task_id, {"lint": "probably"})
