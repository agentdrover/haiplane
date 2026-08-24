"""Activate and verify the pre-push hook in a clone (#532).

``.githooks/pre-push`` carries the whole branch policy, but git only runs it
when ``core.hooksPath`` points at it. That is per-clone local configuration
that nothing automated, so it was set nowhere: verified on 2026-08-01, the
operator's working clone and both server workspaces had it empty. The hook
existed and never ran.

WHAT COUNTS AS ACTIVE. Not "the config key has a value" — the key can name a
directory that holds no hook, and git then skips the push silently. That
would be a setting that reads as protection while nothing is enforced, which
is the same mistake as trusting a commit subject to prove a merge (#534). So
the checked fact is the one that decides the push: a hook file exists at the
configured path and is executable.

THREE STATES, NOT TWO, for the same reason the drift guard has three: a
repository that carries no hook at all is not "unprotected by mistake", it is
a repository this policy does not apply to — calc-kids on production is one.
Reporting that as a failure to fix would send the operator chasing a
configuration that cannot exist.

WHAT THIS DOES NOT DO. ``git push --no-verify`` skips the hook entirely, and
so does any clone where nobody ran the setup. The hook is fast feedback for
honest mistakes, never a guarantee — the guarantee, such as it is, is the
server-side drift detection in #534.
"""

from __future__ import annotations

import logging
import os
import shlex

# Synchronous by design: this runs as a CLI (doctor/activate) and standalone
# on servers. argv is always a fixed list — no shell ever sees these strings.
import subprocess  # nosec B404
from dataclasses import dataclass

from hub import brand

log = logging.getLogger("hub.git_policy")

HOOKS_DIR = ".githooks"
HOOK_NAME = "pre-push"
ACTIVATE_COMMAND = f"git config core.hooksPath {HOOKS_DIR}"

# #475: the hook enforces a branch policy, and WHICH branches it protects is a
# per-project fact. It cannot ask the hub — it runs offline, inside git — so
# the hub leaves the answer where the hook can read it: local git config in the
# clone it just armed. Two keys, because "where work lands" and "what is in
# production" are two different questions (the same split as PAIR_BASE_BRANCH
# vs RELEASE_BRANCH in hub/config.py).
#
BASE_BRANCH_KEY = brand.GIT_BASE_BRANCH_KEY
RELEASE_BRANCH_KEY = brand.GIT_RELEASE_BRANCH_KEY


def activate_command(repo: str) -> str:
    """The activation command as the reader must type it from where they are.

    The doctor takes a path, so it is routinely run from somewhere else. A bare
    ``git config`` printed there configures the caller's own clone and leaves
    the target untouched — an instruction that looks followed and did nothing.
    """
    target = os.path.abspath(repo)
    if target == os.path.abspath(os.getcwd()):
        return ACTIVATE_COMMAND
    # Quoted, because the path is printed to be pasted into a shell: an
    # unquoted directory with a space splits into two arguments, and the
    # command a reader follows literally does nothing (#532 review).
    return f"git -C {shlex.quote(target)} config core.hooksPath {HOOKS_DIR}"


ACTIVE = "active"
# The push is checked, but by a copy in .git/hooks rather than by the
# repository's own file: it works today and silently stops matching the
# policy the moment the hook changes.
COPIED = "copied"
INACTIVE = "inactive"
ABSENT = "absent"


@dataclass
class HookStatus:
    """What would actually happen on the next push from this checkout."""

    state: str
    reason: str
    fix: str = ""

    @property
    def enforced(self) -> bool:
        """Whether a push from this checkout gets checked at all."""
        return self.state in (ACTIVE, COPIED)

    @property
    def canonical(self) -> bool:
        """Whether it is checked by the repository's own hook file."""
        return self.state == ACTIVE


def _git(repo: str, *args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(  # nosec B603 B607 - fixed argv; "git" from PATH
            # deliberately: the doctor must consult the same git the user's
            # shell would run, or its verdict describes a different world.
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - env
        return 1, str(exc)
    return p.returncode, (p.stdout or "").strip()


def _toplevel(repo: str) -> str:
    rc, out = _git(repo, "rev-parse", "--show-toplevel")
    return out if rc == 0 and out else repo


def hook_file(repo: str) -> str:
    """Where the hook lives in this checkout, configured or not."""
    return os.path.join(_toplevel(repo), HOOKS_DIR, HOOK_NAME)


def _default_hook(repo: str) -> str:
    """Where git looks when core.hooksPath is unset."""
    rc, common = _git(repo, "rev-parse", "--git-common-dir")
    if rc != 0 or not common:
        return os.path.join(repo, ".git", "hooks", HOOK_NAME)
    if not os.path.isabs(common):
        common = os.path.join(_toplevel(repo), common)
    return os.path.join(common, "hooks", HOOK_NAME)


def _configured_hook(repo: str, configured: str) -> str:
    """The hook path git would run for ``core.hooksPath = configured``.

    A relative value resolves against the top level of the working tree, not
    against the shared .git directory — checked against real git, because it
    decides whether each worktree gets its own hook or the main clone's.
    """
    if os.path.isabs(configured):
        return os.path.join(configured, HOOK_NAME)
    return os.path.join(_toplevel(repo), configured, HOOK_NAME)


def inspect(repo: str) -> HookStatus:
    """Report whether a push from ``repo`` would be checked by the hook."""
    rc, _ = _git(repo, "rev-parse", "--git-dir")
    if rc != 0:
        return HookStatus(ABSENT, f"{repo} is not a git repository")

    present = hook_file(repo)
    if not os.path.isfile(present):
        # Two very different repositories look identical here. One never had
        # the hook — calc-kids, its own remote, no policy to apply. In the
        # other the hook is tracked in git and simply gone from the working
        # tree: that is protection that broke, and answering NOT APPLICABLE
        # would file it under "nothing to do here" (#532 review).
        rc, _ = _git(repo, "ls-files", "--error-unmatch", f"{HOOKS_DIR}/{HOOK_NAME}")
        if rc == 0:
            return HookStatus(
                INACTIVE,
                f"{HOOKS_DIR}/{HOOK_NAME} is tracked in git but missing from the "
                "working tree, so nothing checks the push",
                fix=(
                    f"git -C {shlex.quote(os.path.abspath(repo))} "
                    f"checkout -- {HOOKS_DIR}/{HOOK_NAME}"
                ),
            )
        return HookStatus(
            ABSENT,
            f"this repository carries no {HOOKS_DIR}/{HOOK_NAME}; "
            "the branch policy does not apply to it",
        )

    _, configured = _git(repo, "config", "--get", "core.hooksPath")
    if not configured:
        # Before calling it inactive, look where git looks by default. An
        # older instruction in docs/repository-rules.md told people to COPY
        # the hook into .git/hooks, and in such a clone the push really is
        # checked. Reporting "not active" there would send someone to fix a
        # setup that works — and a checker that cries wolf gets muted, which
        # is the failure mode this whole feature is about.
        installed = _default_hook(repo)
        if os.path.isfile(installed) and os.access(installed, os.X_OK):
            return HookStatus(
                COPIED,
                f"push is checked by a copy at {installed}; the copy does not "
                f"follow changes to {HOOKS_DIR}/{HOOK_NAME}",
                fix=activate_command(repo),
            )
        return HookStatus(
            INACTIVE,
            f"core.hooksPath is unset, so git will not run {HOOKS_DIR}/{HOOK_NAME}",
            fix=activate_command(repo),
        )

    target = _configured_hook(repo, configured)
    if not os.path.isfile(target):
        return HookStatus(
            INACTIVE,
            f"core.hooksPath is {configured!r}, but no {HOOK_NAME} exists there — "
            "git skips the push silently",
            fix=activate_command(repo),
        )
    if not os.access(target, os.X_OK):
        return HookStatus(
            INACTIVE,
            f"{target} is not executable, so git cannot run it",
            fix=f"chmod +x {shlex.quote(target)}",
        )
    return HookStatus(ACTIVE, f"push is checked by {target}")


def record_branch_policy(
    repo: str, base_branch: str = "", release_branch: str = ""
) -> dict[str, str]:
    """Tell the hook in ``repo`` which branches this project protects (#475).

    Written as local git config so the hook — which runs offline and cannot
    ask the hub — reads the project's own branches instead of the hub's. An
    empty value writes nothing rather than writing an empty key: a key that
    exists and says nothing is worse than an absent one, because the hook
    would read it as configured and fall back to nothing.

    Never raises: recording the policy must not fail a clone, and a key that
    could not be written leaves the hook on its documented fallback.
    """
    written: dict[str, str] = {}
    for key, value in (
        (BASE_BRANCH_KEY, (base_branch or "").strip()),
        (RELEASE_BRANCH_KEY, (release_branch or "").strip()),
    ):
        if not value:
            continue
        rc, out = _git(repo, "config", key, value)
        if rc != 0:
            log.warning("could not record %s in %s: %s", key, repo, out)
            continue
        written[key] = value
    return written


# Sync between what the project declares and what its clone protects (#887).
# Three states, and the third is not a shade of the first: "could not look" is
# its own answer with its own cause, the rule CIRunReportState (#546) and
# sha_check (#572) already carry. A clone the hub cannot read is not a clone in
# agreement — it is a clone nobody checked, and the two call for different acts.
BRANCH_IN_SYNC = "match"
BRANCH_DIVERGED = "diverged"
BRANCH_UNCHECKED = "unknown"


@dataclass
class BranchSyncState:
    """Whether the clone protects the branch its project declares.

    Both values are carried, always: a divergence report that names only one
    side tells the reader something is wrong and not what to change. ``reason``
    is filled in every state, including agreement, so the field never reads as
    an empty assertion.
    """

    state: str = BRANCH_UNCHECKED
    reason: str = "сверка клона с проектом не выполнялась"
    project_branch: str = ""
    clone_branch: str = ""

    @property
    def agrees(self) -> bool:
        """True only for observed agreement — never for an unread clone."""
        return self.state == BRANCH_IN_SYNC


def branch_sync(repo: str, project_branch: str) -> BranchSyncState:
    """Compare ``project.default_branch`` with what ``repo`` records (#887).

    The comparison is against the key the hook actually reads, not against the
    hub's own configuration: the hook runs offline and obeys the clone.

    A readable clone with NO key recorded is reported as diverged rather than
    unknown, and deliberately: we did look, and what we found is a hook falling
    back to its own built-in branch instead of protecting the project's. Only a
    clone that could not be read at all — no workspace configured, no such
    directory, not a git repository — is unknown, and it always says why.
    """
    declared = (project_branch or "").strip()
    path = (repo or "").strip()
    if not path:
        return BranchSyncState(
            BRANCH_UNCHECKED,
            "у проекта не задан workspace_path — клона, который можно сверить, нет",
            declared,
        )
    if not os.path.isdir(path):
        return BranchSyncState(
            BRANCH_UNCHECKED,
            f"{path} не существует — workspace не провижинен",
            declared,
        )
    rc, out = _git(path, "rev-parse", "--git-dir")
    if rc != 0:
        return BranchSyncState(
            BRANCH_UNCHECKED,
            f"{path} не читается как git-репозиторий: {out[:200] or 'git не ответил'}",
            declared,
        )
    if not declared:
        return BranchSyncState(
            BRANCH_UNCHECKED,
            "проект не объявляет ветку — сверять не с чем",
            "",
            _recorded_base(path),
        )
    recorded = _recorded_base(path)
    if recorded == declared:
        return BranchSyncState(
            BRANCH_IN_SYNC,
            f"клон защищает ветку проекта: {declared}",
            declared,
            recorded,
        )
    if not recorded:
        return BranchSyncState(
            BRANCH_DIVERGED,
            f"в клоне не записан {BASE_BRANCH_KEY}: хук защищает свою "
            f"умолчательную ветку, а не объявленную проектом ({declared})",
            declared,
            "",
        )
    return BranchSyncState(
        BRANCH_DIVERGED,
        f"проект объявляет {declared}, клон защищает {recorded}",
        declared,
        recorded,
    )


def _recorded_base(repo: str) -> str:
    """The base branch this clone records, or "" when the key is not set."""
    rc, out = _git(repo, "config", "--get", BASE_BRANCH_KEY)
    return out.strip() if rc == 0 else ""


def activate(repo: str) -> HookStatus:
    """Point git at the hook. Idempotent; returns the resulting status.

    A repository that carries no hook is left alone: setting the key there
    would record protection that cannot happen.
    """
    status = inspect(repo)
    if status.state == ABSENT or status.canonical:
        return status

    rc, out = _git(repo, "config", "core.hooksPath", HOOKS_DIR)
    if rc != 0:
        return HookStatus(
            INACTIVE, f"could not set core.hooksPath: {out}", fix=activate_command(repo)
        )
    return inspect(repo)


def activate_quietly(
    repo: str, base_branch: str = "", release_branch: str = ""
) -> HookStatus:
    """Activation for paths the hub prepares itself (clones, workspaces).

    Never raises and never blocks the caller: failing to arm a hook must not
    fail a clone. It does log, because an activation nobody can see is the
    silent state this task exists to remove.

    ``base_branch``/``release_branch`` (#475) are recorded alongside so the
    armed hook protects THIS project's branches. Recorded even when the hook
    is absent or could not be armed: the keys are inert without a hook, and a
    clone that gets one later is then already configured.
    """
    try:
        record_branch_policy(repo, base_branch, release_branch)
    except Exception as exc:  # noqa: BLE001 - best effort by contract
        log.warning("could not record the branch policy in %s: %s", repo, exc)
    try:
        status = activate(repo)
    except Exception as exc:  # noqa: BLE001 - best effort by contract
        log.warning("could not activate the pre-push hook in %s: %s", repo, exc)
        return HookStatus(INACTIVE, str(exc), fix=activate_command(repo))
    if status.canonical:
        log.info("pre-push hook armed in %s", repo)
    else:
        log.info("pre-push hook not armed in %s: %s", repo, status.reason)
    return status


def main(argv: list[str] | None = None) -> int:
    """``doctor`` reports, ``activate`` fixes. Both name the exact command."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="hp-git-policy",
        description="Check or activate the pre-push hook that enforces branch policy.",
    )
    parser.add_argument("command", choices=["doctor", "activate"])
    parser.add_argument("repo", nargs="?", default=".")
    args = parser.parse_args(argv)

    repo = os.path.abspath(args.repo)
    status = inspect(repo) if args.command == "doctor" else activate(repo)

    if status.canonical:
        print(f"pre-push hook: ACTIVE — {status.reason}")
        return 0
    if status.state == COPIED:
        # rc 0: the push IS checked. The warning is that it is checked by a
        # snapshot, which stops matching the policy without telling anyone.
        print(f"pre-push hook: ACTIVE VIA A COPY — {status.reason}")
        print(f"prefer: {status.fix}")
        return 0
    if status.state == ABSENT:
        # Not a failure to fix: there is nothing here to arm. Saying "broken"
        # would send the reader after a hook that does not exist.
        print(f"pre-push hook: NOT APPLICABLE — {status.reason}")
        return 0
    print(f"pre-push hook: NOT ACTIVE — {status.reason}")
    print(f"run: {status.fix}")
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
