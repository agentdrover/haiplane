"""Provisioning lays workflow templates into the project's repository (#476).

The gate merges a pull request only after reading a GitHub Actions outcome
(#605/#606). A repository with no workflow produces no run, so the probe
answers ``absent`` and the gate refuses with ``ci_absent`` — approved, green
work with no supported path to delivery. Until this task, ``.github/workflows``
existed in the hub's own repository and nowhere else, and provisioning wrote
no files at all: a clone and two git-config keys.

The tests below run against a REAL git repository with a real bare origin,
because every interesting property of this feature is a git property — what is
committed, what is pushed, and what is left untouched when something refuses.
A mocked git would confirm the call and prove nothing about the tree.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import yaml

from hub import repository as repo
from hub import services
from hub.integrations.registry import plugins
from hub.services import workflow_seed
from hub.services.project_policy import ci_runner_of

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = REPO_ROOT / "hub" / "workflow_templates"

CI_FILE = "haiplane-ci.yml"
STALE_FILE = "haiplane-stale.yml"
CI_FILE_LEGACY = "openclaw-ci.yml"
STALE_FILE_LEGACY = "openclaw-stale.yml"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _repo_pair(tmp_path: Path, branch: str) -> tuple[Path, Path]:
    """A working clone on ``branch`` plus the bare origin it pushes to."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", branch, str(origin)],
        check=True,
        capture_output=True,
    )
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", branch)
    _git(work, "config", "user.name", "Tester")
    _git(work, "config", "user.email", "tester@example.com")
    (work / "README.md").write_text("x\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "initial")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-u", "origin", branch)
    return work, origin


def _workflow(work: Path, name: str) -> dict:
    text = (work / ".github" / "workflows" / name).read_text(encoding="utf-8")
    # YAML 1.1 parses the bare key ``on`` as the boolean True — the same
    # reading the #475 guard on the hub's own workflow does.
    return yaml.safe_load(text)


def _triggers(document: dict) -> dict:
    return document.get(True, document.get("on"))


async def _project(
    db: aiosqlite.Connection,
    *,
    slug: str = "satellite",
    workspace_path: str,
    default_branch: str = "master",
    release_base: str = "",
    gate_policy: dict | None = None,
) -> int:
    project_id = await repo.create_project(db, slug=slug, name=slug.title())
    await repo.update_project(
        db,
        project_id,
        repo=f"mrPDA/{slug}",
        workspace_path=workspace_path,
        default_branch=default_branch,
        default_branch_policy=json.dumps(
            {"release_base": release_base} if release_base else {}
        ),
        gate_policy=json.dumps(gate_policy or {}),
    )
    await db.commit()
    return project_id


# --------------------------------------------------------------------------
# AC-1 — a repository with no workflows gets them, on its own branches
# --------------------------------------------------------------------------


async def test_provisioning_lays_the_workflows_into_a_repository_that_has_none(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch
):
    """The write path end to end: the Provision button on a project that
    declares ``master`` leaves both workflows committed AND pushed, and the CI
    one names master where a branch is named at all. A file that reaches the
    working tree but not the remote changes nothing about delivery — GitHub
    runs what is on GitHub."""
    work, origin = _repo_pair(tmp_path, "master")
    project_id = await _project(
        db, workspace_path=str(work), default_branch="master", gate_policy={}
    )

    monkeypatch.setattr(
        plugins.git_ops, "clone_repo", AsyncMock(return_value=(True, "cloned"))
    )
    result = await services.provision_project(db, project_id)

    assert result["provision_status"] == "ok"
    for name in (CI_FILE, STALE_FILE):
        assert (work / ".github" / "workflows" / name).is_file(), (
            f"{name} must reach the working tree"
        )

    pushed = _git(origin, "ls-tree", "-r", "--name-only", "master")
    assert f".github/workflows/{CI_FILE}" in pushed
    assert f".github/workflows/{STALE_FILE}" in pushed

    ci = _workflow(work, CI_FILE)
    assert _triggers(ci)["push"]["branches"][0] == "master", (
        "the seeded workflow must name the branch the PROJECT integrates on"
    )
    assert "@@" not in (work / ".github" / "workflows" / CI_FILE).read_text(), (
        "an unresolved placeholder is a ref pattern that matches nothing: the "
        "workflow sits there looking installed and never runs"
    )


async def test_the_project_declares_the_test_runner_and_the_template_takes_it(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch
):
    """The hub does not know how this repository runs its tests, so it reads
    the answer from the project instead of guessing one. An undeclared runner
    falls back to the shared action's documented default, never to a build
    command invented for a repository nobody read."""
    work, _ = _repo_pair(tmp_path, "main")
    project_id = await _project(
        db,
        slug="pnpm-shop",
        workspace_path=str(work),
        default_branch="main",
        gate_policy={"ci_runner": "pnpm test"},
    )
    monkeypatch.setattr(
        plugins.git_ops, "clone_repo", AsyncMock(return_value=(True, "cloned"))
    )

    await services.provision_project(db, project_id)

    step = _workflow(work, CI_FILE)["jobs"]["test"]["steps"][-1]
    assert step["with"]["ac-runner"] == "pnpm test"
    assert ci_runner_of({"gate_policy": "{}"}) == ""
    assert (
        workflow_seed.render(
            "ci.yml", base_branch="main", release_branch="main", ac_runner=""
        ).count(workflow_seed.DEFAULT_AC_RUNNER)
        == 1
    )


# --------------------------------------------------------------------------
# AC-2 — the templates name no branch of their own
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(workflow_seed.SEEDED_WORKFLOWS))
def test_the_templates_name_no_branch_and_leave_no_placeholder(name: str):
    """A template that named develop would be correct for the hub and wrong
    for calc-kids and spike-bo, which is the whole defect #475 removed from
    the gates — a new file is exactly where it comes back. And a placeholder
    the renderer does not know must raise rather than ship empty."""
    raw = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    for branch in ("develop", "master", "trunk"):
        assert f'"{branch}"' not in raw, (
            f"{name} names the branch {branch!r} instead of taking a placeholder"
        )

    rendered = workflow_seed.render(
        name, base_branch="trunk", release_branch="production"
    )
    assert "@@" not in rendered
    assert workflow_seed._PLACEHOLDER.search(rendered) is None

    with pytest.raises(ValueError):
        workflow_seed.render(name, base_branch="", release_branch="production")


def test_the_seeded_ci_trigger_names_no_base_branch_allowlist():
    """The mistake #475 fixed in the hub's own workflow, in the file that
    would have copied it into every project: an allowlist that does not name a
    PR's base produces no run, the probe answers absent, and the gate refuses
    approved work. The base is a project setting, never a list in a file."""
    document = yaml.safe_load(
        workflow_seed.render("ci.yml", base_branch="trunk", release_branch="production")
    )
    triggers = _triggers(document)

    assert "pull_request" in triggers
    assert not (triggers["pull_request"] or {}).get("branches"), (
        "the seeded CI must run on a pull request into any base branch"
    )
    assert "trunk" in triggers["push"]["branches"]
    assert "production" in triggers["push"]["branches"]


def test_the_stale_template_closes_only_pull_requests_and_honours_keep():
    """#460 is about pull requests piling up. Issues have a different
    lifetime, and a timer that closes them was never asked for; an explicit
    ``keep`` outranks the timer, or the only way to hold a long PR open is to
    poke it on a schedule — the busywork this removes."""
    document = yaml.safe_load(
        workflow_seed.render(
            "stale.yml", base_branch="trunk", release_branch="production"
        )
    )
    settings = document["jobs"]["stale"]["steps"][0]["with"]

    assert document["jobs"]["stale"]["steps"][0]["uses"].startswith("actions/stale@")
    assert settings["days-before-issue-stale"] == -1
    assert settings["days-before-issue-close"] == -1
    assert settings["days-before-pr-stale"] > 0
    assert settings["days-before-pr-close"] > 0
    assert "keep" in str(settings["exempt-pr-labels"]).split(",")


# --------------------------------------------------------------------------
# AC-3 — idempotence: a second provision adds nothing
# --------------------------------------------------------------------------


async def test_a_second_provision_adds_nothing(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch
):
    """The constraint on the task, stated as the thing that would go wrong:
    the Provision button is pressed again and the repository grows a second
    commit — or a second copy — every time."""
    work, _ = _repo_pair(tmp_path, "master")
    project_id = await _project(db, workspace_path=str(work), default_branch="master")
    monkeypatch.setattr(
        plugins.git_ops, "clone_repo", AsyncMock(return_value=(True, "verified"))
    )

    await services.provision_project(db, project_id)
    after_first = _git(work, "rev-parse", "HEAD")
    count_first = _git(work, "rev-list", "--count", "HEAD")

    again = await services.provision_project(db, project_id)

    assert _git(work, "rev-parse", "HEAD") == after_first
    assert _git(work, "rev-list", "--count", "HEAD") == count_first
    assert "already carries workflows" in again["provision_detail"]
    assert len(list((work / ".github" / "workflows").iterdir())) == 2


# --------------------------------------------------------------------------
# AC-4 — a repository with CI of its own is never edited
# --------------------------------------------------------------------------


async def test_a_repository_with_its_own_workflow_is_left_alone(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch
):
    """The rule that keeps the hub out of somebody else's pipeline — and the
    reason this task does not touch the hub's own repository, which carries
    ci.yml. A repository that already runs something on a pull request has
    answered the question the gate asks; adding a second opinion beside it, or
    overwriting the first, is the hub editing CI it was never given."""
    work, origin = _repo_pair(tmp_path, "master")
    workflows = work / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: theirs\non: [pull_request]\n", "utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "their own CI")
    _git(work, "push", "origin", "master")
    before = _git(work, "rev-parse", "HEAD")

    project_id = await _project(db, workspace_path=str(work), default_branch="master")
    monkeypatch.setattr(
        plugins.git_ops, "clone_repo", AsyncMock(return_value=(True, "verified"))
    )
    result = await services.provision_project(db, project_id)

    assert result["provision_status"] == "ok"
    assert sorted(p.name for p in workflows.iterdir()) == ["ci.yml"]
    assert _git(work, "rev-parse", "HEAD") == before
    assert "already carries workflows" in result["provision_detail"]


def test_legacy_only_workflows_are_present_without_second_pair(tmp_path: Path):
    """Haiplane rebrand (Wave 3): a repository the hub seeded before the
    rename carries only openclaw-ci.yml / openclaw-stale.yml. Those files are
    hub-owned and complete — the answer is PRESENT, and no haiplane-* pair is
    written beside them."""
    work, _ = _repo_pair(tmp_path, "develop")
    workflows = work / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / CI_FILE_LEGACY).write_text("name: OpenClaw CI\n", "utf-8")
    (workflows / STALE_FILE_LEGACY).write_text("name: OpenClaw stale\n", "utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "seeded before the rename")

    result = workflow_seed.seed_project_workflows(
        str(work), base_branch="develop", release_branch="main", push=False
    )

    assert result.state == workflow_seed.PRESENT
    assert result.written == ()
    assert sorted(p.name for p in workflows.iterdir()) == sorted(
        [CI_FILE_LEGACY, STALE_FILE_LEGACY]
    ), "the hub must never lay a second pair beside its legacy-named files"


def test_the_seeded_names_are_the_haiplane_pair():
    """Fresh repositories get the new names; the legacy pair stays known so
    pre-rename repositories read as hub-owned."""
    assert set(workflow_seed.SEEDED_WORKFLOWS.values()) == {CI_FILE, STALE_FILE}
    assert workflow_seed.LEGACY_SEEDED == {CI_FILE_LEGACY, STALE_FILE_LEGACY}


# --------------------------------------------------------------------------
# AC-5 — seeding never damages the clone and never fails provisioning
# --------------------------------------------------------------------------


async def test_a_push_that_fails_leaves_the_clone_as_it_was(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch
):
    """A read-only deploy key, or a token without the ``workflow`` scope, is
    the ordinary case — not an exotic one. The commit must not survive the
    failed push: a local commit sitting on the integration branch ahead of its
    remote is exactly the shape the drift guard shouts about, and the next
    push from any task branch would carry it along unannounced. Provisioning
    still answers ok, because the clone did work."""
    work, origin = _repo_pair(tmp_path, "master")
    before = _git(work, "rev-parse", "HEAD")
    _git(work, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))

    project_id = await _project(db, workspace_path=str(work), default_branch="master")
    monkeypatch.setattr(
        plugins.git_ops, "clone_repo", AsyncMock(return_value=(True, "cloned"))
    )
    result = await services.provision_project(db, project_id)

    assert result["provision_status"] == "ok", (
        "a clone that worked is still a clone that worked"
    )
    assert "could not push" in result["provision_detail"]
    assert _git(work, "rev-parse", "HEAD") == before
    assert _git(work, "status", "--porcelain") == ""
    assert not (work / ".github" / "workflows" / CI_FILE).exists()


def test_a_dirty_or_wrong_branch_workspace_is_refused_not_swept_up(tmp_path: Path):
    """Committing beside somebody else's uncommitted work would sweep it into
    the hub's commit, and the pre-push hook blocks such a push anyway (#532).
    Standing on another branch is refused rather than switched: the workflows
    have to reach the branch task branches are cut from, and moving a
    workspace out from under whoever is using it is not the hub's call."""
    work, _ = _repo_pair(tmp_path, "master")
    (work / "someone-elses-work.txt").write_text("wip\n", encoding="utf-8")

    dirty = workflow_seed.seed_project_workflows(
        str(work), base_branch="master", release_branch="main"
    )
    assert dirty.state == workflow_seed.UNAVAILABLE
    assert "uncommitted" in dirty.detail
    assert not (work / ".github").exists()

    (work / "someone-elses-work.txt").unlink()
    _git(work, "checkout", "-b", "task-1/elsewhere")
    elsewhere = workflow_seed.seed_project_workflows(
        str(work), base_branch="master", release_branch="main"
    )
    assert elsewhere.state == workflow_seed.UNAVAILABLE
    assert "not master" in elsewhere.detail


def test_seeding_never_raises_and_never_fails_provisioning(tmp_path: Path):
    """Provisioning must always answer (#347): its outcome is a line an
    operator reads, not an exception. Every refusal is a state and a sentence,
    including the one where there is no workspace at all."""
    absent = workflow_seed.seed_project_workflows(
        str(tmp_path / "nothing-here"), base_branch="master", release_branch="main"
    )
    assert absent.state == workflow_seed.UNAVAILABLE
    assert absent.changed is False

    work, _ = _repo_pair(tmp_path, "master")
    undeclared = workflow_seed.seed_project_workflows(
        str(work), base_branch="", release_branch="main"
    )
    assert undeclared.state == workflow_seed.UNAVAILABLE

    def _explode(*_args, **_kwargs):
        raise RuntimeError("git went missing")

    original = workflow_seed.existing_workflows
    workflow_seed.existing_workflows = _explode  # type: ignore[assignment]
    try:
        crashed = workflow_seed.seed_project_workflows(
            str(work), base_branch="master", release_branch="main"
        )
    finally:
        workflow_seed.existing_workflows = original  # type: ignore[assignment]
    assert crashed.state == workflow_seed.FAILED
    assert "git went missing" in crashed.detail
