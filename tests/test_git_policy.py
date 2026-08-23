"""The pre-push hook is armed, and the fact is checkable (#532).

These tests drive real git against real repositories on disk. Mocking git here
would test my idea of git rather than git: the whole question is whether a
push is actually refused, and only git can answer it. The hook under test is
the repository's own ``.githooks/pre-push``, copied in — not a stand-in, so a
change to the real policy is felt here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hub import brand, git_policy, repository as repo

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_HOOK = REPO_ROOT / ".githooks" / "pre-push"


def _git(repo: Path | str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
        },
    )


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clone carrying the repository's real hook, with a remote to push to."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, timeout=30)

    repo = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=30)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "checkout", "-q", "-b", "develop")

    hooks = repo / ".githooks"
    hooks.mkdir()
    shutil.copy2(REAL_HOOK, hooks / "pre-push")
    (hooks / "pre-push").chmod(0o755)
    (repo / "file.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


# ---- AC-1: setup arms the hook, and the hook then actually runs ----


def test_activation_makes_git_run_the_hook(clone: Path):
    """The point is not the config value — it is that a push gets checked.

    Asserting core.hooksPath alone would pass on a clone where the hook is
    missing or unreadable and nothing would ever run.
    """
    assert git_policy.inspect(str(clone)).state == git_policy.INACTIVE

    status = git_policy.activate(str(clone))

    assert status.enforced, status.reason
    assert (
        _git(clone, "config", "--get", "core.hooksPath").stdout.strip() == ".githooks"
    )

    _git(clone, "checkout", "-q", "-b", "not-a-valid-name")
    pushed = _git(clone, "push", "origin", "not-a-valid-name")
    assert pushed.returncode != 0, "the hook is armed but the push went through"
    assert "Blocked push from branch" in pushed.stderr


def test_activation_is_idempotent(clone: Path):
    """A setup step people re-run must not punish them for it."""
    first = git_policy.activate(str(clone))
    second = git_policy.activate(str(clone))

    assert first.enforced and second.enforced
    assert _git(clone, "config", "--get-all", "core.hooksPath").stdout.split() == [
        ".githooks"
    ], "a repeated setup must not stack duplicate values"


# ---- AC-1, the half I missed: the step a PERSON runs ----


def test_the_bootstrap_step_arms_a_clone(clone: Path):
    """AC-1 asks for a setup step, and I had shipped only a command to
    remember. The hub arms the clones it makes itself; a developer's clone was
    still left to memory — which is the state the task was opened about.

    This runs the real make target against a real unarmed clone rather than
    reading the Makefile. `setup` also builds the venv and installs the
    package; those are not exercised here — `hooks` is the part that arms, and
    `setup` invokes this very target.
    """
    assert shutil.which("make"), "the documented bootstrap step needs make"
    assert not git_policy.inspect(str(clone)).enforced, "precondition"

    ran = subprocess.run(
        ["make", "hooks", f"REPO={clone}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert ran.returncode == 0, ran.stderr
    assert git_policy.inspect(str(clone)).canonical, (
        "the documented setup step ran and left the clone unprotected"
    )

    _git(clone, "checkout", "-q", "-b", "whatever-i-feel-like")
    assert _git(clone, "push", "origin", "whatever-i-feel-like").returncode != 0


def test_setup_runs_the_arming_target(clone: Path):
    """`hooks` is proven above by running it. What this adds is that `setup` —
    the command the README tells a person to run — actually reaches it.
    """
    ran = subprocess.run(
        ["make", "-n", "setup"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert ran.returncode == 0, ran.stderr
    # The command itself, expanded through the sub-make — not the word
    # "hooks", which a stub echoing that word would satisfy just as well. The
    # first version of this assertion did exactly that and survived mutation.
    assert "oc-git-policy activate" in ran.stdout, (
        "setup must reach the arming command, or the README documents a step "
        f"that leaves the clone unprotected. Dry run was:\n{ran.stdout}"
    )


# ---- AC-3: an armed hook refuses a branch outside the whitelist ----


def test_a_branch_outside_the_whitelist_is_refused(clone: Path):
    git_policy.activate(str(clone))
    _git(clone, "checkout", "-q", "-b", "my-experiment")

    pushed = _git(clone, "push", "origin", "my-experiment")

    assert pushed.returncode != 0
    assert "task-<hub-task-id>/<short-slug>" in pushed.stderr, (
        "the refusal must name the shapes that are allowed"
    )


def test_a_task_branch_still_goes_through(clone: Path):
    """The guard has to let correct work past, or it gets turned off."""
    git_policy.activate(str(clone))
    _git(clone, "checkout", "-q", "-b", "task-532/pre-push-bootstrap")

    pushed = _git(clone, "push", "origin", "task-532/pre-push-bootstrap")

    assert pushed.returncode == 0, pushed.stderr


# ---- AC-2: the doctor makes the state a fact, not an assumption ----


def test_doctor_fails_and_the_printed_command_actually_works(
    clone: Path, tmp_path: Path, capsys
):
    """Asserting that some command was printed is not the same as the reader
    being able to run it. The doctor takes a path and is routinely run from
    elsewhere, so the printed line is executed here from a DIFFERENT working
    directory and the target clone must come out activated (#532 review).
    """
    rc = git_policy.main(["doctor", str(clone)])

    assert rc == 1
    out = capsys.readouterr().out
    assert "NOT ACTIVE" in out
    printed = next(line for line in out.splitlines() if line.startswith("run: "))
    command = printed[len("run: ") :]

    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=elsewhere, check=True, timeout=30)
    ran = subprocess.run(
        command, shell=True, cwd=elsewhere, capture_output=True, text=True, timeout=30
    )

    assert ran.returncode == 0, ran.stderr
    assert git_policy.inspect(str(clone)).canonical, (
        "the printed command ran and the target clone is still unprotected"
    )
    assert _git(elsewhere, "config", "--get", "core.hooksPath").stdout.strip() == "", (
        "it configured the caller's own clone instead of the one asked about"
    )


def test_the_printed_command_survives_a_path_with_a_space(tmp_path: Path, capsys):
    """A path is printed to be pasted into a shell. Unquoted, a directory with
    a space splits into two arguments and the pasted line silently does
    nothing while the doctor keeps saying NOT ACTIVE (#532 review).
    """
    awkward = tmp_path / "oc hub" / "clone"
    awkward.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(awkward)], check=True, timeout=30)
    hooks = awkward / ".githooks"
    hooks.mkdir()
    shutil.copy2(REAL_HOOK, hooks / "pre-push")
    (hooks / "pre-push").chmod(0o755)

    assert git_policy.main(["doctor", str(awkward)]) == 1
    printed = next(
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("run: ")
    )
    ran = subprocess.run(
        printed[len("run: ") :],
        shell=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert ran.returncode == 0, ran.stderr
    assert git_policy.inspect(str(awkward)).canonical, (
        "the command was printed, pasted, reported success — and armed nothing"
    )


def test_doctor_passes_once_activated(clone: Path, capsys):
    git_policy.activate(str(clone))

    rc = git_policy.main(["doctor", str(clone)])

    assert rc == 0
    assert "ACTIVE" in capsys.readouterr().out


def test_a_repository_without_the_hook_is_not_a_failure(tmp_path: Path, capsys):
    """calc-kids on production is such a repository: its own remote, no
    .githooks. Reporting that as broken would send the operator chasing a
    configuration that cannot exist there."""
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True, timeout=30)

    status = git_policy.inspect(str(other))
    rc = git_policy.main(["doctor", str(other)])

    assert status.state == git_policy.ABSENT
    assert rc == 0
    assert "NOT APPLICABLE" in capsys.readouterr().out


def test_activation_does_not_pretend_where_there_is_no_hook(tmp_path: Path):
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True, timeout=30)

    status = git_policy.activate(str(other))

    assert status.state == git_policy.ABSENT
    assert _git(other, "config", "--get", "core.hooksPath").stdout.strip() == "", (
        "a key pointing at nothing records protection that cannot happen"
    )


def test_a_hook_deleted_from_the_working_tree_is_broken_not_irrelevant(clone: Path):
    """Two repositories look the same to a file check: one never had the hook,
    the other lost it. Answering NOT APPLICABLE for the second files broken
    protection under "nothing to do here" (#532 review)."""
    git_policy.activate(str(clone))
    (clone / ".githooks" / "pre-push").unlink()

    status = git_policy.inspect(str(clone))

    assert status.state == git_policy.INACTIVE, (
        "a tracked hook missing from the checkout is a break, not an absence"
    )
    assert "tracked in git but missing" in status.reason
    assert "checkout --" in status.fix, (
        "the fix is to restore the file, not to reconfigure"
    )

    # And the claim is true: nothing checks the push in this state.
    _git(clone, "checkout", "-q", "-b", "definitely-not-allowed")
    assert _git(clone, "push", "origin", "definitely-not-allowed").returncode == 0


def test_a_configured_path_with_no_hook_in_it_is_not_active(clone: Path):
    """The failure this check exists for: the key is set, so a reader who
    greps the config concludes the policy is on — and git skips the push,
    because there is no hook where the key points."""
    (clone / "empty-dir").mkdir()
    _git(clone, "config", "core.hooksPath", "empty-dir")

    status = git_policy.inspect(str(clone))

    assert not status.enforced
    assert "no pre-push exists there" in status.reason


def test_a_hook_that_cannot_be_executed_is_not_active(clone: Path):
    git_policy.activate(str(clone))
    (clone / ".githooks" / "pre-push").chmod(0o644)

    status = git_policy.inspect(str(clone))

    assert not status.enforced
    assert "chmod" in status.fix


def test_a_hook_copied_into_git_hooks_is_reported_as_working(clone: Path, capsys):
    """docs/repository-rules.md used to tell people to COPY the hook into
    .git/hooks. In such a clone the push really is checked, so calling it
    "not active" would send someone to fix a working setup — and a checker
    that cries wolf gets muted. It is still worth naming: a copy stops
    matching the policy the moment the hook changes, and nothing says so.
    """
    hooks = clone / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REAL_HOOK, hooks / "pre-push")
    (hooks / "pre-push").chmod(0o755)

    status = git_policy.inspect(str(clone))
    rc = git_policy.main(["doctor", str(clone)])
    out = capsys.readouterr().out

    assert status.state == git_policy.COPIED
    assert status.enforced, "the push is genuinely checked here"
    assert not status.canonical, "but not by the repository's own file"
    assert rc == 0
    assert "COPY" in out
    assert git_policy.activate_command(str(clone)) in out, (
        "the upgrade command must name the clone it applies to"
    )

    # And the claim is true: the copy does refuse a bad branch.
    _git(clone, "checkout", "-q", "-b", "still-not-valid")
    assert _git(clone, "push", "origin", "still-not-valid").returncode != 0


def test_activation_upgrades_a_copy_to_the_repository_s_own_hook(clone: Path):
    hooks = clone / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REAL_HOOK, hooks / "pre-push")
    (hooks / "pre-push").chmod(0o755)

    status = git_policy.activate(str(clone))

    assert status.canonical, "a copy that can go stale is not the end state"


# ---- AC-4: a worktree behaves like the clone it came from ----


def test_a_worktree_is_covered_by_the_clone_s_activation(clone: Path, tmp_path: Path):
    """Checked against real git rather than assumed: the task listed worktree
    inheritance as an assumption to verify. core.hooksPath lives in the shared
    config, and a relative value resolves against each worktree's own top
    level — so a worktree runs its own checkout's hook, not the main clone's.
    """
    git_policy.activate(str(clone))
    wt = tmp_path / "wt"
    _git(clone, "worktree", "add", "-q", str(wt), "-b", "task-1/x")

    status = git_policy.inspect(str(wt))

    assert status.enforced, status.reason
    assert str(wt) in status.reason, "the worktree must run its own copy of the hook"


# ---- the hub arms what it prepares, so nobody has to remember ----


async def test_an_existing_workspace_is_armed_too(tmp_path: Path):
    """The finding that mattered most: arming only after a fresh clone.

    Every workspace on production already exists, so ``clone_repo`` always
    takes the "existing clone verified" branch there — the activation would
    have run on a path that never happens, and the deploy would have changed
    nothing. This drives the real function against a real existing clone.
    """
    from hub.integrations.git_ops import GitOpsIntegration

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, timeout=30)

    source = tmp_path / "source"
    subprocess.run(["git", "init", "-q", str(source)], check=True, timeout=30)
    _git(source, "checkout", "-q", "-b", "develop")
    (source / ".githooks").mkdir()
    shutil.copy2(REAL_HOOK, source / ".githooks" / "pre-push")
    (source / ".githooks" / "pre-push").chmod(0o755)
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "init")
    _git(source, "remote", "add", "origin", str(origin))
    _git(source, "push", "-q", "origin", "develop")

    workspace = tmp_path / "workspace"
    subprocess.run(
        ["git", "clone", "-q", "--branch", "develop", str(origin), str(workspace)],
        check=True,
        timeout=60,
    )
    assert not git_policy.inspect(str(workspace)).enforced, "precondition"

    ok, detail = await GitOpsIntegration().clone_repo(
        str(origin), str(workspace), base_branch="develop"
    )

    assert ok, detail
    assert "existing clone" in detail, "this test must exercise the existing-clone path"
    assert git_policy.inspect(str(workspace)).canonical, (
        "a workspace the hub already had is exactly the one nobody will arm by hand"
    )


async def test_startup_arms_the_workspaces_that_already_exist(clone: Path, db):
    """The finding of the fourth round: arming had a caller, and that caller
    had no caller. clone_repo runs only from provision_project, which runs
    only when a human presses Provision — so on a server where every workspace
    already exists, the release would have changed nothing at all.

    Startup is the moment that always happens. This drives the real sweep
    against a real unarmed workspace.
    """
    from hub import poller

    created = await repo.create_project(db, slug="p", name="P")
    project_id = created if isinstance(created, int) else created["id"]
    await db.execute(
        "UPDATE projects SET workspace_path=? WHERE id=?", (str(clone), project_id)
    )
    await db.commit()
    assert not git_policy.inspect(str(clone)).enforced, "precondition"

    outcomes = await poller.arm_workspace_hooks(db)

    assert ("p", git_policy.ACTIVE) in outcomes, outcomes
    assert git_policy.inspect(str(clone)).canonical, (
        "a workspace nobody will re-provision is exactly the one left unarmed"
    )

    _git(clone, "checkout", "-q", "-b", "not-on-the-list")
    assert _git(clone, "push", "origin", "not-on-the-list").returncode != 0


def test_startup_actually_reaches_the_sweep():
    """The sweep above is proven by running it. What this adds is that
    start_poller launches it — a function with no caller is the defect this
    round was returned for, and the round before that, one level down."""
    import ast

    tree = ast.parse((REPO_ROOT / "hub" / "poller.py").read_text())
    starters = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "start_poller"
    ]
    launched = {
        arg.func.id
        for fn in starters
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        for arg in node.args
        if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
    }

    assert "arm_workspace_hooks" in launched, (
        "start_poller must launch the sweep, or existing workspaces wait for "
        "a human to press Provision"
    )


# ---- Haiplane rebrand (Wave 3): dual git-config key families --------------


def test_record_branch_policy_writes_both_key_families(tmp_path: Path):
    """The hub writes haiplane.* AND openclaw.*: a clone whose pre-push hook
    is still the old file reads the legacy family, and writing only the new
    one would silently disable branch policy there."""
    target = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(target)], check=True, timeout=30)

    written = git_policy.record_branch_policy(str(target), "develop", "main")

    for key in (brand.GIT_BASE_BRANCH_KEY, brand.GIT_BASE_BRANCH_KEY_LEGACY):
        assert _git(target, "config", "--get", key).stdout.strip() == "develop"
        assert written[key] == "develop"
    for key in (brand.GIT_RELEASE_BRANCH_KEY, brand.GIT_RELEASE_BRANCH_KEY_LEGACY):
        assert _git(target, "config", "--get", key).stdout.strip() == "main"
        assert written[key] == "main"


def test_an_empty_new_key_does_not_hide_the_legacy_value(tmp_path: Path):
    """git config --get succeeds on an empty key, so `new || old` would stop
    at the empty new value. The reader must test for emptiness and fall back."""
    target = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(target)], check=True, timeout=30)
    _git(target, "config", brand.GIT_BASE_BRANCH_KEY, "")
    _git(target, "config", brand.GIT_BASE_BRANCH_KEY_LEGACY, "master")

    assert git_policy._recorded_base(str(target)) == "master"


def test_the_hook_reads_the_new_key_family(clone: Path):
    """A clone configured only with haiplane.baseBranch protects that branch."""
    git_policy.activate(str(clone))
    _git(clone, "config", brand.GIT_BASE_BRANCH_KEY, "integration")
    _git(clone, "checkout", "-q", "-b", "integration")

    pushed = _git(clone, "push", "origin", "integration")

    assert pushed.returncode == 0, pushed.stderr


def test_the_hook_still_reads_the_legacy_key_family(clone: Path):
    """A clone recorded before the rename carries only openclaw.*; the hook
    must keep honouring it."""
    git_policy.activate(str(clone))
    _git(clone, "config", brand.GIT_BASE_BRANCH_KEY_LEGACY, "integration")
    _git(clone, "checkout", "-q", "-b", "integration")

    pushed = _git(clone, "push", "origin", "integration")

    assert pushed.returncode == 0, pushed.stderr


def test_the_hook_ignores_an_empty_new_key_and_reads_legacy(clone: Path):
    """The shell reader mirrors _recorded_base: an empty haiplane.baseBranch
    must not shadow a real openclaw.baseBranch."""
    git_policy.activate(str(clone))
    _git(clone, "config", brand.GIT_BASE_BRANCH_KEY, "")
    _git(clone, "config", brand.GIT_BASE_BRANCH_KEY_LEGACY, "integration")
    _git(clone, "checkout", "-q", "-b", "integration")

    pushed = _git(clone, "push", "origin", "integration")

    assert pushed.returncode == 0, pushed.stderr


def test_the_clone_path_arms_the_hook_itself():
    """Submission-time guard, in the shape #534 needed: an activation step
    that exists but is never called is the same dead code as a hook that is
    never run."""
    import ast

    tree = ast.parse((REPO_ROOT / "hub" / "integrations" / "git_ops.py").read_text())
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "activate_quietly" in called, (
        "cloning a workspace must arm the hook — otherwise every server clone "
        "waits for a human to remember"
    )
