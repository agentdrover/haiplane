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
            "findings_confirmed": [{"title": "leak", "severity": "high"}],
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
