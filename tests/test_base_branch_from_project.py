"""The integration branch comes from the project, everywhere (#475).

Three projects, three different answers to "which branch does work land on
here": the hub integrates on ``develop``, calc-kids on ``master``, spike-bo
straight on ``main``. Every gate that answered that question with a literal
answered it for one of them and silently mis-answered it for the other two.

What "silently" means differs by surface, which is why these tests reach
into more than one:

* provisioning cloned ``--branch develop`` from a repository that has no
  develop, so the workspace never appeared and the error blamed the remote;
* the drift guard dropped a project with a blank branch out of the watch
  entirely, and an unwatched base reads exactly like a clean one;
* the pre-push hook refused every push from ``master`` as an illegal branch
  name while letting a push to it through unprotected — protection inverted,
  not merely missing;
* the release gate opened a PR from the project's branch into the hub's
  release branch, which on spike-bo is the same branch on both ends.

The fallback tested here is the one the task allows and only that one: a
project that declares NO branch gets ``config.PAIR_BASE_BRANCH``. A project
that declares one is never overridden.
"""

from __future__ import annotations

import json
import os
import subprocess
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from hub import config, git_policy
from hub import repository as repo
from hub import services
from hub.integrations.noop import NoopGitOps
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
from hub.integrations.registry import plugins
from hub.services import release as release_svc
from hub.services.drift_guard import check_project
from hub.services.project_policy import base_branch_of, release_base_of

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


async def _project(
    db: aiosqlite.Connection,
    *,
    slug: str,
    default_branch: str,
    branch_policy: dict | None = None,
    gate_policy: dict | None = None,
) -> int:
    created = await repo.create_project(db, slug=slug, name=slug.title())
    project_id = created if isinstance(created, int) else created["id"]
    await repo.update_project(
        db,
        project_id,
        repo=f"mrPDA/{slug}",
        workspace_path=f"/srv/ws/{slug}",
        default_branch=default_branch,
        default_branch_policy=json.dumps(branch_policy or {}),
        gate_policy=json.dumps(gate_policy or {}),
    )
    await db.commit()
    return project_id


def _drift_row(**overrides) -> dict:
    row = {
        "id": 1,
        "slug": "calc-kids",
        "default_branch": "master",
        "workspace_path": "/srv/ws/calc-kids",
        "status": "active",
        "archived": 0,
        "drift_baseline_sha": "base000",
    }
    row.update(overrides)
    return row


class _DriftGit:
    def __init__(self):
        self.fetched: list[tuple[str, str]] = []

    async def fetch_base(self, repo_path: str, base: str):
        self.fetched.append((repo_path, base))
        return True, ""

    async def first_parent_log(self, repo_path: str, base: str, limit: int):
        return ""


def _seed_clone(tmp_path, branches: list[str]):
    """A real git repository with the repo's own pre-push hook armed."""
    work = tmp_path / "clone"
    work.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(work), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    git("init", "-q", "-b", branches[0])
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (work / "README").write_text("x")
    hooks = work / ".githooks"
    hooks.mkdir()
    hook = hooks / "pre-push"
    hook.write_text((REPO_ROOT / ".githooks" / "pre-push").read_text())
    hook.chmod(0o755)
    git("add", "-A")
    git("commit", "-qm", "seed")
    # Branched AFTER the hook is committed, so every branch carries it — a
    # checkout that removes the hook would test nothing at all.
    for extra in branches[1:]:
        git("branch", extra)
    return work, git, hook


def _run_hook(hook, work, ref: str) -> subprocess.CompletedProcess:
    """Run the hook exactly as git would: ref lines on stdin, cwd = clone."""
    sha = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return subprocess.run(
        ["sh", str(hook), "origin", "git@github.com:mrPDA/x.git"],
        input=f"refs/heads/{ref} {sha} refs/heads/{ref} {'0' * 40}\n",
        cwd=str(work),
        capture_output=True,
        text=True,
        check=False,
    )


# --------------------------------------------------------------------------
# AC-1 — a project that declares master gets master
# --------------------------------------------------------------------------


async def test_a_project_on_master_is_provisioned_and_watched_on_master(
    db, monkeypatch
):
    """The declared branch reaches both a write path (provisioning clones it)
    and a read path (the drift guard fetches it). Either one answering
    'develop' is the defect, and they used to answer it independently."""
    project_id = await _project(db, slug="calc-kids", default_branch="master")

    git = NoopGitOps()
    git.clone_repo = AsyncMock(return_value=(True, "cloned"))
    monkeypatch.setattr(plugins, "git_ops", git)

    await services.provision_project(db, project_id)

    assert git.clone_repo.await_args.args[2] == "master", (
        "provisioning must clone the branch the project declares"
    )

    drift = _DriftGit()
    monkeypatch.setattr(plugins, "git_ops", drift)
    report = await check_project(_drift_row())

    assert drift.fetched == [("/srv/ws/calc-kids", "master")]
    assert report.base_branch == "master"


def test_a_declared_branch_is_never_overridden_by_the_fallback():
    """The fallback applies to an absent answer, never to a present one — a
    fallback that can win over a declared value is a second source of truth."""
    assert base_branch_of({"default_branch": "master"}) == "master"
    assert base_branch_of({"default_branch": "main"}) == "main"
    assert base_branch_of({"default_branch": "  trunk  "}) == "trunk"


# --------------------------------------------------------------------------
# AC-2 — the fallback fires only when the project declares nothing
# --------------------------------------------------------------------------


def test_an_undeclared_branch_falls_back_to_the_configured_base():
    """Every shape of 'declares nothing' is one case, not four: the column
    missing, NULL, empty, or whitespace. Callers of a gate cannot be asked to
    tell them apart, and a gate that raises on one of them stops the task."""
    for row in ({}, {"default_branch": None}, {"default_branch": ""}, None):
        assert base_branch_of(row) == config.PAIR_BASE_BRANCH
    assert base_branch_of({"default_branch": "   "}) == config.PAIR_BASE_BRANCH


async def test_a_project_without_a_branch_is_still_watched(monkeypatch):
    """It used to drop out of the drift watch with 'project has no
    default_branch'. Nothing then watched its base, and an unwatched base
    reports the same way a clean one does — the exact failure #534 exists to
    prevent."""
    drift = _DriftGit()
    monkeypatch.setattr(plugins, "git_ops", drift)

    report = await check_project(_drift_row(default_branch=""))

    assert drift.fetched == [("/srv/ws/calc-kids", config.PAIR_BASE_BRANCH)]
    assert report.status != "unknown", (
        "a project that declares no branch is watched on the default, not dropped"
    )


# --------------------------------------------------------------------------
# AC-3 — branch protection protects the project's own branch
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base,release,pushed,blocked",
    [
        # calc-kids: master integrates, main is protected.
        ("master", "main", "master", False),
        ("master", "main", "main", True),
        # The hub's own clone, unconfigured: the documented fallback.
        ("", "", "develop", False),
        ("", "", "main", True),
        # A branch that is neither: still refused by name, on every project.
        ("master", "main", "wip", True),
        # Task branches are allowed regardless of what the base is.
        ("master", "main", "task-475/x", False),
    ],
)
def test_the_pre_push_hook_protects_the_projects_own_branch(
    tmp_path, base, release, pushed, blocked
):
    """Run the real hook the way git runs it. Before #475 a calc-kids clone
    refused every push from master as an illegal branch name AND left main
    unprotected — protection inverted, not merely absent."""
    work, git, hook = _seed_clone(tmp_path, ["develop", "master", "main", "wip"])
    if base:
        git("config", git_policy.BASE_BRANCH_KEY, base)
    if release:
        git("config", git_policy.RELEASE_BRANCH_KEY, release)
    git("checkout", "-q", pushed)

    result = _run_hook(hook, work, pushed)

    assert (result.returncode != 0) is blocked, (
        f"pushing {pushed!r} with base={base or 'unset'!r}: "
        f"rc={result.returncode} stderr={result.stderr}"
    )


def test_arming_a_workspace_records_the_branches_for_the_hook(tmp_path):
    """The hook runs offline and cannot ask the hub, so the hub leaves the
    answer in the clone's git config. Without this write the parametrised
    hook silently falls back to the hub's own branches everywhere."""
    work, git, _ = _seed_clone(tmp_path, ["master"])

    git_policy.activate_quietly(str(work), base_branch="master", release_branch="trunk")

    assert git("config", "--get", git_policy.BASE_BRANCH_KEY).stdout.strip() == "master"
    assert (
        git("config", "--get", git_policy.RELEASE_BRANCH_KEY).stdout.strip() == "trunk"
    )


def test_an_undeclared_branch_writes_no_key_at_all(tmp_path):
    """An empty value must not be written: the hook reads a key that exists as
    configured, so an empty one would defeat its own fallback."""
    work, git, _ = _seed_clone(tmp_path, ["develop"])

    written = git_policy.record_branch_policy(str(work), "", "")

    assert written == {}
    assert git("config", "--get", git_policy.BASE_BRANCH_KEY).returncode != 0


# --------------------------------------------------------------------------
# AC-4 — the release gate reads both ends from the project
# --------------------------------------------------------------------------


def test_the_release_base_is_declared_per_project():
    """``default_branch_policy.release_base`` has been offered in the UI since
    the column existed and nothing read it — dead configuration reads as a
    setting that works."""
    assert (
        release_base_of({"default_branch_policy": '{"release_base": "production"}'})
        == "production"
    )
    assert release_base_of({"default_branch_policy": "{}"}) == config.RELEASE_BRANCH
    assert release_base_of({"default_branch_policy": "{oops"}) == config.RELEASE_BRANCH


async def test_the_release_pr_runs_between_the_projects_own_branches(db, monkeypatch):
    project_id = await _project(
        db,
        slug="calc-kids",
        default_branch="master",
        branch_policy={"release_base": "production"},
        gate_policy={"release": "auto"},
    )
    task_id = await _task_in_project(db, project_id)

    git = NoopGitOps()
    git.release_range = AsyncMock(return_value=["feat(task): x (#1)"])
    # #968: content is asked first; this project has work to release.
    git.content_differs = AsyncMock(return_value=True)
    git.open_release_pr = AsyncMock(return_value=901)
    monkeypatch.setattr(plugins, "git_ops", git)

    pr = await release_svc.open_release_for_task(db, task_id)

    assert pr == 901
    assert git.release_range.await_args.args[:2] == ("production", "master")
    body = git.open_release_pr.await_args.args[3]
    assert "master → production" in body, body


async def test_a_project_that_integrates_on_its_release_branch_has_no_release(
    db, monkeypatch
):
    """spike-bo delivers straight to main. Opening a main→main PR is a failure
    reported as a release."""
    project_id = await _project(
        db,
        slug="spike-bo",
        default_branch="main",
        gate_policy={"release": "auto"},
    )
    task_id = await _task_in_project(db, project_id)

    git = NoopGitOps()
    git.release_range = AsyncMock(return_value=["feat(task): x (#1)"])
    # #968: content is asked first; this project has work to release.
    git.content_differs = AsyncMock(return_value=True)
    git.open_release_pr = AsyncMock(return_value=902)
    git.pr_for_branch = AsyncMock(return_value=903)
    git.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.passed, "checks_passed")
    )
    git.merge_pr = AsyncMock(return_value=True)
    monkeypatch.setattr(plugins, "git_ops", git)

    assert await release_svc.open_release_for_task(db, task_id) is None
    git.open_release_pr.assert_not_awaited()

    row = await repo.get_project(db, project_id)
    merged, reason = await release_svc.merge_ready_release(db, row)

    assert (merged, reason) == (False, "")
    git.merge_pr.assert_not_awaited()


async def _task_in_project(db: aiosqlite.Connection, project_id: int) -> int:
    from hub.models import TaskCreate

    tv = await services.create_task(db, TaskCreate(title="Ship me", task_type="epic"))
    await repo.update_task(db, tv.id, project_id=project_id)
    await db.commit()
    return tv.id


# --------------------------------------------------------------------------
# AC-5 — the literal cannot come back
# --------------------------------------------------------------------------

# The surfaces that must resolve the branch rather than name it. Listed by
# path because the defect this guards is not a wrong line — it is a NEW line
# written the old way in a module nobody re-checked. #576 shipped with five
# acceptance criteria pointing at tests that did not exist, and a require
# gate did not catch it; a literal is at least greppable.
_GATE_MODULES = (
    "hub/services/orchestration.py",
    "hub/services/drift_guard.py",
    "hub/services/release.py",
    "hub/services/review_brief.py",
    "hub/services/review_dispatch.py",
    "hub/services/review_evidence.py",
    "hub/integrations/protocols.py",
    "hub/integrations/noop.py",
    # #476: the module that renders workflow templates decides which branch
    # each provisioned repository runs CI on — exactly the question this guard
    # is about, in exactly the kind of module "nobody re-checked".
    "hub/services/workflow_seed.py",
)


@pytest.mark.parametrize("path", _GATE_MODULES)
def test_no_gate_resolves_the_integration_branch_with_a_literal(path):
    """Code lines only — the prose in these modules names develop and master
    deliberately, and a check that cannot tell a comment from a default is a
    check that gets deleted the first time it cries wolf."""
    offenders = []
    for number, line in enumerate((REPO_ROOT / path).read_text().splitlines(), 1):
        code = line.split("#", 1)[0]
        if '"develop"' in code or "'develop'" in code:
            offenders.append(f"{path}:{number}: {line.strip()}")
    assert not offenders, (
        "the integration branch must come from project.default_branch via "
        "project_policy.base_branch_of (or config.PAIR_BASE_BRANCH as the "
        "documented fallback), never from a literal:\n" + "\n".join(offenders)
    )


def test_the_ci_trigger_names_no_base_branch_allowlist():
    """The delivery gate merges only after reading this workflow's outcome, so
    a base the allowlist did not name produced no run at all and the probe
    answered "absent" — approved, green work with no supported path to
    delivery. YAML 1.1 parses the key ``on`` as the boolean True; that is the
    key being read here, not a typo."""
    import yaml

    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    )
    triggers = workflow.get(True, workflow.get("on"))

    assert "pull_request" in triggers
    assert not (triggers["pull_request"] or {}).get("branches"), (
        "CI must run on a pull request into any base branch; the base is a "
        "project setting, not a list maintained in this file"
    )


def test_the_only_sanctioned_fallback_is_the_configured_one():
    """config.PAIR_BASE_BRANCH is the single place the word may be written,
    and it stays env-overridable so a different deployment is a setting."""
    import sys

    from hub import config

    # In this process (no override set in CI): the configured default.
    assert config.PAIR_BASE_BRANCH == config.env_get("PAIR_BASE_BRANCH", "develop")
    # In a fresh process with no override under either prefix, the value is
    # exactly the sanctioned fallback — behaviour, not a frozen source line.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("HAIPLANE_", "OPEN" + "CLAW_"))
    }
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from hub.config import PAIR_BASE_BRANCH; print(PAIR_BASE_BRANCH)",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        check=True,
    )
    assert proc.stdout.strip() == "develop"
