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
        # #1233 joins it too: this call names no base-merge block at all, and a
        # block the brief does not carry is not a block that stayed silent.
        "base_merge",
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


# --- #764: the locator block stops saying "unavailable" ---------------------
#
# Every brief read on production reported `unknown` for every criterion, nine
# days running, while pytest ran fine in the task's own worktree. The hub was
# looking in the shared clone, which worktree-per-task leaves on the base
# branch for every task — so the HEAD guard never matched and collection was
# never attempted. Not one line in the log said so, because it never got that
# far.


class _RealFiles(NoopGitOps):
    """Real git reads only: the tree of a ref and a file out of it."""

    async def file_at_ref(self, repo: str, ref: str, path: str) -> str | None:
        from hub.integrations.git_ops import GitOpsIntegration

        return await GitOpsIntegration().file_at_ref(repo, ref, path)

    async def files_at_ref(self, repo: str, ref: str):
        from hub.integrations.git_ops import GitOpsIntegration

        return await GitOpsIntegration().files_at_ref(repo, ref)

    async def read_file_at_ref(self, repo: str, ref: str, path: str, **kw):
        from hub.integrations.git_ops import GitOpsIntegration

        return await GitOpsIntegration().read_file_at_ref(repo, ref, path, **kw)

    async def head_sha(self, repo: str, base: str) -> str:
        from hub.integrations.git_ops import GitOpsIntegration

        return await GitOpsIntegration().head_sha(repo, base)

    async def resolve_ref(self, name: str, repo: str):
        from hub.integrations.git_ops import GitOpsIntegration

        return await GitOpsIntegration().resolve_ref(name, repo)


async def _task_with_test_ac(db, client: AsyncClient, workspace, locator: str) -> int:
    task_id = await _project_with(db, client, workspace, "main")
    await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "given": "g",
                    "when": "w",
                    "then": "t",
                    "verifiable_by": "test",
                    "test_ref": locator,
                }
            ]
        },
    )
    return task_id


async def test_locator_resolves_at_the_ref_not_in_a_working_tree(
    db, client: AsyncClient, workspace
):
    """#764 AC-1 / #1650: the file is read at the task's ref, wherever HEAD is.

    The shared clone sits on the base branch for every task under
    worktree-per-task, so a check that depended on what is checked out could
    not have resolved anything; reading the ref does not care.
    """
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_a.py").write_text("def test_ok():\n    pass\n")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "the test the submission adds")
    _git(workspace, "checkout", "main")  # HEAD is NOT the task's branch
    task_id = await _task_with_test_ac(
        db, client, workspace, "tests/test_a.py::test_ok"
    )
    plugins.git_ops = _RealFiles()

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()

    assert brief["locator_resolution"][0]["status"] == "resolvable"
    assert "[ref: task branch task-42/work" in brief["locator_resolution"][0]["reason"]


async def test_no_readable_tree_reports_unknown_not_missing(
    db, client: AsyncClient, workspace
):
    """#764 AC-4: "could not look" is never dressed up as "not there".

    A missing locator is an accusation about the author's work. Making it on
    the strength of a failed read would be the same defect this task fixes,
    pointed the other way.
    """
    task_id = await _task_with_test_ac(
        db, client, workspace, "tests/test_a.py::test_ok"
    )
    await repo.update_task(db, task_id, submission_sha="0" * 40)
    await db.commit()
    plugins.git_ops = _RealFiles()  # real reads against a sha that is not there

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()

    resolution = brief["locator_resolution"][0]
    assert resolution["status"] == "unknown"
    assert resolution["reason"]


async def test_a_locator_naming_a_file_the_submission_lacks_is_missing(
    db, client: AsyncClient, workspace
):
    """#764: the commonest way a locator dies — the file was never written.

    Both of my own cards yesterday named files that do not exist in the
    repository at all (tests/test_digest.py, tests/test_lifecycle_matrix.py),
    and nothing said a word.
    """
    task_id = await _task_with_test_ac(
        db, client, workspace, "tests/never_written.py::test_ok"
    )
    plugins.git_ops = _RealFiles()

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()

    resolution = brief["locator_resolution"][0]
    assert resolution["status"] == "missing"
    assert "tests/never_written.py" in resolution["reason"]


def _awaitable(value):
    async def _coro():
        return value

    return _coro()


# ---- #1055: the brief reads the diff of a branch it only has as origin/<name> ----


@pytest.fixture
def remote_only_workspace(tmp_path: Path) -> Path:
    """A clone in production's shape: the task branch is a remote ref only."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "develop", ".")
    author = tmp_path / "author"
    author.mkdir()
    _git(author, "clone", str(origin), ".")
    (author / "hub").mkdir()
    (author / "hub" / "mod.py").write_text("def touched():\n    return 1\n")
    (author / "hub" / "caller.py").write_text(
        "from hub.mod import touched\n\n\ndef use():\n    return touched()\n"
    )
    _git(author, "add", ".")
    _git(author, "commit", "-m", "base")
    _git(author, "push", "-q", "origin", "develop")
    _git(author, "checkout", "-b", "task-42/work")
    (author / "hub" / "mod.py").write_text("def touched():\n    return 2\n")
    _git(author, "commit", "-am", "work")
    _git(author, "push", "-q", "origin", "task-42/work")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "clone", str(origin), ".")
    return workspace


async def test_call_sites_read_on_a_branch_only_in_origin(
    db, client: AsyncClient, remote_only_workspace
):
    # #1055 AC-3: on production every pair branch is in this state, and the
    # brief lost two of its six evidence blocks to it — call sites and diff
    # volume both reported "could not read the diff of <branch> against
    # <base>" while the commit sat in the clone under origin/<branch>.
    from hub.integrations.git_ops import GitOpsIntegration

    task_id = await _project_with(db, client, remote_only_workspace, "develop")
    plugins.git_ops = GitOpsIntegration()

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()

    assert "could not read the diff" not in brief["call_sites"]["reason"], (
        "the diff of a branch the clone holds as origin/<name> must be readable"
    )
    assert brief["call_sites"]["status"] != "unknown", brief["call_sites"]["reason"]
    report = brief["review_report"]
    assert report["diff_files"] == 1 and report["diff_lines"] == 2, report
    assert not report["diff_note"], (
        "with the diff read there is nothing to excuse: the volume is the answer"
    )


class _OnTaskBranch(_RealFiles):
    """Real file reads, and the tree standing on the task's own branch."""

    async def current_branch(self, repo: str | None = None) -> str:
        return "task-42/work"


async def test_foreign_locator_is_read_from_its_file(
    db, client: AsyncClient, workspace
):
    """#1203: a locator of another runner is judged by its own reader.

    Nothing pytest could list speaks for a vitest test, and the file text must
    actually be fetched — a resolver handed nothing answered "could not read"
    about a file no one had opened.
    """
    (workspace / "frontend").mkdir()
    (workspace / "frontend" / "recent.test.ts").write_text(
        'it("still toggles", () => {});\n'
    )
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "a vitest test the submission adds")

    task_id = await _task_with_test_ac(
        db, client, workspace, "frontend/recent.test.ts::still toggles"
    )
    plugins.git_ops = _OnTaskBranch()

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()

    resolution = brief["locator_resolution"][0]
    assert resolution["status"] == "resolvable", resolution
    assert "could not read" not in resolution["reason"], resolution


async def test_an_unasked_base_merge_does_not_pass_for_full_coverage():
    """Блок мержа базы посчитан, а не показан рядом со счётчиком (#1233).

    Находка 84b9b04c160f8350. Правило записала сама эта функция для #814: блок,
    который в брифе есть, но в счёте не участвует, оставляет заголовок врущим в
    успокаивающую сторону. Здесь цена особенно высока — «спросить не удалось» и
    «мерж будет чистым» ведут человека к разным решениям ровно перед тем
    вердиктом, который #1233 бережёт.
    """
    common = {
        "diff_base": {"state": review_evidence.BASE_RESOLVED, "base": "main"},
        "branch": "task-1233/x",
        "call_sites_status": "analysed",
        "has_test_acs": False,
        "locator_resolution": [],
        "ac_test_results": [],
        "ci_state": "current",
        "freshness": {"state": "no_overlap"},
        "sha_check": "unknown",
    }

    blind = review_evidence.evidence_coverage(
        **common, base_merge={"state": "unknown", "reason": "спросить не удалось"}
    )
    assert blind["state"] != review_evidence.COVERAGE_COMPLETE, (
        "«все блоки дали сигнал» при неспрошенном расхождении с базой — это "
        "тот самый успокаивающий заголовок, который #725/#814 запретили"
    )
    assert "base_merge" in [c["check"] for c in blind["checks_missing"]]

    seen = review_evidence.evidence_coverage(
        **common, base_merge={"state": "conflicting", "reason": "не будет чистым"}
    )
    assert seen["state"] == review_evidence.COVERAGE_COMPLETE, (
        "названный конфликт — это СИГНАЛ, а не отсутствие его: человек узнал "
        "о расхождении до вердикта, чего AC-1 и требует"
    )
    assert "base_merge" in seen["checks_ran"]

    absent = review_evidence.evidence_coverage(**common)
    assert "base_merge" in [c["check"] for c in absent["checks_not_applicable"]], (
        "блока в брифе нет вовсе — требовать с него сигнала не с чего"
    )


async def test_the_brief_feeds_its_base_merge_block_into_the_coverage_verdict(
    client: AsyncClient, monkeypatch
):
    """Блок, показанный в брифе, обязан быть в его же счёте (#1233).

    Вторая половина находки 84b9b04c160f8350: мало научить счётчик принимать
    блок — бриф обязан его туда ПЕРЕДАТЬ. Без передачи счёт видит «блока нет»,
    объявляет полное покрытие, а рядом в том же брифе стоит названный конфликт
    с базой. Один документ, два несогласных утверждения — ровно та подмена,
    против которой #725 и заводил единый вердикт над блоками.
    """
    from unittest.mock import AsyncMock

    from hub.services import review_brief

    task_id = (await client.post("/api/tasks", json={"title": "Base merge"})).json()[
        "id"
    ]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: go"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    monkeypatch.setattr(
        review_brief,
        "base_merge_section",
        AsyncMock(
            return_value=review_brief.BaseMergeState(
                state="conflicting",
                reason="мерж в базу НЕ будет чистым",
                files=["tests/test_review_dispatch.py"],
            )
        ),
    )

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    coverage = brief["evidence_coverage"]

    assert brief["base_merge"]["state"] == "conflicting", "блок в брифе есть"
    assert "base_merge" in coverage["checks_ran"], (
        "и он посчитан: блок, который виден в брифе, но не участвует в счёте, "
        "оставляет заголовок покрытия несогласным с самим брифом"
    )
    assert "base_merge" not in [
        c["check"] for c in coverage["checks_not_applicable"]
    ], "«спрашивать нечего» — это не про блок, который прямо сейчас показан"


async def test_the_brief_feeds_an_unasked_base_merge_into_the_coverage_too(
    client: AsyncClient, monkeypatch
):
    """Через бриф проверен и второй исход блока — «спросить не удалось».

    Находка 17a0fa0673a52b4f. Сосед выше идёт через бриф, но мокает ТОЛЬКО
    ``conflicting``, а это сигнал: покрытие остаётся полным, и равенство
    сходится. Поэтому мутация «передавать блок в счёт, лишь когда он дал
    сигнал» оставляла его зелёным: при ``unknown`` счётчик читал бы ``None``
    как «блока нет вовсе» и объявлял ПОЛНОЕ покрытие — ровно подмену «спросить
    не удалось» на «чисто», ради снятия которой блок в счёт и заводили.
    Рядом стоящий тест пинит счётчик напрямую и проводки не видит.
    """
    from unittest.mock import AsyncMock

    from hub.services import review_brief

    task_id = (await client.post("/api/tasks", json={"title": "Base merge"})).json()[
        "id"
    ]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: go"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    monkeypatch.setattr(
        review_brief,
        "base_merge_section",
        AsyncMock(
            return_value=review_brief.BaseMergeState(
                state="unknown",
                reason="клон не ответил про расхождение с базой",
                files=[],
            )
        ),
    )

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    coverage = brief["evidence_coverage"]

    assert brief["base_merge"]["state"] == "unknown", "блок в брифе есть"
    assert "base_merge" in [c["check"] for c in coverage["checks_missing"]], (
        "блок показан и сигнала не дал — это недостающий сигнал, а не "
        f"отсутствующий блок: {coverage}"
    )
    assert "base_merge" not in [
        c["check"] for c in coverage["checks_not_applicable"]
    ], "«спрашивать нечего» — это не про блок, который прямо сейчас показан"
    assert coverage["state"] != "complete", (
        "покрытие не может быть полным, пока показанный блок молчит"
    )


async def test_the_brief_names_the_second_provider_and_why(client: AsyncClient, db):
    """AC-3 (#1266): бриф называет канал отчёта и причину, когда это не облако.

    Текст «ВТОРЫМ поставщиком... причина» сегодня живёт только в ленте
    задачи (review_dispatch.py, находка при create). MachineReviewView не
    несёт channel и причину отказа облака, и бриф показывает локальный
    отчёт неотличимым от облачного.
    """
    from hub.services.review_dispatch import LOCAL_CHANNEL

    task_id = await _submitted_task(client, db, "Second door brief task")
    row = dict(await repo.get_task(db, task_id))
    generation = row["submission_generation"]

    dispatch_id = await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=generation,
        agent_id="local:abc123",
        run_id="abc123",
        model="local-reviewer",
        profile="lite",
        channel=LOCAL_CHANNEL,
        second_door_reason=(
            "облако отчёта НЕ дало — usage_limit_exceeded; отчёт добывается "
            "ВТОРЫМ поставщиком, локальным (#1252)"
        ),
    )
    await repo.set_review_dispatch_status(db, dispatch_id, "done")
    await db.commit()

    await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review",
            "model": "local-reviewer",
            "raw_count": 1,
            "findings_confirmed": [],
            "findings_rejected": [],
            "incomplete": False,
            "unresolved": [],
            "lost_dimensions": [],
            "agent": "local-reviewer",
        },
    )

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    machine_review = brief["review_report"]["machine_review"]
    assert machine_review["second_door_channel"] == "local", (
        "бриф обязан назвать канал отчёта — иначе локальный читается как облачный"
    )
    assert "usage_limit_exceeded" in machine_review["second_door_reason"], (
        "бриф обязан назвать причину, по которой отчёт не облачный"
    )


async def test_the_brief_top_level_and_review_report_agree_on_the_provider(
    client: AsyncClient, db
):
    """F2 (раунд 2, c0babbdf6d557c91): в брифе ДВЕ независимые MachineReviewView.

    Верхнеуровневый ``machine_review`` строится в build_review_brief напрямую
    из ``machine_reviews`` и не получает second_door_channel/reason — их
    заполняет только ``review_evidence.review_report()``. MCP hub_get_review_
    brief и CLI читают верхнеуровневое поле и не могут отличить локальный
    отчёт от облачного, хотя review_report.machine_review уже умеет.
    """
    from hub.services.review_dispatch import LOCAL_CHANNEL

    task_id = await _submitted_task(client, db, "Two copies brief task")
    row = dict(await repo.get_task(db, task_id))
    generation = row["submission_generation"]

    dispatch_id = await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=generation,
        agent_id="local:f2",
        run_id="f2",
        model="local-reviewer",
        profile="lite",
        channel=LOCAL_CHANNEL,
        second_door_reason=(
            "облако отчёта НЕ дало — usage_limit_exceeded; отчёт добывается "
            "ВТОРЫМ поставщиком, локальным (#1252)"
        ),
    )
    await repo.set_review_dispatch_status(db, dispatch_id, "done")
    await db.commit()

    await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review",
            "model": "local-reviewer",
            "raw_count": 1,
            "findings_confirmed": [],
            "findings_rejected": [],
            "incomplete": False,
            "unresolved": [],
            "lost_dimensions": [],
            "agent": "local-reviewer",
        },
    )

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    assert brief["machine_review"]["second_door_channel"] == "local", (
        "верхнеуровневое поле machine_review — то, что читает MCP "
        "hub_get_review_brief и CLI — обязано называть канал, как и "
        "review_report.machine_review"
    )
    assert brief["review_report"]["machine_review"]["second_door_channel"] == "local"


async def test_the_brief_pairs_the_displayed_report_with_its_own_dispatch(
    client: AsyncClient, db
):
    """F1 (раунд 2, 715481fedbf41130): показанный отчёт красится СВОИМ заказом.

    get_settled_review_dispatch отдаёт ПОСЛЕДНИЙ done ЗАКАЗ (по id заказа), а
    показывается ПОСЛЕДНИЙ ОТЧЁТ (по id отчёта, get_latest_machine_review). В
    сценарии AC-5 (#1266): локальная замена закрылась СВОИМ отчётом первой,
    затем облачный заказ закрылся своим поздним отчётом. «Последний done
    заказ» — локальный (он вставлен позже облачного и тоже done), а
    «последний отчёт» — облачный (он пришёл позже). Бриф красит облачный
    отчёт как локальный — ровно то, что AC-3 должна была закрыть.
    """
    from hub.services.machine_review_intake import record_machine_review
    from hub.models import MachineReviewSubmit
    from hub.services import admin as admin_svc
    from hub.services.review_dispatch import CLOUD_CHANNEL, LOCAL_CHANNEL

    task_id = await _submitted_task(client, db, "Own-dispatch pairing task")
    row = dict(await repo.get_task(db, task_id))
    generation = row["submission_generation"]

    cloud_principal = await admin_svc.create_principal(
        db, kind="agent", username="cloud-reviewer-f1"
    )
    local_principal = await admin_svc.create_principal(
        db, kind="agent", username="local-reviewer-f1"
    )
    cloud_pid, local_pid = cloud_principal["id"], local_principal["id"]

    cloud_id = await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=generation,
        agent_id="bc-f1",
        run_id="r-cloud",
        model="grok-4.6",
        profile="lite",
        channel=CLOUD_CHANNEL,
        reviewer_principal_id=cloud_pid,
    )
    local_id = await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=generation,
        agent_id="local:f1",
        run_id="r-local",
        model="local-reviewer",
        profile="lite",
        channel=LOCAL_CHANNEL,
        reviewer_principal_id=local_pid,
        replaces_dispatch_id=cloud_id,
        second_door_reason=(
            "облако отчёта НЕ дало — refused; ВТОРЫМ поставщиком, локальным"
        ),
    )
    await db.commit()

    payload = dict(
        harness_skill="lite-diff-review",
        raw_count=1,
        findings_confirmed=[],
        findings_rejected=[],
        incomplete=False,
        unresolved=[],
        lost_dimensions=[],
    )

    # Локальный отчёт приходит и закрывает ЛОКАЛЬНЫЙ заказ ПЕРВЫМ.
    await record_machine_review(
        db,
        task_id,
        MachineReviewSubmit(model="local-reviewer", **payload),
        principal_id=local_pid,
        username="local-reviewer-f1",
    )
    await repo.set_review_dispatch_status(db, local_id, "done")
    await db.commit()

    # Поздний облачный отчёт приходит ПОСЛЕ и закрывает облачный заказ.
    await record_machine_review(
        db,
        task_id,
        MachineReviewSubmit(model="grok-4.6", **payload),
        principal_id=cloud_pid,
        username="cloud-reviewer-f1",
    )
    await repo.set_review_dispatch_status(db, cloud_id, "done")
    await db.commit()

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    machine_review = brief["review_report"]["machine_review"]
    # Предпосылка: показанный отчёт — облачный (он пришёл последним).
    assert machine_review["submitted_by"] == "cloud-reviewer-f1", (
        "предпосылка: get_latest_machine_review обязана отдать облачный отчёт "
        "как самый свежий"
    )
    assert machine_review["second_door_channel"] == "", (
        "показан ОБЛАЧНЫЙ отчёт — second_door_channel обязан остаться пустым, "
        "иначе облачный отчёт подписан как локальный по чужому (более "
        "позднему по id) заказу"
    )


# --- #1262: есть ли ревью у ТЕКУЩЕГО поколения — один правдивый ответ ---------


_FULL_REPORT = {
    "harness_skill": "lite-diff-review",
    "agent_count": 1,
    "model": "grok-4.6",
    "raw_count": 1,
    "findings_confirmed": [],
    "findings_rejected": [],
    "incomplete": False,
    "unresolved": [],
    "lost_dimensions": [],
    "agent": "cursor-cloud-reviewer",
}


async def test_brief_says_whether_the_current_generation_has_a_review(
    client: AsyncClient, db, monkeypatch
):
    """AC-3 (#1262): отказ провайдера — «ревью у поколения N нет» с причиной;
    полный отчёт — ревьюер (принципал и модель), sha и полнота."""
    from tests.test_review_dispatch import (
        _LIMIT_REFUSAL,
        _DispatchRecorder,
        _no_local_path,
        _submitted,
        _wire,
    )

    _wire(monkeypatch, _DispatchRecorder(None, refusal=_LIMIT_REFUSAL))
    _no_local_path(monkeypatch)
    refused = await _submitted(
        client, db, "spike-brief-refused", policy={"review": "dispatch"}
    )
    reviewed = await _submitted_task(client, db, "Reviewed in full")
    resp = await client.post(f"/api/tasks/{reviewed}/machine-review", json=_FULL_REPORT)
    assert resp.status_code in (200, 201), resp.text

    refused_brief = (await client.get(f"/api/tasks/{refused}/review-brief")).json()
    answer = refused_brief["current_generation_review"]
    assert answer["has_review"] is False
    assert answer["generation"] == refused_brief["submission_generation"] == 1
    assert answer["reason"] == "provider_refused"
    assert "usage_limit_exceeded" in answer["reason_detail"]
    assert "ревью у поколения 1 НЕТ" in answer["headline"]

    reviewed_brief = (await client.get(f"/api/tasks/{reviewed}/review-brief")).json()
    answer = reviewed_brief["current_generation_review"]
    assert answer["has_review"] is True
    assert answer["reviewer_principal"] == "cursor-cloud-reviewer"
    assert answer["reviewer_model"] == "grok-4.6"
    assert answer["sha"] == _PINNED_SHA
    assert answer["complete"] is True
    assert answer["reason"] == ""


async def test_an_old_generation_report_is_not_a_review_of_the_current_one(
    client: AsyncClient, db
):
    """AC-4 (#1262): полный отчёт поколения N-1 не ревью поколения N."""
    task_id = await _submitted_task(client, db, "Resubmitted after review")
    resp = await client.post(f"/api/tasks/{task_id}/machine-review", json=_FULL_REPORT)
    assert resp.status_code in (200, 201), resp.text
    await repo.update_task(db, task_id, submission_generation=2)
    await repo.record_submission(
        db, task_id=task_id, generation=2, sha="b" * 40, base_branch="develop"
    )
    await db.commit()

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    answer = brief["current_generation_review"]
    assert answer["generation"] == 2
    assert answer["has_review"] is False, "отчёт прошлой сдачи — не ревью текущей"
    assert answer["reason"] == "not_dispatched"
    assert answer["previous_generation"] == 1
    assert "отчёт поколения 1 — отчёт прошлой сдачи" in answer["headline"]
    assert answer["sha"] == "b" * 40


async def test_an_incomplete_or_evidence_free_report_is_no_review(
    client: AsyncClient, db
):
    """Неполный отчёт и отчёт без улики исполнения — отсутствие ревью (#750, #841)."""
    incomplete = await _submitted_task(client, db, "Incomplete report")
    await client.post(
        f"/api/tasks/{incomplete}/machine-review",
        json={**_FULL_REPORT, "incomplete": True},
    )
    empty = await _submitted_task(client, db, "Evidence-free report")
    await client.post(
        f"/api/tasks/{empty}/machine-review", json={**_FULL_REPORT, "raw_count": 0}
    )

    for task_id, reason in (
        (incomplete, "incomplete_report"),
        (empty, "no_execution_evidence"),
    ):
        brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
        answer = brief["current_generation_review"]
        assert answer["has_review"] is False
        assert answer["reason"] == reason


# --- #1587: the brief names the profile the cap or the circle took away ----


async def _brief_task_with_orders(db, orders: list[tuple]) -> int:
    """Задача на ревью с заказами ``(channel, profile, reasons)`` в одном поколении."""
    from hub import services
    from hub.models import TaskCreate

    tv = await services.create_task(db, TaskCreate(title="downgrade brief"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: x")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")
    await services.submit_for_review(db, tv.id)
    generation = dict(await repo.get_task(db, tv.id))["submission_generation"]
    for i, (channel, profile, reasons) in enumerate(orders):
        agent = f"local:r{i}" if channel == "local" else f"bc-{tv.id}-{i}"
        await repo.create_review_dispatch(
            db,
            task_id=tv.id,
            submission_generation=generation,
            agent_id=agent,
            run_id=f"r{i}",
            model="grok-4.6",
            profile=profile,
            channel=channel,
        )
        await repo.insert_event(
            db,
            kind="review_dispatched",
            task_id=tv.id,
            actor="policy",
            payload={
                "agent_id": agent,
                "generation": generation,
                "profile": profile,
                "profile_reasons": reasons,
            },
        )
    await db.commit()
    return tv.id


_CAP = ["deep по правилу (риск), но суточный потолок 2 исчерпан — lite"]
_CIRCLE = ["круг: 3 захода, deep приостановлен до решения человека"]


async def test_brief_names_required_and_actual_review_profile(client: AsyncClient, db):
    from hub.services.review_brief import build_review_brief

    # Потолок #1414: требовался deep, заказан lite — до результата «заказан».
    capped = await _brief_task_with_orders(db, [("cloud", "lite", _CAP)])
    block = (await build_review_brief(db, capped)).profile_downgrade
    assert block is not None
    assert (block.required_profile, block.ordered_profile) == ("deep", "lite")
    assert block.state == "заказан", "before a result the order is not 'done'"
    assert block.reasons == _CAP and "потолок" in block.headline

    # Круг и запасной облачный заказ после local-first: причина того заказа.
    fallback = await _brief_task_with_orders(
        db, [("local", "deep", ["правило"]), ("cloud", "lite", _CIRCLE)]
    )
    block = (await build_review_brief(db, fallback)).profile_downgrade
    assert block is not None and block.channel == "cloud"
    assert block.reasons == _CIRCLE, "the reasons are the LATEST order's own"

    # Несколько заказов: бриф говорит о заказе, породившем ТЕКУЩИЙ отчёт.
    multi = await _brief_task_with_orders(
        db, [("cloud", "lite", _CAP), ("cloud", "deep", ["ревью запрошено"])]
    )
    assert (await build_review_brief(db, multi)).profile_downgrade is None, (
        "the latest order is a plain deep: no downgrade block"
    )
    resp = await client.post(
        f"/api/tasks/{multi}/machine-review",
        json={
            "harness_skill": "lite-diff-review",
            "raw_count": 1,
            "incomplete": False,
            "agent": "reviewer",
        },
    )
    assert resp.status_code == 200, resp.text
    done = (await build_review_brief(db, multi)).profile_downgrade
    assert done is not None and done.reasons == _CAP, (
        "the order that produced the report is the one the brief speaks of"
    )
    assert done.state == "отчёт получен"

    # Случай без понижения: lite по правилу, причина — не потолок и не круг.
    plain = await _brief_task_with_orders(
        db, [("cloud", "lite", ["класс риска r1, процессных поверхностей нет"])]
    )
    assert (await build_review_brief(db, plain)).profile_downgrade is None
    deep_only = await _brief_task_with_orders(db, [("cloud", "deep", ["security"])])
    assert (await build_review_brief(db, deep_only)).profile_downgrade is None

    # После рестарта хаба: блок читается из событий и строки заказа, не из памяти.
    again = await client.get(f"/api/tasks/{capped}/review-brief")
    assert again.status_code == 200
    assert again.json()["profile_downgrade"]["state"] == "заказан"
    from hub.mcp_server import _profile_downgrade_line

    assert "Требовался deep" in _profile_downgrade_line(again.json())


# --- #1589: path_notices в ответе на сдачу, брифе и карточке вердикта --------


def _commit_on(root, branch, name, text):
    from tests.test_lifecycle import _real

    _real(root, "checkout", "-q", branch)
    (root / name).write_text((root / name).read_text() + text)
    _real(root, "add", "-A")
    _real(root, "commit", "-q", "-m", f"edit {name}")
    _real(root, "checkout", "-q", "main")


async def test_brief_and_verdict_card_show_path_notices(
    db, client: AsyncClient, tmp_path, monkeypatch
):
    """#1589 AC-2: без ключа и с пустым списком — ничего; с правилами — ТЕКУЩЕЕ поколение."""
    import json

    from hub import mcp_server
    from tests.test_lifecycle import (
        _edit,
        _notice_lines,
        make_path_notices_git,
        make_path_notices_workspace,
        pair_task_with_branch,
        path_notices_project,
    )

    workspace = make_path_notices_workspace(tmp_path)
    git = make_path_notices_git(workspace)
    monkeypatch.setattr(plugins, "git_ops", git)
    evil = "<script>alert(1)</script> обновить копию"
    rules = [{"pattern": "deploy/remote-deploy.sh", "text": evil}]

    # Без ключа и с пустым списком: ни поля, ни строки, ни текста.
    for slug, configured in (("pn-nokey", None), ("pn-empty", [])):
        _, epic = await path_notices_project(
            db, client, workspace, configured, slug=slug
        )
        task_id = await pair_task_with_branch(
            db,
            client,
            epic,
            workspace,
            f"quiet {slug}",
            lambda r: _edit(r, "deploy/remote-deploy.sh"),
        )
        resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
        assert resp.status_code == 200, resp.text
        assert resp.json()["path_notices"] is None, slug
        assert not _notice_lines(await repo.get_task_updates(db, task_id)), slug
        brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
        assert brief["path_notices"] is None, slug
        assert mcp_server._path_notices_text(brief["path_notices"]) == ""
        page = (await client.get(f"/tasks/{task_id}")).text
        assert "Пути диффа требуют ручных шагов" not in page, slug

    # С правилами: пути задеты.
    _, epic = await path_notices_project(db, client, workspace, rules, slug="pn-rules")
    task_id = await pair_task_with_branch(
        db,
        client,
        epic,
        workspace,
        "with rules",
        lambda r: _edit(r, "deploy/remote-deploy.sh"),
    )
    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.status_code == 200, resp.text
    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    assert brief["path_notices"]["state"] == "matched"
    assert evil in brief["path_notices"]["text"]
    assert evil in mcp_server._path_notices_text(brief["path_notices"])

    async def fake_get(path):
        return brief

    monkeypatch.setattr(mcp_server, "_api_get", fake_get)
    mcp_result = await mcp_server.hub_get_review_brief(task_id)
    assert evil in mcp_result.content[0].text, "MCP-бриф называет предупреждение"

    page = (await client.get(f"/tasks/{task_id}")).text
    assert "Пути диффа требуют ручных шагов" in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page, "текст экранирован"
    assert "<script>alert(1)</script>" not in page

    # Правка политики ПОСЛЕ сдачи результат поколения не меняет.
    project = await repo.resolve_project_for_task(db, task_id)
    await repo.update_project(
        db,
        project["id"],
        gate_policy=json.dumps(
            {"path_notices": [{"pattern": "deploy/remote-deploy.sh", "text": "новый"}]}
        ),
    )
    await db.commit()
    brief2 = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    assert brief2["path_notices"]["text"] == brief["path_notices"]["text"]
    # Даже полное снятие правил: результат поколения зафиксирован сдачей.
    await repo.update_project(db, project["id"], gate_policy=json.dumps({}))
    await db.commit()
    brief2 = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    assert brief2["path_notices"]["text"] == brief["path_notices"]["text"]
    await repo.update_project(
        db, project["id"], gate_policy=json.dumps({"path_notices": rules})
    )
    await db.commit()

    # Пересдача без совпадения: правку скрипта откатили, старое предупреждение
    # прошлого поколения не показано ни в брифе, ни в карточке.
    from tests.test_lifecycle import _real

    branch = f"task-{task_id}/work"
    _real(workspace, "checkout", "-q", branch)
    _real(workspace, "checkout", "main", "--", "deploy/remote-deploy.sh")
    _real(workspace, "commit", "-q", "-m", "revert script edit")
    _real(workspace, "checkout", "-q", "main")
    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["path_notices"]["state"] == "none"
    assert len(_notice_lines(await repo.get_task_updates(db, task_id))) == 1, (
        "новой строки нет: совпадений в этом поколении нет"
    )
    brief3 = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    assert (
        brief3["path_notices"]["generation"] == brief["path_notices"]["generation"] + 1
    )
    assert mcp_server._path_notices_text(brief3["path_notices"]) == ""
    old_page = (await client.get(f"/tasks/{task_id}")).text
    assert "Пути диффа требуют ручных шагов" not in old_page
    assert 'id="path-notices"' not in old_page, (
        "блок карточки только у текущего поколения"
    )

    # Чистая ветка с пустым диффом: состояние none, ничего не показывается.
    clean_id = await pair_task_with_branch(
        db, client, epic, workspace, "empty diff", lambda r: None
    )
    await repo.update_project(
        db,
        project["id"],
        gate_policy=json.dumps({"path_notices": rules}),
    )
    await db.commit()
    resp = await client.post(f"/api/tasks/{clean_id}/submit-review", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["path_notices"]["state"] == "none"
    assert not _notice_lines(await repo.get_task_updates(db, clean_id))
    clean_brief = (await client.get(f"/api/tasks/{clean_id}/review-brief")).json()
    assert mcp_server._path_notices_text(clean_brief["path_notices"]) == ""
    assert (
        "Пути диффа требуют ручных шагов"
        not in (await client.get(f"/tasks/{clean_id}")).text
    )

    # Непрочитанный дифф: явное «проверка путей не выполнена», не тишина.
    class _Blind(type(git)):
        async def branch_touched_paths(self, *a, **kw):
            return None

    monkeypatch.setattr(plugins, "git_ops", _Blind())
    blind_id = await pair_task_with_branch(
        db,
        client,
        epic,
        workspace,
        "blind",
        lambda r: _edit(r, "deploy/remote-deploy.sh"),
    )
    resp = await client.post(f"/api/tasks/{blind_id}/submit-review", json={})
    assert resp.status_code == 200, resp.text
    block = resp.json()["path_notices"]
    assert block["state"] == "unknown" and block["reason"]
    assert "проверка путей не выполнена" in block["text"]
    lines = _notice_lines(await repo.get_task_updates(db, blind_id))
    assert len(lines) == 1 and "проверка путей не выполнена" in lines[0]
    blind_page = (await client.get(f"/tasks/{blind_id}")).text
    assert "проверка путей не выполнена" in blind_page

    # Дифф сдачи не прочитан вовсе (branch_diff_paths вернул None): то же явное
    # «не выполнена» с причиной диффа, а не тишина.
    class _NoDiff(type(git)):
        async def branch_diff_paths(self, *a, **kw):
            return None

    monkeypatch.setattr(plugins, "git_ops", _NoDiff())
    nodiff_id = await pair_task_with_branch(
        db,
        client,
        epic,
        workspace,
        "no diff",
        lambda r: _edit(r, "deploy/remote-deploy.sh"),
    )
    resp = await client.post(f"/api/tasks/{nodiff_id}/submit-review", json={})
    assert resp.status_code == 200, resp.text
    block = resp.json()["path_notices"]
    assert block["state"] == "unknown"
    assert "не удалось прочитать дифф ветки" in block["reason"]
    assert len(_notice_lines(await repo.get_task_updates(db, nodiff_id))) == 1


# ---- #1606: mutation / baseline evidence in the brief ------------------------


async def _store_report(db, task_id: int, *, mutations: dict, baseline: dict):
    import json

    await repo.upsert_ci_run_report(
        db,
        task_id=task_id,
        head_sha="a" * 40,
        ac_results="{}",
        validation_status="pass",
        validation_log="",
        reason="",
        reported_by="ci",
        checks="{}",
        mutations=json.dumps(mutations),
        baseline=json.dumps(baseline),
    )
    await db.commit()


async def test_missing_mutation_evidence_reads_as_not_received(
    db, client: AsyncClient, workspace
):
    """AC-4: absence is "не получено", a block names its run, an old one says unknown."""
    task_id = await _project_with(db, client, workspace, "main")
    plugins.git_ops = _RealRefs()
    await repo.update_task(db, task_id, submission_sha="a" * 40)
    await db.commit()

    async def evidence() -> dict:
        brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
        return brief["ci_evidence"]

    # No report for the pinned commit: neither block is a pass.
    both = await evidence()
    for key in ("mutations", "baseline"):
        assert both[key]["state"] == "not_received", both
        assert "не получено" in both[key]["reason"]

    # A report that carries no evidence keys (a synchronize run) is the same.
    await _store_report(db, task_id, mutations={}, baseline={})
    both = await evidence()
    assert both["mutations"]["state"] == "not_received"
    assert both["baseline"]["state"] == "not_received"

    # A block with provenance names the run and the event.
    provenance = {
        "run_id": "777",
        "run_url": "https://github.example/runs/777",
        "event": "pull_request.opened",
        "at": "2026-10-06T10:00:00Z",
    }
    await _store_report(
        db,
        task_id,
        mutations={"state": "ran", "survivors": [], "provenance": provenance},
        baseline={"state": "ran", "tests": {}},
    )
    both = await evidence()
    assert both["mutations"]["state"] == "received"
    assert both["mutations"]["result"] == "ran"
    assert "777" in both["mutations"]["run"]
    assert "pull_request.opened" in both["mutations"]["run"]
    # An old block, stored before provenance existed, does not invent one.
    assert both["baseline"]["state"] == "received"
    assert "прогон неизвестен" in both["baseline"]["run"]


async def test_brief_rejects_foreign_evidence_and_keeps_error_reasons(
    db, client: AsyncClient, workspace
):
    """#1606: provenance alone is not evidence; an error block shows its cause."""
    task_id = await _project_with(db, client, workspace, "main")
    plugins.git_ops = _RealRefs()
    await repo.update_task(db, task_id, submission_sha="a" * 40)
    await db.commit()
    prov = {"run_id": "9", "event": "workflow_dispatch", "at": "t", "run_url": ""}

    async def evidence() -> dict:
        brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
        return brief["ci_evidence"]

    for foreign in ({"provenance": prov}, {"unexpected": "x", "provenance": prov}):
        await _store_report(db, task_id, mutations=foreign, baseline=foreign)
        both = await evidence()
        for key in ("mutations", "baseline"):
            assert both[key]["state"] == "not_received", (key, foreign)
            assert both[key]["result"] == ""
            assert both[key]["reason"]

    error = {"state": "error", "reason": "timeout после 540 с", "provenance": prov}
    await _store_report(db, task_id, mutations=error, baseline=error)
    both = await evidence()
    for key in ("mutations", "baseline"):
        assert both[key]["state"] == "received"
        assert both[key]["result"] == "error"
        assert "timeout после 540 с" in both[key]["reason"]
        assert "9" in both[key]["run"]

    # An old valid block without provenance stays acceptable.
    old = {"state": "ran", "survivors": []}
    await _store_report(db, task_id, mutations=old, baseline={"state": "ran"})
    both = await evidence()
    assert both["mutations"]["state"] == "received"
    assert "прогон неизвестен" in both["mutations"]["run"]


# ---- #1650: the hub never runs the task branch's code to list its tests ----


async def test_brief_never_imports_task_branch_code(
    db, client: AsyncClient, tmp_path: Path, spawn_spy
):
    """#1650 AC-1: the brief and the steward packet read the branch, not run it.

    The branch carries a conftest.py that writes a marker outside the tree of
    the pytest running this test. HEAD of the clone stands on the task branch,
    which is exactly the condition under which the old collector ran
    ``uv run pytest --collect-only`` with the hub's whole environment.
    """
    from hub.integrations.git_ops import GitOpsIntegration
    from hub.services.steward_evidence import build_evidence_packet
    from tests.branch_code_support import TEST_FILE, make_clone

    marker = tmp_path / "outside" / "marker"
    marker.parent.mkdir()
    workspace, tip = make_clone(tmp_path, marker)
    plugins.git_ops = GitOpsIntegration()
    task_id = await _task_with_test_ac(db, client, workspace, f"{TEST_FILE}::test_ok")
    await repo.update_task(db, task_id, submission_sha=tip)
    await db.commit()

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    packet = await build_evidence_packet(db, task_id)

    assert not marker.exists(), "the branch's conftest.py ran on the hub host"
    assert spawn_spy.pytest_runs() == [], "the hub started pytest"
    assert packet is not None and packet.brief is not None
    for resolution in (
        brief["locator_resolution"][0],
        packet.brief.locator_resolution[0].model_dump(),
    ):
        assert resolution["status"] == "resolvable", resolution
        assert "without running" in resolution["reason"], resolution


# --- #1648: бриф задачи-состояния --------------------------------------------


async def test_state_brief_shows_generation_evidence_and_marks_code_checks_not_applicable(
    client: AsyncClient, db, monkeypatch
):
    """AC-2 (#1648): бриф state-задачи (REST и MCP) показывает снимок AC,
    rollback, доказательства поколения 2 с автором и observed_at, номер
    поколения и expected_generation; проверки кода — «не применимо», а не
    unknown и не match; git при сборке не трогается."""
    from hub import mcp_server
    from tests.state_support import (
        ROLLBACK,
        GitSpy,
        drive_to_second_generation,
        make_state_task,
    )

    task_id = await make_state_task(db, title="Переключить DNS")
    await drive_to_second_generation(client, db, task_id)
    spy = GitSpy()
    monkeypatch.setattr(plugins, "git_ops", spy)

    resp = await client.get(f"/api/tasks/{task_id}/review-brief")
    assert resp.status_code == 200, resp.text
    brief = resp.json()

    assert brief["sha_check"] == "not_applicable", brief["sha_check"]
    assert brief["ci_run_report"]["state"] == "not_applicable"
    assert brief["base_merge"]["state"] == "not_applicable"
    assert brief["call_sites"]["status"] == "not_applicable"
    assert brief["ac_test_results"] == [] and brief["locator_resolution"] == []
    assert brief["path_notices"] is None
    assert brief["evidence_coverage"]["state"] == "complete"
    missing = {c["check"] for c in brief["evidence_coverage"]["checks_missing"]}
    assert not missing, "ничего из кода не «отсутствует»: оно не применимо"
    na = {c["check"] for c in brief["evidence_coverage"]["checks_not_applicable"]}
    assert {"sha_check", "ci_run_report", "ac_tests", "base_merge"} <= na

    state = brief["state_review"]
    assert state["generation"] == 2 and state["expected_generation"] == 2
    assert state["label"] == "результат: состояние"
    assert state["rollback"] == ROLLBACK
    assert [a["id"] for a in state["acceptance_criteria"]] == ["AC-1", "AC-2"]
    assert state["evidence_complete"] is True
    assert {e["generation"] for e in state["evidence"]} == {2}, "поколение 1 не в брифе"
    first = state["evidence"][0]
    assert first["observed"] == "повторное наблюдение, поколение 2"
    assert first["observed_at"] == "2026-10-09T12:00:00Z"
    assert first["action"] and first["target"] and first["author"]
    assert brief["verdict_route"]["final"] == "human"
    assert brief["verdict_route"]["code"] == "state_task_no_automation"
    assert spy.calls == [], f"бриф state ходил в git: {spy.calls}"

    async def fake_get(path):
        return brief

    monkeypatch.setattr(mcp_server, "_api_get", fake_get)
    text = (await mcp_server.hub_get_review_brief(task_id)).content[0].text
    for needle in (
        "результат: состояние",
        ROLLBACK,
        "поколение 2",
        "expected_generation=2",
        "повторное наблюдение, поколение 2",
        "2026-10-09T12:00:00Z",
        "не применимо",
    ):
        assert needle in text, needle
    assert "sha_check: unknown" not in text and "Branch:" not in text
