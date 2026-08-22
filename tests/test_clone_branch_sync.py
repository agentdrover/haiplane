"""Changing a project's branch re-arms its clone, and drift is visible (#887).

#475 moved the branch policy out of the pre-push hook and into the clone's own
git config, where the hook — which runs offline, inside git — can read it. The
hub writes those keys in exactly two moments: when it clones a workspace, and
when it starts. Between those two moments an owner could change
``default_branch`` in the project card and nothing reached the clone: the card
showed ``master``, the hook kept protecting ``develop``, and a push from the
new integration branch was refused as an illegal branch name with no cause
recorded anywhere.

Two things are held here, and they are different things:

* the write — the change reaches the clone in the same operation, so the
  behaviour that the hook produces changes immediately (AC-1, AC-2). AC-2 runs
  the real ``.githooks/pre-push`` on a real clone, using the harness #475 built
  for exactly this: asserting on the config VALUE would pass even if the hook
  read a different key;
* the read — comparing what the project declares with what the clone records
  yields three states, never two (AC-3, AC-4). "We could not look" keeps its
  own answer and its own cause, the rule already settled in CIRunReportState
  (#546) and in sha_check (#572). Collapsing it into "agrees" is the same
  defect one level up: a state nobody observed reading as a state that is fine.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from hub import git_policy
from hub import repository as repo
from hub.services.project_policy import clone_branch_state, rearm_clone

# The harness from #475: a real repository carrying the repo's own hook, and a
# runner that feeds the hook the ref lines git feeds it. Reused rather than
# rebuilt — a second copy of "how git runs a hook" is a second thing to keep
# true, and this one is already checked by that suite.
from tests.test_base_branch_from_project import _run_hook, _seed_clone


async def _provisioned(db, workspace: Path, *, branch: str, slug: str = "ck") -> int:
    """A project whose workspace is a real clone armed on ``branch``."""
    pid = await repo.create_project(db, slug=slug, name=slug.title())
    await repo.update_project(
        db,
        pid,
        repo=f"mrPDA/{slug}",
        workspace_path=str(workspace),
        default_branch=branch,
        provision_status="ok",
    )
    await db.commit()
    git_policy.activate_quietly(str(workspace), base_branch=branch)
    return pid


def _recorded(git, key: str) -> str:
    result = git("config", "--get", key)
    return result.stdout.strip() if result.returncode == 0 else ""


# ---------------------------------------------------------------------------
# AC-1 — the change reaches the clone in the same operation
# ---------------------------------------------------------------------------


async def test_changing_default_branch_rearms_the_clone(
    client: AsyncClient, db, tmp_path
) -> None:
    """PATCH the project, read the clone. No restart, no Provision button.

    The assertion is on the key the HOOK reads, in the clone, not on the row
    that was written — the row was never the thing that broke.
    """
    work, git, _ = _seed_clone(tmp_path, ["develop", "master"])
    pid = await _provisioned(db, work, branch="develop")
    assert _recorded(git, git_policy.BASE_BRANCH_KEY) == "develop"

    resp = await client.patch(f"/api/projects/{pid}", json={"default_branch": "master"})

    assert resp.status_code == 200, resp.text
    assert _recorded(git, git_policy.BASE_BRANCH_KEY) == "master", (
        "the branch the owner set must reach the clone in the same operation; "
        "before #887 it waited for the next hub restart"
    )
    assert resp.json()["clone_branch"]["state"] == git_policy.BRANCH_IN_SYNC


async def test_the_release_base_reaches_the_clone_too(
    client: AsyncClient, db, tmp_path
) -> None:
    """The other key of the same policy, changed through the other field.

    ``release_base`` lives in ``default_branch_policy``, so an implementation
    that watched only ``default_branch`` would leave the hook protecting the
    previous release branch — the identical window, one field over.
    """
    work, git, _ = _seed_clone(tmp_path, ["develop"])
    pid = await _provisioned(db, work, branch="develop", slug="rel")

    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"default_branch_policy": {"release_base": "trunk"}},
    )

    assert resp.status_code == 200, resp.text
    assert _recorded(git, git_policy.RELEASE_BRANCH_KEY) == "trunk"


async def test_rearming_is_idempotent_and_leaves_other_keys_alone(db, tmp_path) -> None:
    """Run twice, change nothing else. A repair step that edits a clone has to
    be safe to run on every project edit, including the ones that changed
    nothing about branches."""
    work, git, _ = _seed_clone(tmp_path, ["master"])
    git("config", "user.name", "Someone Else")
    git("config", "openclaw.somethingElse", "keep me")
    pid = await _provisioned(db, work, branch="master", slug="idem")
    row = await repo.get_project(db, pid)

    first = rearm_clone(row)
    second = rearm_clone(row)

    assert first is not None and second is not None
    assert first.state == second.state
    assert _recorded(git, git_policy.BASE_BRANCH_KEY) == "master"
    assert _recorded(git, "user.name") == "Someone Else"
    assert _recorded(git, "openclaw.somethingElse") == "keep me"


async def test_an_edit_that_touches_no_branch_touches_no_clone(
    client: AsyncClient, db, tmp_path
) -> None:
    """Renaming a project must not reach into a git config at all."""
    work, _, _ = _seed_clone(tmp_path, ["develop"])
    pid = await _provisioned(db, work, branch="develop", slug="rename")

    with patch("hub.services.project_policy.rearm_clone") as rearm:
        resp = await client.patch(f"/api/projects/{pid}", json={"name": "New Name"})

    assert resp.status_code == 200, resp.text
    assert not rearm.called, (
        "an edit to a field the hook never reads must not rewrite git config"
    )


async def test_the_project_card_form_rearms_the_clone_too(
    client: AsyncClient, db, tmp_path
) -> None:
    """The surface an owner actually uses.

    The bug report describes a person editing the project CARD, not calling
    PATCH. The form is a thin wrapper over the same handler, and this test is
    what says so — connecting the rearm to the first surface a grep finds and
    stopping there is how the other surfaces keep the old behaviour.
    """
    work, git, _ = _seed_clone(tmp_path, ["develop", "master"])
    pid = await _provisioned(db, work, branch="develop", slug="via-form")

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"name": "Via Form", "default_branch": "master"},
        follow_redirects=False,
    )

    assert resp.status_code == 303, resp.text
    assert _recorded(git, git_policy.BASE_BRANCH_KEY) == "master"


# ---------------------------------------------------------------------------
# AC-2 — the hook itself, on a real clone, right after the change
# ---------------------------------------------------------------------------


async def test_hook_protects_the_new_branch_immediately(
    client: AsyncClient, db, tmp_path
) -> None:
    """Run the real hook the way git runs it, before and after the change.

    The value in the config is a proxy; the push verdict is the thing the owner
    experiences. Before #887, ``master`` was refused as an illegal branch name
    while ``develop`` was still treated as the integration branch — protection
    aimed at a branch the project had left.
    """
    work, git, hook = _seed_clone(tmp_path, ["develop", "master"])
    pid = await _provisioned(db, work, branch="develop", slug="switch")

    git("checkout", "-q", "master")
    before = _run_hook(hook, work, "master")
    assert before.returncode != 0, (
        "precondition: while the clone still records develop, a push from "
        "master is refused — this is the state the owner is stuck in"
    )

    resp = await client.patch(f"/api/projects/{pid}", json={"default_branch": "master"})
    assert resp.status_code == 200, resp.text

    after = _run_hook(hook, work, "master")
    assert after.returncode == 0, (
        "the new integration branch must be pushable at once: "
        f"rc={after.returncode} stderr={after.stderr}"
    )

    git("checkout", "-q", "develop")
    old = _run_hook(hook, work, "develop")
    assert old.returncode != 0, (
        "the branch the project left is no longer the integration branch, so "
        "the hook refuses it by name like any other non-task branch"
    )


# ---------------------------------------------------------------------------
# AC-3 — divergence names both values
# ---------------------------------------------------------------------------


async def test_divergence_names_both_values(db, tmp_path) -> None:
    """A report naming one side says something is wrong, not what to change."""
    work, git, _ = _seed_clone(tmp_path, ["develop", "master"])
    pid = await _provisioned(db, work, branch="develop", slug="drift")
    # The window this task closes, reproduced: the row moved, the clone did not.
    await repo.update_project(db, pid, default_branch="master")
    await db.commit()

    state = clone_branch_state(await repo.get_project(db, pid))

    assert state.state == git_policy.BRANCH_DIVERGED
    assert state.project_branch == "master"
    assert state.clone_branch == "develop"
    assert "master" in state.reason and "develop" in state.reason
    assert not state.agrees
    assert _recorded(git, git_policy.BASE_BRANCH_KEY) == "develop"


async def test_a_clone_that_records_nothing_is_not_agreement(db, tmp_path) -> None:
    """We looked, and the hook is falling back to its own built-in branch.

    That is a divergence, not an unknown: the difference from AC-4 is whether
    the clone could be read at all. The reason has to say which key is missing,
    because the fix is to arm the clone rather than to change the project.
    """
    work, _, _ = _seed_clone(tmp_path, ["master"])
    pid = await repo.create_project(db, slug="bare", name="Bare")
    await repo.update_project(
        db, pid, workspace_path=str(work), default_branch="master"
    )
    await db.commit()

    state = clone_branch_state(await repo.get_project(db, pid))

    assert state.state == git_policy.BRANCH_DIVERGED
    assert state.project_branch == "master"
    assert state.clone_branch == ""
    assert git_policy.BASE_BRANCH_KEY in state.reason


async def test_the_card_and_the_api_read_the_same_state(
    client: AsyncClient, db, tmp_path
) -> None:
    """Two surfaces, one reader — a divergence visible in one place only is
    how two answers to one question start to disagree (#475's own lesson)."""
    from hub.services.dashboard import get_project_cards

    work, _, _ = _seed_clone(tmp_path, ["develop"])
    pid = await _provisioned(db, work, branch="develop", slug="both")
    await repo.update_project(db, pid, default_branch="master")
    await db.commit()

    resp = await client.get("/api/projects")
    assert resp.status_code == 200, resp.text
    from_api = next(p for p in resp.json() if p["id"] == pid)["clone_branch"]
    cards = await get_project_cards(db)
    from_card = next(c for c in cards if c["project"]["id"] == pid)["clone_branch"]

    assert from_api["state"] == from_card.state == git_policy.BRANCH_DIVERGED
    assert from_api["clone_branch"] == from_card.clone_branch == "develop"
    assert from_api["project_branch"] == from_card.project_branch == "master"


# ---------------------------------------------------------------------------
# AC-4 — "could not look" is its own state, with a cause
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "workspace,expected_in_reason",
    [
        ("", "workspace_path"),
        ("/nonexistent/workspace/for/887", "не существует"),
    ],
)
async def test_unreadable_clone_is_its_own_state(
    db, tmp_path, workspace, expected_in_reason
) -> None:
    """A project with no workspace and a workspace that is not there are both
    unknown — never ``match``. Each says why, because the two call for
    different acts: provision the project, or find out where the disk went."""
    pid = await repo.create_project(db, slug="unseen", name="Unseen")
    await repo.update_project(
        db, pid, workspace_path=workspace, default_branch="master"
    )
    await db.commit()

    state = clone_branch_state(await repo.get_project(db, pid))

    assert state.state == git_policy.BRANCH_UNCHECKED
    assert state.state != git_policy.BRANCH_IN_SYNC
    assert not state.agrees, (
        "'could not look' must not read as agreement — the rule of "
        "CIRunReportState (#546) and sha_check (#572)"
    )
    assert expected_in_reason in state.reason
    assert state.project_branch == "master", (
        "the unknown still names what the project declares; only the clone "
        "side is missing"
    )


async def test_a_directory_that_is_not_a_repository_is_unknown(db, tmp_path) -> None:
    """The workspace exists on disk and is not a clone — the state that
    provisioning half-finished. Reading a config out of it would answer
    "no key recorded", which is the wrong sentence entirely."""
    plain = tmp_path / "not-a-clone"
    plain.mkdir()
    pid = await repo.create_project(db, slug="plain", name="Plain")
    await repo.update_project(
        db, pid, workspace_path=str(plain), default_branch="master"
    )
    await db.commit()

    state = clone_branch_state(await repo.get_project(db, pid))

    assert state.state == git_policy.BRANCH_UNCHECKED
    assert "git" in state.reason


async def test_the_unchecked_default_never_claims_agreement() -> None:
    """The API model's default. A reader that never filled the field must not
    be mistaken for one that looked and found agreement."""
    from hub.models import CloneBranchState, ProjectView

    assert CloneBranchState().state == git_policy.BRANCH_UNCHECKED
    assert CloneBranchState().reason, "an unknown without a cause is a blank"
    view = ProjectView(id=1, slug="x", name="X")
    assert view.clone_branch.state == git_policy.BRANCH_UNCHECKED


# ---------------------------------------------------------------------------
# Provisioning repairs a clone as well — the path that runs on production
# ---------------------------------------------------------------------------


async def test_provisioning_rearms_an_existing_clone(db, tmp_path) -> None:
    """Every workspace on the server already exists, so "existing clone
    verified" is the only provisioning path that ever runs there (#532's own
    finding). It has to be a repair, not a no-op."""
    from hub import services

    work, git, _ = _seed_clone(tmp_path, ["master"])
    pid = await repo.create_project(db, slug="prov", name="Prov")
    await repo.update_project(
        db,
        pid,
        repo="mrPDA/prov",
        workspace_path=str(work),
        default_branch="master",
        default_branch_policy=json.dumps({"release_base": "trunk"}),
    )
    await db.commit()

    with patch(
        "hub.integrations.registry.plugins.git_ops.clone_repo",
        AsyncMock(return_value=(True, "existing clone verified, origin fetched")),
    ):
        result = await services.provision_project(db, pid)

    assert result["provision_status"] == "ok", result
    assert _recorded(git, git_policy.BASE_BRANCH_KEY) == "master"
    assert _recorded(git, git_policy.RELEASE_BRANCH_KEY) == "trunk", (
        "clone_repo records only the base branch; the release branch reaches "
        "the clone through the same writer or not at all"
    )
