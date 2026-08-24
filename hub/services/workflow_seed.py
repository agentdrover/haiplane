"""Lay the hub's workflow templates into a project's repository (#476).

THE HOLE THIS FILLS. The delivery gate merges a pull request only after
``check_pr_ci`` answers ``passed`` (#605/#606). A repository with no GitHub
Actions workflow produces no run, the probe answers ``absent``, and the gate
refuses with ``ci_absent`` — so every project the hub provisioned could be
given work, could finish it, could have it approved, and then had no
supported path to delivery at all. ``.github/workflows`` existed in the hub's
own repository and nowhere else.

WHAT THE HUB OWNS, AND ONLY THAT. Two file names — ``haiplane-ci.yml`` and
``haiplane-stale.yml``. New files are written only into a repository that
carries NO workflows of its own, and into no other.
A repository that already runs something on a pull request has already
answered the question this seeding asks, and the gate can read that answer;
overwriting it, or adding a second opinion beside it, would be the hub
editing somebody else's CI. That rule is also what keeps the hub's own
repository out of scope here: it carries ``ci.yml``, so this code never
touches it.

WHY NOTHING IS GUESSED. The CI template contains no build command. Inventing
one for a repository the hub has never read would turn ``ci_absent`` into
``ci_failed``, and a red job is strictly worse than a missing one: the gate
treats it as a blocker and burns fix cycles on it, while an honest silence is
read as unknown and routed to a human (#839's rule, applied one layer down).

WHY IT NEVER RAISES. Provisioning must always answer (#347): its outcome is a
line an operator reads, not an exception. A template that could not be laid
down leaves the project exactly as it was and says so in that line.
"""

from __future__ import annotations

import json
import logging
import os
import re

# Synchronous by design, like hub/git_policy.py next to it: the calls are a
# handful of short git invocations with a fixed argv, and provisioning already
# makes blocking git calls on this path.
import subprocess  # nosec B404
from dataclasses import dataclass, field
from importlib import resources

from hub import brand

log = logging.getLogger(__name__)

TEMPLATE_PACKAGE = "hub.workflow_templates"

#: Where GitHub looks. Not configurable — it is GitHub's constant, not ours.
WORKFLOWS_DIR = os.path.join(".github", "workflows")

WORKFLOW_SUFFIXES = (".yml", ".yaml")

#: template file -> the name the hub owns in the target repository. The names
#: carry the owner on purpose: re-provisioning may rewrite a file the hub
#: wrote, and must never rewrite one it did not.
SEEDED_WORKFLOWS = {
    "ci.yml": brand.SEEDED_CI,
    "stale.yml": brand.SEEDED_STALE,
}

#: The runner the shared reporting action documents as its default
#: (docs/satellite-ci-report.md). A project that runs its tests some other way
#: declares it as ``ci_runner`` in its gate policy; a wrong value here costs a
#: ``not_found`` per criterion with the reason printed, never a red job.
DEFAULT_AC_RUNNER = "uv run pytest"

_PLACEHOLDER = re.compile(r"@@([A-Z_]+)@@")

# --- outcomes ---------------------------------------------------------------

#: Files were written, committed and pushed.
SEEDED = "seeded"
#: The repository already carries workflows; the hub added nothing.
PRESENT = "present"
#: Nothing could be attempted — no workspace, a dirty tree, another branch.
#: Distinct from FAILED because there is nothing broken to fix here.
UNAVAILABLE = "unavailable"
#: Seeding was attempted and did not complete. The workspace is left as it was.
FAILED = "failed"


@dataclass
class SeedResult:
    """What provisioning did about this repository's workflows, and why."""

    state: str
    detail: str
    written: tuple[str, ...] = field(default_factory=tuple)

    @property
    def changed(self) -> bool:
        """Whether the project's repository gained anything."""
        return self.state == SEEDED and bool(self.written)


# --- rendering --------------------------------------------------------------


def read_template(name: str) -> str:
    """Read one packaged template verbatim. Raises FileNotFoundError."""
    candidate = resources.files(TEMPLATE_PACKAGE).joinpath(name)
    if not candidate.is_file():
        raise FileNotFoundError(name)
    return candidate.read_text(encoding="utf-8")


def push_branches(base_branch: str, release_branch: str) -> list[str]:
    """The branches a push to which is worth a CI run, in reading order.

    Deduplicated here rather than in the template, because on a project whose
    integration branch IS its release branch — spike-bo works straight on
    ``main`` — a template with two fixed lines would name the same branch
    twice. The task pattern is last and is not a branch at all: pair tasks
    land evidence without ever opening a pull request, and the CI report is
    keyed by commit, so the push of a task branch is what produces it.
    """
    ordered: list[str] = []
    for branch in (base_branch.strip(), release_branch.strip(), "task-*/**"):
        if branch and branch not in ordered:
            ordered.append(branch)
    return ordered


def render(
    name: str,
    *,
    base_branch: str,
    release_branch: str,
    ac_runner: str = "",
) -> str:
    """Substitute the ``@@NAME@@`` placeholders of one template.

    Every placeholder must resolve. An unresolved one would ship a workflow
    naming a branch called ``@@BASE_BRANCH@@``, which GitHub accepts as a
    perfectly valid ref pattern that matches nothing — the workflow would sit
    in the repository looking installed and never run once. Raising here turns
    that into a provisioning detail somebody reads, which is the difference
    between a bad answer and no answer.
    """
    base = (base_branch or "").strip()
    release = (release_branch or "").strip()
    if not base or not release:
        raise ValueError(
            f"{name}: both the integration and the release branch must be known "
            "before a workflow can be rendered for this project"
        )
    # require_github_owner() first: an empty owner must refuse the render
    # as a provisioning detail somebody reads, not fail deeper in.
    brand.require_github_owner()
    values = {
        "BASE_BRANCH": base,
        "RELEASE_BRANCH": release,
        "CI_REPORT_ACTION": brand.ci_report_action(),
        # A JSON array is also a valid YAML flow sequence, and it quotes and
        # escapes every element for us — a branch name with a slash in it is
        # the ordinary case here, not the exotic one.
        "PUSH_BRANCHES": json.dumps(push_branches(base, release)),
        "AC_RUNNER": (ac_runner or "").strip() or DEFAULT_AC_RUNNER,
    }
    unknown: list[str] = []

    def _substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            unknown.append(key)
            return match.group(0)
        return values[key]

    rendered = _PLACEHOLDER.sub(_substitute, read_template(name))
    if unknown:
        raise ValueError(
            f"{name}: unknown placeholder(s) {', '.join(sorted(set(unknown)))}"
        )
    return rendered


def render_all(
    *, base_branch: str, release_branch: str, ac_runner: str = ""
) -> dict[str, str]:
    """``{target file name: rendered content}`` for every seeded workflow."""
    return {
        target: render(
            template,
            base_branch=base_branch,
            release_branch=release_branch,
            ac_runner=ac_runner,
        )
        for template, target in SEEDED_WORKFLOWS.items()
    }


# --- inspection -------------------------------------------------------------


def existing_workflows(workspace: str) -> list[str]:
    """Workflow file names already present in the repository, sorted.

    The question is "does anything run here", not "does OUR file exist": a
    repository with its own pipeline needs nothing from the hub, and the gate
    reads its outcome just as well.
    """
    directory = os.path.join(workspace, WORKFLOWS_DIR)
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(n for n in names if n.endswith(WORKFLOW_SUFFIXES))


def _git(workspace: str, *args: str) -> tuple[int, str]:
    """Run one git command in ``workspace``; ``(rc, combined output)``.

    A local twin of the helper in hub/git_policy.py rather than an import of
    its private one: this module must not reach into another module's
    internals to make a five-line call.
    """
    try:
        proc = subprocess.run(  # nosec B603 B607 - fixed argv; git from PATH
            ["git", "-C", workspace, *args],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - env
        return 1, str(exc)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    return proc.returncode, out or err


def _current_branch(workspace: str) -> str:
    rc, out = _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    return out if rc == 0 else ""


# --- the act itself ---------------------------------------------------------


def seed_project_workflows(
    workspace: str,
    *,
    base_branch: str,
    release_branch: str,
    ac_runner: str = "",
    push: bool = True,
) -> SeedResult:
    """Write, commit and push the hub's workflows into a project's clone.

    Never raises: every failure is a state and a sentence. Idempotent by
    construction — the second call finds the files it wrote the first time and
    returns PRESENT without touching anything.

    The preconditions are refusals, not warnings. A dirty tree is refused
    because committing beside somebody else's uncommitted work would sweep it
    into the hub's commit, and because the pre-push hook blocks such a push
    anyway (#532). A checkout standing on another branch is refused because
    the workflows have to reach the branch task branches are cut from, and
    switching a workspace out from under whoever is using it is not the hub's
    call to make.
    """
    try:
        return _seed(
            workspace,
            base_branch=base_branch,
            release_branch=release_branch,
            ac_runner=ac_runner,
            push=push,
        )
    except Exception as exc:  # noqa: BLE001 - provisioning must always answer
        log.warning("could not seed workflows into %s: %s", workspace, exc)
        return SeedResult(FAILED, f"workflow seeding failed: {exc}")


def _seed(
    workspace: str,
    *,
    base_branch: str,
    release_branch: str,
    ac_runner: str,
    push: bool,
) -> SeedResult:
    if not workspace or not os.path.isdir(os.path.join(workspace, ".git")):
        return SeedResult(UNAVAILABLE, "no git workspace to seed workflows into")

    present = existing_workflows(workspace)
    if present:
        return SeedResult(
            PRESENT,
            "repository already carries workflows, none added: " + ", ".join(present),
        )

    base = (base_branch or "").strip()
    if not base:
        return SeedResult(UNAVAILABLE, "project declares no integration branch")

    files = render_all(
        base_branch=base,
        release_branch=release_branch,
        ac_runner=ac_runner,
    )

    rc, dirty = _git(workspace, "status", "--porcelain")
    if rc != 0:
        return SeedResult(UNAVAILABLE, f"could not read the working tree: {dirty}")
    if dirty:
        return SeedResult(
            UNAVAILABLE,
            "working tree has uncommitted changes; workflows not seeded",
        )

    current = _current_branch(workspace)
    if current != base:
        return SeedResult(
            UNAVAILABLE,
            f"workspace stands on {current or 'a detached HEAD'}, not {base}; "
            "workflows not seeded",
        )

    directory = os.path.join(workspace, WORKFLOWS_DIR)
    os.makedirs(directory, exist_ok=True)
    written: list[str] = []
    for name, content in sorted(files.items()):
        with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
            handle.write(content)
        written.append(name)

    paths = [os.path.join(WORKFLOWS_DIR, name) for name in written]
    rc, out = _git(workspace, "add", "--", *paths)
    if rc != 0:
        _discard(workspace, paths)
        return SeedResult(FAILED, f"could not stage the workflows: {out}")

    rc, out = _git(
        workspace,
        "-c",
        "user.name=Haiplane Hub",
        "-c",
        "user.email=hub@haiplane.local",
        "commit",
        "-m",
        "ci: add Haiplane workflows (hub provisioning, #476)",
        "--",
        *paths,
    )
    if rc != 0:
        _discard(workspace, paths)
        return SeedResult(FAILED, f"could not commit the workflows: {out}")

    if not push:
        return SeedResult(
            SEEDED,
            f"committed locally on {base}: " + ", ".join(written),
            tuple(written),
        )

    rc, out = _git(workspace, "push", "origin", base)
    if rc != 0:
        # Undo our own commit rather than leave the workspace one commit ahead
        # of its remote: an unpushed local commit on the integration branch is
        # exactly the shape the drift guard is built to shout about (#534), and
        # the next push from any task branch would carry it along unannounced.
        # Safe to reset hard — the tree was verified clean above, so the only
        # thing this drops is the commit made three lines up.
        _git(workspace, "reset", "--hard", "HEAD~1")
        return SeedResult(
            FAILED,
            f"could not push the workflows to {base} (needs write access and, "
            f"for a token, the workflow scope): {out}",
        )

    log.info("seeded workflows into %s on %s: %s", workspace, base, ", ".join(written))
    return SeedResult(SEEDED, f"added on {base}: " + ", ".join(written), tuple(written))


def _discard(workspace: str, paths: list[str]) -> None:
    """Remove files this module just wrote, leaving the clone as it was."""
    _git(workspace, "reset", "--", *paths)
    for path in paths:
        try:
            os.remove(os.path.join(workspace, path))
        except OSError:  # pragma: no cover - already gone
            pass
