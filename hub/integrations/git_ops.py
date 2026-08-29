"""Git + GitHub operations for Hub branching workflow.

Local git operations run against the workspace repo (config.WORKSPACE_REPO_LINK).
GitHub operations use the ``gh`` CLI (config.GH_BIN).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from hub.actionable_errors import (
    pair_branch_dirty_detail,
    pair_worktree_dirty_detail,
)
from hub.config import GH_BIN, PAIR_BASE_BRANCH, REPO_NAME, WORKSPACE_REPO_LINK
from hub.integrations.protocols import (
    CIProbeOutcome,
    CIProbeResult,
    MergeabilityOutcome,
)
from hub.mcp_envelope import enrich_error_payload

from hub import git_policy
from hub.commit_scope import parse_porcelain_paths
from hub.process_kill import kill_process_group

log = logging.getLogger(__name__)

# Exit code for a killed-on-timeout command: the shell convention, and distinct
# from any rc git itself returns, so a caller can tell a timeout from a refusal.
_TIMEOUT_RC = 124


class WorkspaceNotReadyError(Exception):
    """create_branch refused to prepare a branch (#361).

    Distinct from returning "": that means "no branch was created" and covers
    the perfectly ordinary case of a hub running without git integration
    (NoopGitOps). Collapsing a refusal into it would turn an unconfigured hub
    into a blocked one — the same ambiguity that made auto_commit's False
    useless to its caller.
    """


class WorkspaceBranchMismatchError(Exception):
    """auto_commit was asked to commit onto a branch that is not current (#361).

    A distinct type on purpose: auto_commit returns False both for "nothing to
    commit" and, previously, for "refused". The caller could not tell those
    apart, so the refusal was ignored and the destructive tail (squash --soft,
    push) ran anyway — the guard was inert.
    """


class PairBranchConflictError(Exception):
    """Pair-start cannot proceed without risking loss of uncommitted work."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "pair_branch_conflict",
        hint: str | None = None,
        workspace_path: str | None = None,
        hostname: str | None = None,
        suggested_tool: str | None = "hub_pair_start",
        files: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason
        self.hint = hint
        self.workspace_path = workspace_path
        self.hostname = hostname
        self.suggested_tool = suggested_tool
        self.files = files or []

    def to_detail(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "reason": self.reason,
            "message": self.message,
        }
        if self.hint:
            payload["hint"] = self.hint
        if self.files:
            payload["files"] = self.files
        if self.workspace_path:
            payload["workspace_path"] = self.workspace_path
        if self.hostname:
            payload["hostname"] = self.hostname
        if self.suggested_tool:
            payload["suggested_tool"] = self.suggested_tool
        if self.reason == "pair_branch_dirty":
            return pair_branch_dirty_detail(payload)
        if self.reason == "pair_worktree_dirty":
            return pair_worktree_dirty_detail(payload)
        return enrich_error_payload(payload)


# ---------------------------------------------------------------------------
# Helpers (module-private)
# ---------------------------------------------------------------------------


def _repo_root() -> str:
    p = WORKSPACE_REPO_LINK
    if p.is_symlink():
        p = p.resolve()
    return str(p)


def _hostname() -> str:
    return socket.gethostname()


def _task_id_from_branch(branch: str) -> int | None:
    if not branch.startswith("task-"):
        return None
    head = branch.split("/", 1)[0]
    try:
        return int(head.removeprefix("task-"))
    except ValueError:
        return None


# How many paths a refusal names before it starts counting instead. The tail
# is counted rather than dropped: a bare "10 files" list reads as the whole of
# it, and the person then stashes believing they have seen everything.
_DIRTY_PATHS_NAMED = 10


async def _dirty_state(repo: str) -> tuple[str, list[str]]:
    """``(raw porcelain, paths)`` for ``repo`` — dirtiness and what is dirty.

    Both halves come from one ``git status`` because they must agree: the raw
    text decides whether to refuse, the parsed paths say what to name. The
    ``-z`` form is what makes the second half usable — without it git escapes
    any non-ASCII path (``docs/Тест.md`` arrives as ``"docs/\\320\\242..."``),
    and a refusal that names a file under a name the person cannot find in
    their own checkout has not really named it (#555, same defect one layer up).
    """
    _, out, _ = await _git("status", "--porcelain", "-z", repo=repo, check=False)
    return out, parse_porcelain_paths(out)


def _name_dirty_files(paths: list[str]) -> str:
    if not paths:
        return "git не назвал ни одного пути"
    named = ", ".join(paths[:_DIRTY_PATHS_NAMED])
    rest = len(paths) - _DIRTY_PATHS_NAMED
    return f"{named} и ещё {rest}" if rest > 0 else named


def _rescue_command(repo: str, task_id: int | None = None) -> str:
    """A command that PRESERVES the dirty work, ready to paste.

    ``stash push -u`` and not ``clean``: the whole point of the refusal is that
    untracked files do not come back, so the way out it offers must keep them.
    """
    label = f" -m 'before pair-start #{task_id}'" if task_id is not None else ""
    return f"cd {repo} && git stash push -u{label}"


def _pair_branch_conflict(
    message: str,
    *,
    repo: str,
    reason: str = "pair_branch_conflict",
    hint: str | None = None,
    suggested_tool: str | None = "hub_pair_start",
    current_branch: str | None = None,
    task_id: int | None = None,
    base_branch: str | None = None,
    files: list[str] | None = None,
) -> PairBranchConflictError:
    host = _hostname()
    if hint is None and current_branch and task_id is not None:
        # #475: the operator is told to check out the branch this project
        # actually integrates on. A literal here sent a calc-kids operator to
        # `git checkout develop` in a clone that has no develop — an
        # instruction that fails is worse than none, because it reads as the
        # fix and leaves the workspace exactly as it was.
        base = _resolve_base(base_branch)
        hint = (
            f"SSH to {host}, then: cd {repo} && git checkout {base} "
            f"(or push {current_branch!r} if unpushed), then "
            f"hub_pair_start for #{task_id}."
        )
    return PairBranchConflictError(
        message,
        reason=reason,
        hint=hint,
        workspace_path=repo,
        hostname=host,
        suggested_tool=suggested_tool,
        files=files,
    )


async def _count_commits(spec: str, repo: str) -> int:
    """``git rev-list --count <spec>``, or 0 when git cannot answer."""
    rc, count, _ = await _git("rev-list", "--count", spec, repo=repo, check=False)
    if rc != 0:
        return 0
    try:
        return int(count.strip() or "0")
    except ValueError:
        return 0


@dataclass(frozen=True)
class BranchPushStatus:
    """Where a branch's commits live — on the server, or only in this clone.

    Two separate facts, kept separate (#954). The old check asked only whether
    ``refs/remotes/origin/<branch>`` existed locally and reported its absence
    as unpushed work, so a branch that was on the remote and simply never
    fetched was indistinguishable from one that existed nowhere else. On 25.08
    that read blocked the start of #953 with a warning about commits of #951
    that did not exist; since #966 it instead makes the hub push a stranger's
    branch on the same untrue premise.
    """

    on_remote: bool
    # Against origin/<branch> when the remote has it; against the base branch
    # when it does not, because then every commit on it is at stake.
    commits_at_stake: int

    @property
    def needs_push(self) -> bool:
        return not self.on_remote or self.commits_at_stake > 0


async def _branch_push_status(branch: str, repo: str, base: str) -> BranchPushStatus:
    """Ask the remote, not the local cache of it, where a branch stands.

    A clone that never fetched a branch knows nothing about it, and "I have not
    looked" must not be reported as "the work is not there" — the line #767 and
    #498 drew for PR lookup, applied to refs. So a missing tracking ref is
    refreshed with one targeted fetch before any verdict is formed. The refspec
    is explicit: ``git fetch origin <branch>`` alone is not guaranteed to write
    ``refs/remotes/origin/<branch>``, and this function's whole job is that ref.
    """
    ref = f"origin/{branch}"

    async def _have_ref() -> bool:
        rc, _, _ = await _git(
            "rev-parse", "--verify", f"{ref}^{{commit}}", repo=repo, check=False
        )
        return rc == 0

    if not await _have_ref():
        await _git(
            "fetch",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            repo=repo,
            check=False,
        )
        if not await _have_ref():
            # Genuinely unknown to the server: everything since the base is at
            # stake, and that is a different sentence than "not fetched here".
            return BranchPushStatus(
                on_remote=False,
                commits_at_stake=await _count_commits(f"{base}..HEAD", repo),
            )

    return BranchPushStatus(
        on_remote=True,
        commits_at_stake=await _count_commits(f"{ref}..HEAD", repo),
    )


async def _base_ahead_of_origin(base: str, repo: str) -> bool:
    """True when local ``base`` has commits not present on ``origin/base`` (#457).

    A pair branch cut from such a base would inherit those commits — the
    contamination of incident #392, where a broken fetch left the local base
    ahead of a stale origin ref. Returns False when ``origin/base`` is unknown
    so a missing remote ref never blocks branch creation on its own.
    """
    rc, _, _ = await _git(
        "rev-parse", "--verify", f"origin/{base}^{{commit}}", repo=repo, check=False
    )
    if rc != 0:
        return False
    rc, count, _ = await _git(
        "rev-list", "--count", f"origin/{base}..{base}", repo=repo, check=False
    )
    if rc != 0:
        return False
    try:
        return int(count.strip() or "0") > 0
    except ValueError:
        return False


def _worktree_path(task_id: int, repo: str) -> str:
    """Deterministic per-task worktree path, a sibling of the main clone (#459).

    Placed next to (not inside) the main working tree so the main clone never
    sees it as untracked content. Derivable from task_id alone, so no DB column
    is needed to locate a task's worktree.
    """
    import os

    repo = os.path.abspath(repo.rstrip("/"))
    parent = os.path.dirname(repo)
    name = os.path.basename(repo)
    return os.path.join(parent, f".{name}-worktrees", f"task-{task_id}")


async def _worktree_registered(path: str, repo: str) -> bool:
    """True when ``path`` is a registered worktree of ``repo`` (#459)."""
    import os

    rc, out, _ = await _git("worktree", "list", "--porcelain", repo=repo, check=False)
    if rc != 0:
        return False
    target = os.path.abspath(path)
    for line in out.splitlines():
        if line.startswith("worktree "):
            wt = os.path.abspath(line[len("worktree ") :].strip())
            if wt == target:
                return True
    return False


async def _default_workspace_error() -> str | None:
    """Readable reason when the default-project workspace is unusable (#378).

    Returns None when WORKSPACE_REPO_LINK is a git repository. Callers that
    operate with repo=None consult this instead of failing later with a
    bare 'not a git repository' from the git binary. Probed through _git so
    the check sees exactly what subsequent commands will see.
    """
    root = _repo_root()
    try:
        rc, _, _ = await _git("rev-parse", "--git-dir", repo=root, check=False)
    except (FileNotFoundError, NotADirectoryError, OSError):
        rc = 1
    if rc == 0:
        return None
    return (
        f"default workspace is not a git repository ({root}); "
        "set HAIPLANE_WORKSPACE_REPO "
        "to a git clone or use a project with workspace_path"
    )


# Transliteration for slugs (#607). A fully Cyrillic title used to slug to
# the empty string: pair-start then built "task-601/" (fatal: invalid branch
# name) and the delivery gate's very first squash subject came out as
# "feat(task):  (#569)". One table here fixes every consumer at once — six
# call sites funnel through _slugify, and both incidents were the same hole.
# Deterministic by construction: the canonical-branch gate (#533) compares
# names strictly, so one title must always yield one slug.
_CYRILLIC_TO_LATIN = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "i",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def _slugify(title: str, max_len: int = 40) -> str:
    lowered = title.lower()
    transliterated = "".join(_CYRILLIC_TO_LATIN.get(ch, ch) for ch in lowered)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", transliterated).strip("-").lower()
    return slug[:max_len].rstrip("-")


def canonical_task_branch(task_id: int, branch_slug: str, title: str = "") -> str:
    """The one place a task's branch name is assembled (#884).

    Three call sites used to build ``f"task-{id}/{slug}"`` inline, and none
    asked whether the slug already carried that prefix. A caller passing
    "task-818/daily-digest" — which is exactly how the branch is written in a
    work plan — got ``task-818/task-818/daily-digest``. On production that
    cost four mechanisms in a row: the gate could not find the PR for such a
    name, pr_number stayed empty, the merge went outside the pipeline, and
    the dependency graph then reported delivered code as undelivered.

    Same lesson as #607, which put transliteration behind one function: a
    rule copied into three call sites drifts, starting with the copy nobody
    touches.

    The prefix is stripped only when it matches THIS task exactly, slash
    included — a slug like "task-runner-fix" is a name, not a prefix. The
    strip repeats, so a branch already doubled by the old code normalises on
    its next pair-start instead of growing again.
    """
    slug = (branch_slug or "").strip()
    prefix = f"task-{task_id}/"
    # Strip BEFORE trimming slashes: "task-601/" only reads as a prefix while
    # its trailing slash is still there. Trimming first left "task-601" and
    # produced "task-601/task-601" — caught by the empty-slug test below.
    while slug.startswith(prefix):
        slug = slug[len(prefix) :]
    slug = slug.strip("/")
    if not slug:
        # An empty slug once produced "task-601/" — an invalid ref (#607).
        slug = _slugify(title)
    return f"task-{task_id}/{slug}"


def _conv_commit_type(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ("fix", "bug", "исправ", "баг")):
        return "fix"
    if any(k in t for k in ("refactor", "рефактор", "выдел", "перенес")):
        return "refactor"
    if any(k in t for k in ("test", "тест")):
        return "test"
    if any(k in t for k in ("doc", "readme")):
        return "docs"
    if any(k in t for k in ("ci", "cd", "pipeline", "deploy")):
        return "ci"
    return "feat"


def _git_env() -> dict[str, str]:
    """Build env dict with SSH key for GitHub push."""
    import os
    from pathlib import Path

    env = os.environ.copy()
    ssh_key = Path.home() / ".ssh" / "id_ed25519"
    if ssh_key.exists():
        env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key} -o StrictHostKeyChecking=accept-new"
    # #377: anonymous https against a private repo must fail fast, not hang
    # waiting for credentials on a headless server.
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


async def _run(
    *cmd: str,
    cwd: str | None = None,
    timeout: int = 60,
    check: bool = True,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=_git_env(),
        # Required by kill_process_group: without its own session the child
        # shares the hub's process group, and the group kill below would refuse
        # to fire (killing our own group would take the hub down with it).
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        # #363 I5. This used to propagate. The poller wraps its entire tick in
        # one try/except, so a single hung git call skipped every remaining
        # stage of that tick — review, ci_check, stale sweeps, claim expiry —
        # and did so again every tick until the cause went away. Worse, the
        # child survived: asyncio cancels the read, not the process.
        await kill_process_group(proc)
        detail = f"timed out after {timeout}s: {' '.join(cmd[:4])}"
        log.error("_run: %s", detail)
        return _TIMEOUT_RC, "", detail
    rc = proc.returncode or 0
    out = stdout.decode(errors="replace").strip()
    err = stderr.decode(errors="replace").strip()
    if check and rc != 0:
        log.warning("%s failed (rc=%d): %s", " ".join(cmd[:4]), rc, err)
    return rc, out, err


async def _git(*args: str, repo: str | None = None, **kw) -> tuple[int, str, str]:
    repo = repo or _repo_root()
    return await _run("git", "-C", repo, *args, cwd=repo, **kw)


async def _gh(*args: str, repo: str | None = None, **kw) -> tuple[int, str, str]:
    repo = repo or _repo_root()
    return await _run(GH_BIN, *args, cwd=repo, **kw)


async def _reject_broken_files(repo: str) -> list[str]:
    """Revert .py files that look single-line-serialized (literal \\n instead of newlines)."""
    import pathlib

    reverted: list[str] = []
    repo_path = pathlib.Path(repo)

    rc, diff_out, _ = await _git(
        "diff",
        "--name-only",
        "--diff-filter=ACMR",
        repo=repo,
        check=False,
    )
    rc2, untracked, _ = await _git(
        "ls-files",
        "--others",
        "--exclude-standard",
        repo=repo,
        check=False,
    )
    candidates = (diff_out + "\n" + untracked).strip().splitlines()

    for rel in candidates:
        rel = rel.strip()
        if not rel.endswith(".py"):
            continue
        fpath = repo_path / rel
        if not fpath.is_file():
            continue
        try:
            content = fpath.read_text(errors="replace")
        except OSError:
            continue
        line_count = content.count("\n")
        if len(content) > 500 and line_count <= 1:
            log.warning(
                "auto_commit: BROKEN file %s (%d chars, %d lines) — reverting",
                rel,
                len(content),
                line_count,
            )
            await _git("checkout", "--", rel, repo=repo, check=False)
            if fpath.exists():
                rc3, st, _ = await _git(
                    "ls-files",
                    "--error-unmatch",
                    rel,
                    repo=repo,
                    check=False,
                )
                if rc3 != 0:
                    fpath.unlink(missing_ok=True)
            reverted.append(rel)
    return reverted


async def _resolve_ref(name: str, repo: str) -> str | None:
    """Resolve a branch name to a commit sha, falling back to origin/<name>.

    Local-first is for "does this clone have the ref?". Do not use it for a
    comparison the hub will judge — a leftover checkout can trail origin
    (#762, #1046). Those callers use ``_resolve_ref_remote_first``.
    """
    for ref in (name, f"origin/{name}"):
        rc, out, _ = await _git(
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
            repo=repo,
            check=False,
        )
        if rc == 0 and out:
            return out.strip()
    return None


async def _resolve_ref_remote_first(name: str, repo: str) -> str | None:
    """Resolve a branch to a sha, preferring ``origin/<name>`` (#762).

    The mirror image of ``_resolve_ref``, and the difference is the point. For
    a comparison the hub is going to judge, the pushed branch is the subject:
    a local ref in the workspace is whatever some earlier checkout left there,
    and on pair tasks — where the branch is written on a developer's machine —
    it can trail origin by every commit under review. That is how a submission
    of real work came back as "дифф пуст" (#759, #756): git answered honestly
    about a ref that was not the branch anyone meant.

    Falls back to the local ref when ``origin/<name>`` does not exist at all,
    which covers a workspace with no remote — there is nothing fresher to
    prefer, and refusing would turn a local-only repository into an error.
    """
    for ref in (f"origin/{name}", name):
        rc, out, _ = await _git(
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
            repo=repo,
            check=False,
        )
        if rc == 0 and out:
            return out.strip()
    return None


def _parse_pr_number(gh_output: str) -> int | None:
    m = re.search(r"/pull/(\d+)", gh_output)
    return int(m.group(1)) if m else None


def _resolve_base(base_branch: str | None) -> str:
    """The base a task's work is cut from AND its PR targets (#362 I4).

    One function on purpose. create_branch used to default to "main" while
    create_pr defaulted to PAIR_BASE_BRANCH, so on the default project (whose
    git context is empty) every headless branch was cut from main and its PR
    aimed at develop. The damage is not a cosmetic diff: with a stale
    merge-base, a change develop made to a line the task never touched shows
    up as a conflict against the task branch. Two defaults that must agree
    are a defect waiting to happen; there is now only one.
    """
    return base_branch or PAIR_BASE_BRANCH


async def _find_pr_for_branch(
    branch: str, repo: str | None = None, *, gh_repo: str | None = None
) -> int | None:
    rc, out, _ = await _gh(
        "pr",
        "list",
        "--repo",
        gh_repo or REPO_NAME,
        "--head",
        branch,
        "--state",
        "open",
        "--json",
        "number",
        repo=repo,
        check=False,
    )
    if rc == 0 and out:
        try:
            prs = json.loads(out)
            if prs:
                return prs[0]["number"]
        except (json.JSONDecodeError, KeyError, IndexError):
            pass
    return None


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class GitOpsIntegration:
    """Concrete git_ops plugin backed by local git + gh CLI."""

    async def current_branch(self, repo: str | None = None) -> str:
        rc, out, _ = await _git("branch", "--show-current", repo=repo, check=False)
        return out if rc == 0 else "unknown"

    async def branch_contains_unmerged_commits_of(
        self,
        branch: str,
        other_branch: str,
        base_branch: str | None = None,
        repo: str | None = None,
    ) -> bool:
        """True when ``branch`` carries commits unique to ``other_branch`` (#438).

        Merge-base analysis against ``base_branch``: ``other_branch`` owns the
        commits reachable from it but not from base; if ``branch`` contains
        any of them, the branches are stacked and ``branch`` cannot be
        verified against base independently. Refs are resolved remote-first
        (#1046 / #762): a stale local develop must not invent a stack.
        Best-effort: unresolvable refs or any git failure return False
        (advisory check, never an error).
        """
        if repo is None:
            reason = await _default_workspace_error()
            if reason:
                return False
        repo = repo or _repo_root()
        # #1046: judge the pushed refs. Local-first _resolve_ref made a stale
        # local develop turn independent branches into a false stack.
        head = await _resolve_ref_remote_first(branch, repo)
        other = await _resolve_ref_remote_first(other_branch, repo)
        base = await _resolve_ref_remote_first(_resolve_base(base_branch), repo)
        if not (head and other and base):
            return False

        rc, total, _ = await _git(
            "rev-list", "--count", other, f"^{base}", repo=repo, check=False
        )
        rc2, excluded, _ = await _git(
            "rev-list", "--count", other, f"^{base}", f"^{head}", repo=repo, check=False
        )
        if rc != 0 or rc2 != 0:
            return False
        try:
            total_n = int(total.strip() or "0")
            excluded_n = int(excluded.strip() or "0")
        except ValueError:
            return False
        # other_branch has unmerged commits, and at least one is inside branch.
        return total_n > 0 and excluded_n < total_n

    async def create_branch(
        self,
        task_id: int,
        title: str,
        repo: str | None = None,
        base_branch: str | None = None,
    ) -> str:
        if repo is None:
            reason = await _default_workspace_error()
            if reason:
                log.error("create_branch: %s", reason)
                return ""
        repo = repo or _repo_root()
        # Project git context (#337): headless branches historically cut
        # from main; a project may define its own integration branch.
        base = _resolve_base(base_branch)
        branch = canonical_task_branch(task_id, "", title)

        # Refuse before touching anything (#361 I2). The old shape checked out
        # base without looking at the result, then ran `checkout .` + `clean -fd`
        # on a dirty tree — destroying a person's uncommitted work in the shared
        # workspace. Worse, the checkout's rc was immediately overwritten by the
        # next call, so its failure could not be noticed even in principle: the
        # clean then ran on whatever branch happened to still be checked out.
        #
        # docs/workspace-safety-policy.md invariant 4 prescribes the opposite:
        # when the automation cannot reliably interpret git state, escalate
        # rather than proceed. P1 of that policy documents the destructive
        # behaviour as a known wart with "commit or stash first" as the
        # workaround; this removes the need for the workaround.
        dirty, dirty_files = await _dirty_state(repo)
        if dirty.strip():
            raise WorkspaceNotReadyError(
                f"refusing to prepare #{task_id} in dirty workspace {repo} — "
                f"commit or stash first. Files: {_name_dirty_files(dirty_files)}. "
                f"To keep them: {_rescue_command(repo, task_id)}"
            )

        rc, _, err = await _git("checkout", base, repo=repo, check=False)
        if rc != 0:
            raise WorkspaceNotReadyError(
                f"failed to checkout base {base!r} in {repo}: "
                + ((err or "").strip() or "git checkout failed")
            )

        # Confirm the checkout actually landed. rc alone is not enough — this is
        # the guard whose pair-mode twin had no honest test (see the test file).
        rc, current, _ = await _git("branch", "--show-current", repo=repo, check=False)
        current = (current or "").strip()
        if rc != 0 or current != base:
            raise WorkspaceNotReadyError(
                f"expected base {base!r} in {repo}, currently on {current!r}"
            )

        await _git("pull", "origin", base, "--ff-only", repo=repo, check=False)

        rc, _, err = await _git("checkout", "-b", branch, repo=repo, check=False)
        if rc != 0:
            if "already exists" in err:
                await _git("checkout", branch, repo=repo)
                log.info("Branch %s already exists, checked out", branch)
            else:
                log.error("Failed to create branch %s: %s", branch, err)
                return ""
        else:
            log.info("Created and checked out branch %s", branch)
        return branch

    async def pair_prepare_branch(
        self,
        task_id: int,
        title: str,
        *,
        branch_slug: str = "",
        repo: str | None = None,
        base_branch: str | None = None,
        notify: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Safe branch setup for pair mode: never git-clean a dirty worktree.

        ``notify`` получает человекочитаемое сообщение, когда подготовка сделала
        что-то, что должно быть видно в ленте (#966: авто-push осиротевшей
        ветки). Колбэк, а не запись напрямую: у git-слоя нет соединения с БД.
        """
        if repo is None:
            reason = await _default_workspace_error()
            if reason:
                raise PairBranchConflictError(reason)  # readable 422 (#378)
        repo = repo or _repo_root()
        base = _resolve_base(base_branch)

        dirty, dirty_files = await _dirty_state(repo)
        if dirty.strip():
            raise _pair_branch_conflict(
                "Uncommitted changes in workspace; commit or stash before "
                f"pair-start. Files: {_name_dirty_files(dirty_files)}",
                repo=repo,
                reason="pair_branch_dirty",
                files=dirty_files,
                hint=(
                    f"On host {_hostname()}: {_rescue_command(repo, task_id)} "
                    f"(or commit them), then retry hub_pair_start."
                ),
            )

        rc, current, _ = await _git("branch", "--show-current", repo=repo, check=False)
        current = (current or "").strip()
        prefix = f"task-{task_id}/"
        if current.startswith(prefix):
            log.info("pair_prepare_branch: reusing current branch %s", current)
            return current

        other_id = _task_id_from_branch(current)
        if other_id is not None and other_id != task_id:
            status = await _branch_push_status(current, repo, base)
            # A branch already on origin with nothing of its own is not this
            # start's business (#954): no work is at risk, so it is neither
            # pushed nor allowed to hold up an unrelated task.
            if status.needs_push:
                # #966: отказ «зайди на сервер и запушь» невыполним для
                # вызывающего — доступа к общему клону у агентов нет, и #961
                # часами ждал человека с root. Push обычной task-ветки в origin
                # безопасен и обратим (потеря непушенных коммитов — нет), так
                # что хаб выполняет эту часть remediation сам и отказывает
                # только когда она не удалась. Только ветки task-<id>/* —
                # произвольные ветки публиковать не наше решение.
                whereabouts = (
                    f"has {status.commits_at_stake} commit(s) not on origin/{current}"
                    if status.on_remote
                    else (
                        f"does not exist on the remote; its "
                        f"{status.commits_at_stake} commit(s) since {base} live "
                        f"only in this clone"
                    )
                )
                rc, _, push_err = await _git(
                    "push", "-u", "origin", current, repo=repo, check=False
                )
                if rc != 0:
                    raise _pair_branch_conflict(
                        f"Branch {current!r} {whereabouts}; "
                        f"auto-push failed, push before pair-start for #{task_id}",
                        repo=repo,
                        reason="pair_branch_unpushed",
                        hint=(
                            f"Auto-push of {current!r} failed: "
                            f"{(push_err or '').strip() or 'git push failed'}. "
                            f"On {_hostname()}: cd {repo} && "
                            f"git push -u origin {current}, then retry "
                            f"hub_pair_start for #{task_id}."
                        ),
                        current_branch=current,
                        task_id=task_id,
                    )
                log.info(
                    "pair_prepare_branch: auto-pushed orphaned %s (%s) to unblock #%s",
                    current,
                    whereabouts,
                    task_id,
                )
                if notify is not None:
                    try:
                        await notify(
                            f"pair-start #{task_id}: опубликована осиротевшая "
                            f"ветка {current} ({whereabouts})"
                        )
                    except Exception:
                        log.warning(
                            "pair_prepare_branch: auto-push notification failed",
                            exc_info=True,
                        )
            log.info(
                "pair_prepare_branch: auto-switching from pushed %s to %s for task #%s",
                current,
                base,
                task_id,
            )
            rc, _, err = await _git("checkout", base, repo=repo, check=False)
            if rc != 0:
                raise _pair_branch_conflict(
                    f"Failed to checkout base branch {base!r} after leaving "
                    f"{current!r}: {(err or '').strip() or 'git checkout failed'}",
                    repo=repo,
                    reason="pair_branch_checkout_failed",
                    current_branch=current,
                    task_id=task_id,
                    base_branch=base,
                )

        branch = canonical_task_branch(task_id, branch_slug, title)

        rc, _, _ = await _git("rev-parse", "--verify", branch, repo=repo, check=False)
        if rc == 0:
            await _git("checkout", branch, repo=repo)
            log.info("pair_prepare_branch: checked out existing %s", branch)
            return branch

        rc, _, err = await _git("checkout", base, repo=repo, check=False)
        if rc != 0:
            raise _pair_branch_conflict(
                f"Failed to checkout base branch {base!r}: "
                f"{(err or '').strip() or 'git checkout failed'}",
                repo=repo,
                reason="pair_branch_checkout_failed",
                task_id=task_id,
            )
        rc, current, _ = await _git("branch", "--show-current", repo=repo, check=False)
        current = (current or "").strip()
        if rc != 0 or current != base:
            raise _pair_branch_conflict(
                f"Expected base branch {base!r}, currently on {current!r}",
                repo=repo,
                reason="pair_branch_wrong_branch",
                task_id=task_id,
            )

        await _git("pull", "origin", base, "--ff-only", repo=repo, check=False)

        # Guard against branching on top of a local base that diverged from
        # origin (broken fetch → stale origin ref → foreign commits, #457/#392).
        if await _base_ahead_of_origin(base, repo):
            raise _pair_branch_conflict(
                f"Local {base!r} is ahead of origin/{base!r} in {repo}; "
                f"refusing to create {branch!r} on top of unpushed base commits",
                repo=repo,
                reason="pair_base_ahead_of_origin",
                hint=(
                    f"The workspace {base!r} carries commits not on origin/{base!r} "
                    f"(likely fixes committed to the local base, or a stale origin "
                    f"ref from a broken fetch). On {_hostname()}: cd {repo} && "
                    f"git log origin/{base}..{base} to inspect, then reconcile "
                    f"(e.g. git reset --hard origin/{base} if they belong elsewhere) "
                    f"before pair-start for #{task_id}."
                ),
                task_id=task_id,
            )

        rc, _, err = await _git("checkout", "-b", branch, repo=repo, check=False)
        if rc != 0:
            if "already exists" in err:
                await _git("checkout", branch, repo=repo)
                return branch
            log.error("pair_prepare_branch: failed to create %s: %s", branch, err)
            raise _pair_branch_conflict(
                f"Failed to create branch {branch}: {err}",
                repo=repo,
                reason="pair_branch_create_failed",
                task_id=task_id,
            )
        log.info("pair_prepare_branch: created %s from %s", branch, base)
        return branch

    async def pair_restore_workspace_base(
        self,
        task_id: int,
        *,
        repo: str | None = None,
        base_branch: str | None = None,
    ) -> bool:
        """Best-effort: checkout base when workspace is on this task's branch (#451)."""
        if repo is None:
            reason = await _default_workspace_error()
            if reason:
                log.warning("pair_restore_workspace_base: %s", reason)
                return False
        repo = repo or _repo_root()
        base = _resolve_base(base_branch)

        rc, dirty, _ = await _git("status", "--porcelain", repo=repo, check=False)
        if dirty.strip():
            log.warning(
                "pair_restore_workspace_base: dirty worktree at %s, skipping restore",
                repo,
            )
            return False

        rc, current, _ = await _git("branch", "--show-current", repo=repo, check=False)
        current = (current or "").strip()
        prefix = f"task-{task_id}/"
        if not current.startswith(prefix):
            return False

        rc, _, err = await _git("checkout", base, repo=repo, check=False)
        if rc != 0:
            log.warning(
                "pair_restore_workspace_base: checkout %s failed at %s: %s",
                base,
                repo,
                err,
            )
            return False
        log.info(
            "pair_restore_workspace_base: restored %s to %s (was %s)",
            repo,
            base,
            current,
        )
        return True

    async def pair_switch_to_task_branch(
        self,
        task_id: int,
        branch: str,
        *,
        repo: str | None = None,
        base_branch: str | None = None,
    ) -> bool:
        """Best-effort: checkout the task branch when the workspace sits on base (#457).

        Symmetric to pair_restore_workspace_base: used after a CHANGES_REQUESTED
        verdict so rework commits land on the task branch, not the local base.
        Only switches a clean tree that is currently on the base branch — never
        yanks another task's branch or clobbers uncommitted changes.
        """
        branch = (branch or "").strip()
        if not branch:
            return False
        if repo is None:
            reason = await _default_workspace_error()
            if reason:
                log.warning("pair_switch_to_task_branch: %s", reason)
                return False
        repo = repo or _repo_root()
        base = _resolve_base(base_branch)

        rc, current, _ = await _git("branch", "--show-current", repo=repo, check=False)
        current = (current or "").strip()
        if current == branch:
            return True
        if current != base:
            # Only leave the base branch; never yank a different task's branch.
            return False

        rc, dirty, _ = await _git("status", "--porcelain", repo=repo, check=False)
        if dirty.strip():
            log.warning(
                "pair_switch_to_task_branch: dirty worktree at %s, skipping switch",
                repo,
            )
            return False

        rc, _, err = await _git("checkout", branch, repo=repo, check=False)
        if rc != 0:
            log.warning(
                "pair_switch_to_task_branch: checkout %s failed at %s: %s",
                branch,
                repo,
                err,
            )
            return False
        log.info(
            "pair_switch_to_task_branch: switched %s to %s (was %s)",
            repo,
            branch,
            current,
        )
        return True

    async def resolve_ref(self, name: str, repo: str) -> tuple[str, str]:
        """Does this ref exist here? ``(state, detail)`` (#725).

        Three states, never two: ``resolved`` with the sha, ``missing`` when
        the repository was readable and the ref is genuinely not in it, and
        ``unavailable`` when there was nothing to look in. Collapsing the last
        two would let "we could not check" print as "that branch does not
        exist", which is how a brief ends up asserting something it never
        verified — the failure this was written for.
        """
        rc, _, err = await _git("rev-parse", "--git-dir", repo=repo, check=False)
        if rc != 0:
            return ("unavailable", f"{repo} is not a readable git repository")
        sha = await _resolve_ref(name, repo)
        if sha:
            return ("resolved", sha)
        return ("missing", f"neither {name} nor origin/{name} exists in {repo}")

    async def ensure_remote_branch(
        self,
        branch: str,
        source: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> tuple[str, str]:
        """Make sure ``branch`` still exists in the remote, from ``source`` (#947).

        ``(present|restored|unavailable, detail)``. Called after a release
        merge, because that is the act that removed it: merging the
        integration branch into the release branch can end with the
        integration branch deleted — GitHub offers it, automation takes the
        offer — and the project is then left with no branch for anything to be
        delivered into. Nobody notices, because a release that just succeeded
        looks like the opposite of an outage.

        Restores from the release branch rather than from any local ref: after
        the merge those two carry the same content by construction, and a
        local ref is whatever some earlier checkout left behind. Never forces:
        if the branch is there, it is left exactly as it is.
        """
        if not repo:
            return ("unavailable", "у проекта нет рабочего клона — пушить неоткуда")
        rc, out, err = await _git(
            "ls-remote",
            "--heads",
            "origin",
            f"refs/heads/{branch}",
            repo=repo,
            check=False,
        )
        if rc != 0:
            return ("unavailable", f"ls-remote не ответил: {err[:150] or 'git молчит'}")
        if out.strip():
            return ("present", out.split()[0][:12])

        rc, _, err = await _git("fetch", "origin", source, repo=repo, check=False)
        if rc != 0:
            log.warning(
                "release branch restore: fetch %s failed: %s", source, err[:200]
            )
        sha = await _resolve_ref_remote_first(source, repo)
        if not sha:
            return (
                "unavailable",
                f"не нашёл, от чего восстанавливать: ни origin/{source}, ни {source}",
            )
        # #949: the restore used to push ``<sha>:refs/heads/<branch>``. The
        # pre-push hook reads the LOCAL ref of every push line, and a bare sha
        # matches none of the allowed branch names — so in any armed clone
        # (which is every workspace clone, the hub arms them itself, #532) the
        # restore was refused. Production record #4612 shows exactly that:
        # "Blocked push from branch 'c12ed99…'". The #947 test missed it by
        # running on bare repositories with no hook.
        #
        # The fix is to push a real local branch under the restored name, so
        # the hook sees ``refs/heads/<branch>`` — the same control any push
        # passes, no bypass. Which is possible depends on where the clone
        # stands:
        #   * branch not checked out → ``git branch -f`` and push it;
        #   * branch checked out, tree clean → ``reset --hard`` — this also
        #     realigns the clone with the branch it is about to publish;
        #   * branch checked out, tree dirty → refuse honestly. A reset here
        #     would destroy someone's uncommitted work for a branch fix.
        rc, current, _ = await _git(
            "rev-parse", "--abbrev-ref", "HEAD", repo=repo, check=False
        )
        current = current.strip() if rc == 0 else ""
        if current == branch:
            # Untracked files survive a reset --hard, so they must not block
            # the restore: every workspace clone carries stray untracked files
            # (agent worktrees, wait files), and counting them as dirt would
            # make the refusal the common case. Only tracked modifications are
            # what a reset would actually destroy.
            rc, dirty, _ = await _git(
                "status", "--porcelain", "--untracked-files=no", repo=repo, check=False
            )
            if rc != 0:
                return ("unavailable", "не смог прочитать состояние рабочего дерева")
            if dirty.strip():
                return (
                    "unavailable",
                    f"ветка {branch} выгружена в рабочее дерево клона, и дерево "
                    "грязное — reset уничтожил бы незакоммиченное. Восстановите "
                    "вручную или очистите дерево",
                )
            rc, _, err = await _git("reset", "--hard", sha, repo=repo, check=False)
        else:
            rc, _, err = await _git("branch", "-f", branch, sha, repo=repo, check=False)
        if rc != 0:
            return (
                "unavailable",
                f"не смог поставить локальную ветку {branch} на {sha[:12]}: "
                f"{err[:150] or 'git молчит'}",
            )
        rc, _, err = await _git("push", "origin", branch, repo=repo, check=False)
        if rc != 0:
            return ("unavailable", f"push не прошёл: {err[:200] or 'git молчит'}")
        return ("restored", sha)

    async def base_freshness(self, repo: str, base: str, sha: str) -> tuple[str, str]:
        """Is the base this diff stands on still the one the remote carries (#947)?

        ``(current|stale|unverified, detail)``. The middle state is the point:
        a base can resolve perfectly well inside the workspace and name a
        commit the remote branch does not carry — a squash release rewrote the
        line, or the branch was deleted and the clone kept its copy. Every
        check that reads the diff then compares against a world that no longer
        exists, and produces the most dangerous of answers: an empty one.

        Never guesses. When the remote-tracking ref is absent the remote itself
        is asked, so "the branch is gone upstream" is separated from "this
        clone has not fetched it" instead of the two sharing a verdict.
        """
        rc, remote_sha, _ = await _git(
            "rev-parse",
            "--verify",
            "--quiet",
            f"origin/{base}^{{commit}}",
            repo=repo,
            check=False,
        )
        if rc == 0 and remote_sha.strip():
            remote_sha = remote_sha.strip()
            if remote_sha == sha:
                return ("current", remote_sha)
            rc, _, _ = await _git(
                "merge-base", "--is-ancestor", sha, remote_sha, repo=repo, check=False
            )
            if rc == 0:
                return ("current", remote_sha)
            return (
                "stale",
                f"origin/{base} стоит на {remote_sha[:12]}, а база сравнения — "
                f"{sha[:12]}, которого в этой ветке нет",
            )

        rc, _, err = await _git("remote", "get-url", "origin", repo=repo, check=False)
        if rc != 0:
            return (
                "unverified",
                f"у клона нет origin, сверять базу не с чем: {err[:150]}",
            )
        rc, out, err = await _git(
            "ls-remote",
            "--heads",
            "origin",
            f"refs/heads/{base}",
            repo=repo,
            check=False,
        )
        if rc != 0:
            return ("unverified", f"ls-remote не ответил: {err[:150] or 'git молчит'}")
        if not out.strip():
            return (
                "stale",
                f"ветки {base} нет в remote проекта — база сравнения указывает на "
                f"{sha[:12]} из ветки, которой больше не существует",
            )
        return (
            "unverified",
            f"ветка {base} в remote есть, но клон её не забирал: "
            "origin/{base} в нём отсутствует, свежесть базы не проверить".format(
                base=base
            ),
        )

    async def head_sha(self, repo: str, base: str) -> str:
        """Current tip of origin/<base>, or "" when it cannot be read (#534)."""
        rc, out, _ = await _git("rev-parse", f"origin/{base}", repo=repo, check=False)
        return out.strip() if rc == 0 else ""

    async def branch_diff(self, repo: str, base: str, branch: str) -> str | None:
        """``git diff -U0 base...branch``, or None when it cannot be read (#601).

        None rather than "" on failure: an empty diff and an unreadable one are
        different answers, and the section must be able to say which.
        """
        rc, out, _ = await _git(
            "diff",
            "-U0",
            f"{base}...{branch}",
            repo=repo,
            check=False,
        )
        return out if rc == 0 else None

    async def file_at_ref(self, repo: str, ref: str, path: str) -> str | None:
        """``git show <ref>:<path>``, or None when it is not there (#873).

        Callers pass the BASE ref, never the branch under review — see
        ``collect_review_rules``. Absent and unreadable collapse into None on
        purpose here: for a per-directory policy file both mean "no rules from
        this path", and the caller states the distinction that matters (no
        rules anywhere vs no repository to look in) one level up.
        """
        rc, out, _ = await _git("show", f"{ref}:{path}", repo=repo, check=False)
        return out if rc == 0 else None

    async def files_at_ref(self, repo: str, ref: str) -> set[str] | None:
        """Every path in the tree of ``ref``; ``None`` when it could not be read.

        The distinction file_at_ref deliberately collapses is the one the AC
        locator check needs (#764): a file the submission never added is
        ``missing`` and a file that could not be read is ``unknown``, and
        turning the second into the first is exactly the false accusation
        #506 forbids. Knowing the tree tells the two apart in one call.
        """
        rc, out, _ = await _git(
            "ls-tree", "-r", "--name-only", ref, repo=repo, check=False
        )
        if rc != 0:
            return None
        return {line.strip() for line in out.splitlines() if line.strip()}

    async def commit_exists(self, repo: str, sha: str) -> bool | None:
        """Is this commit here? ``None`` when the repository could not be read.

        Three answers, never two — the same rule ``resolve_ref`` follows (#725).
        "We could not look" printed as "that commit is not here" would accuse a
        submission of having vanished when it is the workspace that is missing.
        """
        rc, _, _ = await _git("rev-parse", "--git-dir", repo=repo, check=False)
        if rc != 0:
            return None
        rc, _, _ = await _git(
            "cat-file", "-e", f"{sha}^{{commit}}", repo=repo, check=False
        )
        return rc == 0

    async def commit_diff(
        self, repo: str, base: str, sha: str, *, context: int = 3
    ) -> str | None:
        """``git diff base...sha``, or None when it cannot be read (#824).

        Against the PINNED sha, never the branch name: the verdict is cast on
        one submission, and a branch that moved after it would show the human
        code they are not approving. Carries real context lines — this diff is
        read by a person, unlike the ``-U0`` one call-site analysis parses.
        """
        rc, out, _ = await _git(
            "diff",
            f"-U{int(context)}",
            f"{base}...{sha}",
            repo=repo,
            check=False,
        )
        return out if rc == 0 else None

    async def is_ancestor(
        self, repo: str, ancestor: str, descendant: str
    ) -> bool | None:
        """Is ``ancestor`` in the history of ``descendant``? (#497)

        Three answers, never two. ``None`` means the question could not be
        asked — an unreadable repository, or a commit this checkout does not
        carry — and it must never collapse into ``False``: "we did not check"
        printed as "not deployed" is the exact failure this epic removes.
        """
        rc, _, _ = await _git("rev-parse", "--git-dir", repo=repo, check=False)
        if rc != 0:
            return None
        for sha in (ancestor, descendant):
            rc, _, _ = await _git(
                "cat-file", "-e", f"{sha}^{{commit}}", repo=repo, check=False
            )
            if rc != 0:
                return None
        rc, _, _ = await _git(
            "merge-base", "--is-ancestor", ancestor, descendant, repo=repo, check=False
        )
        return rc == 0

    async def commit_with_same_tree(
        self, repo: str, sha: str, branch: str
    ) -> str | None:
        """The newest commit on ``branch`` whose content equals ``sha``'s (#946).

        Three answers, like every other question asked here. A commit id means
        the branch held exactly this content at that point; ``""`` means git
        looked through the branch and it never did; ``None`` means the question
        could not be asked — an unreadable repository, an unknown commit, a
        branch this checkout does not carry.

        Exists because a squash release keeps the CONTENT and drops the
        ancestry: main gets a brand-new commit whose tree equals develop's, so
        ``is_ancestor`` answers "no" about work that is demonstrably running.
        Trees are how git already states "the same content", so this asks git
        rather than storing a second copy of the fact in the hub, which would
        be one more thing to go stale (#497).
        """
        rc, tree, _ = await _git("rev-parse", f"{sha}^{{tree}}", repo=repo, check=False)
        want = (tree or "").strip()
        if rc != 0 or not want:
            return None
        # origin/<branch> first: the shared clone sits on the base branch but
        # the question is about what upstream held, not about this checkout.
        for ref in (f"origin/{branch}", branch):
            rc, out, _ = await _git(
                "log", "--format=%T %H", ref, repo=repo, check=False
            )
            if rc != 0:
                continue
            for line in (out or "").splitlines():
                found_tree, _, commit = line.partition(" ")
                if found_tree == want:
                    return commit.strip()
            return ""
        return None

    async def commit_diff_stat(
        self, repo: str, base: str, sha: str
    ) -> list[tuple[int, int, str]] | None:
        """``git diff --numstat base...sha``, or None when unreadable (#825).

        Paths and counts only. The card needs to know WHICH files a submission
        touched to lay criteria against them; reading every hunk for that would
        put the cost of the full diff on every gate render, and the full diff
        is already loaded on demand (#824).
        """
        rc, out, _ = await _git(
            "diff", "--numstat", f"{base}...{sha}", repo=repo, check=False
        )
        if rc != 0:
            return None
        rows: list[tuple[int, int, str]] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, removed, path = parts
            # "-" stands for a binary file: no line counts, but the path still
            # changed and must not vanish from the map.
            rows.append(
                (
                    int(added) if added.isdigit() else 0,
                    int(removed) if removed.isdigit() else 0,
                    path.strip(),
                )
            )
        return rows

    async def fetch_base(self, repo: str, base: str) -> tuple[bool, str]:
        """Refresh one base branch from origin. Read-only, never writes (#534)."""
        rc, _, err = await _git("fetch", "origin", base, repo=repo, check=False)
        return (rc == 0, err or "")

    async def fetch_commit(
        self, repo: str, sha: str, ref: str = "", *, timeout: int = 20
    ) -> tuple[bool, str]:
        """Bring one commit's objects into ``repo`` (#883). Objects ONLY.

        No checkout, no reset, no HEAD movement: pair tasks and their worktrees
        live in this same clone, and moving its working tree to answer a
        question about history would break work in progress.

        Two attempts, because servers differ: fetching a bare sha is allowed
        only when ``uploadpack.allowReachableSHA1InWant`` is on, so a refusal
        falls back to the ref the deploy reported. Failure is returned, never
        raised — the caller degrades to "could not check", which must stay
        distinguishable from "not deployed".
        """
        rc, _, err = await _git(
            "fetch", "--quiet", "origin", sha, repo=repo, check=False, timeout=timeout
        )
        if rc == 0:
            return (True, "")
        if ref:
            rc, _, err_ref = await _git(
                "fetch",
                "--quiet",
                "origin",
                ref,
                repo=repo,
                check=False,
                timeout=timeout,
            )
            if rc == 0:
                return (True, "")
            return (False, (err_ref or err or "").strip())
        return (False, (err or "").strip())

    async def first_parent_log(self, repo: str, base: str, limit: int) -> str | None:
        """Commits on the base's own line, newest first, or None on failure.

        --first-parent stays on the base line so work merged from a branch is
        represented by its merge commit rather than by every commit inside it.
        The unit separator keeps subjects with spaces intact (#534).
        """
        rc, out, _ = await _git(
            "log",
            f"origin/{base}",
            "--first-parent",
            f"-{int(limit)}",
            "--format=%H\x1f%s\x1f%an",
            repo=repo,
            check=False,
        )
        return out if rc == 0 else None

    def worktree_path(self, task_id: int, repo: str | None = None) -> str:
        """Deterministic worktree path for a task (#459); where its branch lives."""
        return _worktree_path(task_id, repo or _repo_root())

    async def worktree_is_registered(self, path: str, repo: str | None = None) -> bool:
        """True when ``path`` is a registered worktree of ``repo`` (#989)."""
        return await _worktree_registered(path, repo or _repo_root())

    async def pair_prepare_worktree(
        self,
        task_id: int,
        title: str,
        *,
        branch_slug: str = "",
        repo: str | None = None,
        base_branch: str | None = None,
    ) -> str:
        """Create/reuse a per-task git worktree; the main clone stays on base (#459).

        Returns the branch name (same contract as ``pair_prepare_branch``), but
        instead of switching the single working tree it gives each task its own
        worktree at a deterministic sibling path. Two pair-starts therefore never
        share a working tree — no mutual checkout, no cross-task clobber. The main
        clone is never moved off the base branch.
        """
        import os

        if repo is None:
            reason = await _default_workspace_error()
            if reason:
                raise PairBranchConflictError(reason)
        repo = repo or _repo_root()
        base = _resolve_base(base_branch)
        branch = canonical_task_branch(task_id, branch_slug, title)
        wt_path = _worktree_path(task_id, repo)

        # Clear stale registrations (a worktree dir deleted out from under git)
        # so a fresh add for the same task never trips a false conflict (AC-4).
        await _git("worktree", "prune", repo=repo, check=False)

        # Reuse this task's existing worktree; never clobber its uncommitted work.
        if await _worktree_registered(wt_path, repo) and os.path.isdir(wt_path):
            rc, cur, _ = await _git(
                "branch", "--show-current", repo=wt_path, check=False
            )
            cur = (cur or "").strip()
            if cur == branch:
                return branch
            dirty, dirty_files = await _dirty_state(wt_path)
            if dirty.strip():
                raise _pair_branch_conflict(
                    f"Worktree {wt_path} is on {cur!r} with uncommitted changes; "
                    f"refusing to switch it to {branch!r}. "
                    f"Files: {_name_dirty_files(dirty_files)}",
                    repo=wt_path,
                    reason="pair_worktree_dirty",
                    files=dirty_files,
                    hint=(
                        f"On host {_hostname()}: {_rescue_command(wt_path, task_id)} "
                        f"(or commit them), then retry pair-start for #{task_id}."
                    ),
                    task_id=task_id,
                )
            # Switch the reused worktree to the target branch. Ensure the branch
            # exists (create from base when missing) and verify the checkout —
            # never return a branch we failed to check out, which would record a
            # wrong branch and push the wrong work (silent false success).
            rc, _, _ = await _git(
                "rev-parse", "--verify", branch, repo=repo, check=False
            )
            if rc == 0:
                rc, _, err = await _git("checkout", branch, repo=wt_path, check=False)
            else:
                if await _base_ahead_of_origin(base, repo):
                    raise _pair_branch_conflict(
                        f"Local {base!r} is ahead of origin/{base!r} in {repo}; "
                        f"refusing to cut {branch!r} onto unpushed base commits",
                        repo=repo,
                        reason="pair_base_ahead_of_origin",
                        task_id=task_id,
                    )
                rc, _, err = await _git(
                    "checkout", "-b", branch, base, repo=wt_path, check=False
                )
            if rc != 0:
                raise _pair_branch_conflict(
                    f"Failed to switch worktree {wt_path} to {branch}: "
                    f"{(err or '').strip() or 'git checkout failed'}",
                    repo=wt_path,
                    reason="pair_worktree_create_failed",
                    task_id=task_id,
                )
            return branch

        # Fresh worktree. Keep the main clone's base current (best-effort).
        await _git("pull", "origin", base, "--ff-only", repo=repo, check=False)
        os.makedirs(os.path.dirname(wt_path), exist_ok=True)

        rc, _, _ = await _git("rev-parse", "--verify", branch, repo=repo, check=False)
        if rc == 0:
            rc, _, err = await _git(
                "worktree", "add", wt_path, branch, repo=repo, check=False
            )
        else:
            # New branch cut from base — guard against a base that diverged from
            # origin (broken fetch → foreign commits, #457/#392).
            if await _base_ahead_of_origin(base, repo):
                raise _pair_branch_conflict(
                    f"Local {base!r} is ahead of origin/{base!r} in {repo}; "
                    f"refusing to cut {branch!r} onto unpushed base commits",
                    repo=repo,
                    reason="pair_base_ahead_of_origin",
                    hint=(
                        f"On {_hostname()}: cd {repo} && git log origin/{base}..{base} "
                        f"to inspect, then reconcile before pair-start for #{task_id}."
                    ),
                    task_id=task_id,
                )
            rc, _, err = await _git(
                "worktree", "add", "-b", branch, wt_path, base, repo=repo, check=False
            )
        if rc != 0:
            raise _pair_branch_conflict(
                f"Failed to create worktree for {branch} at {wt_path}: "
                f"{(err or '').strip() or 'git worktree add failed'}",
                repo=repo,
                reason="pair_worktree_create_failed",
                task_id=task_id,
            )
        log.info(
            "pair_prepare_worktree: %s at %s (main clone stays on %s)",
            branch,
            wt_path,
            base,
        )
        return branch

    async def pair_remove_worktree(
        self, task_id: int, *, repo: str | None = None
    ) -> bool:
        """Remove a task's worktree if clean; never lose uncommitted work (#459)."""
        import os

        if repo is None:
            reason = await _default_workspace_error()
            if reason:
                log.warning("pair_remove_worktree: %s", reason)
                return False
        repo = repo or _repo_root()
        wt_path = _worktree_path(task_id, repo)

        if not await _worktree_registered(wt_path, repo):
            await _git("worktree", "prune", repo=repo, check=False)
            return True

        if os.path.isdir(wt_path):
            rc, dirty, _ = await _git(
                "status", "--porcelain", repo=wt_path, check=False
            )
            if dirty.strip():
                log.warning(
                    "pair_remove_worktree: %s has uncommitted changes, skipping",
                    wt_path,
                )
                return False

        rc, _, err = await _git("worktree", "remove", wt_path, repo=repo, check=False)
        await _git("worktree", "prune", repo=repo, check=False)
        if rc != 0:
            log.warning("pair_remove_worktree: remove %s failed: %s", wt_path, err)
            return False
        log.info("pair_remove_worktree: removed %s", wt_path)
        return True

    async def origin_reachable(
        self, repo: str | None = None, *, timeout: int = 30
    ) -> bool:
        """True when ``git ls-remote origin`` succeeds (#455).

        A live check that the workspace can actually reach GitHub (deploy key,
        ssh/network). Used to surface a silently stale base — pair branches cut
        from a workspace whose fetch fails check=False land on old refs.
        """
        repo = repo or _repo_root()
        rc, _, _ = await _git(
            "ls-remote",
            "--heads",
            "origin",
            repo=repo,
            check=False,
            timeout=timeout,
        )
        return rc == 0

    async def checkout(self, branch: str, repo: str | None = None) -> bool:
        rc, _, _ = await _git("checkout", branch, repo=repo, check=False)
        return rc == 0

    async def dirty_paths(self, repo: str | None = None) -> list[str]:
        """Repo-relative paths git would stage right now (#361 commit-scope).

        Read before auto_commit so the caller can compare what is about to land
        against the task's declared scope. Returns [] both for a clean tree and
        for an unreadable one — the caller must not read that as proof of
        cleanliness; a failed status is logged here and surfaces there as
        "cannot check".
        """
        # -z, not plain --porcelain: git escapes non-ASCII paths in the plain
        # form, and these paths are compared against the task's declared areas,
        # so an escaped name would read as a foreign file (#555).
        rc, out, err = await _git("status", "--porcelain", "-z", repo=repo, check=False)
        if rc != 0:
            log.error(
                "dirty_paths: git status failed in %s: %s",
                repo or _repo_root(),
                (err or "").strip(),
            )
            return []
        return parse_porcelain_paths(out or "")

    async def branch_diff_paths(
        self,
        branch: str,
        base_branch: str | None = None,
        repo: str | None = None,
    ) -> list[str] | None:
        """Files the branch changes relative to its base, or None if unknowable.

        None and [] are deliberately different: [] means "the branch changes
        nothing", None means "we could not find out". Collapsing the two is
        how a check reports agreement it never established — the same
        one-value-two-meanings mistake that surfaced three times in #361.

        ``-z`` from the start: git escapes non-ASCII paths in the plain form
        exactly as ``git status`` does, and these paths are compared against
        the task's declared areas, so an escaped name would read as
        undeclared. Verified against real output before writing this (#555).
        """
        repo = repo or _repo_root()
        base = _resolve_base(base_branch)
        if not (branch or "").strip():
            return None
        # #762: refresh before comparing, then compare against origin. The tip
        # resolver has always fetched (#572) and this one never did, so on a
        # pair task the hub pinned the right commit and diffed a stale local
        # ref — an empty diff that read as "the branch changes nothing" and
        # silently disabled both the surface check (#550) and the risk-class
        # recompute (#583). The fetch is best effort: a workspace that cannot
        # reach origin still has refs, and refusing on a network blip would
        # trade a silent wrong answer for a loud missing one.
        rc, _, err = await _git("fetch", "origin", base, branch, repo=repo, check=False)
        if rc != 0:
            log.warning(
                "branch_diff_paths: could not fetch %s/%s in %s: %s — "
                "comparing whatever refs are already here",
                base,
                branch,
                repo,
                (err or "").strip(),
            )
        base_sha = await _resolve_ref_remote_first(base, repo)
        if base_sha is None:
            log.warning("branch_diff_paths: base %r not found in %s", base, repo)
            return None
        branch_sha = await _resolve_ref_remote_first(branch, repo)
        if branch_sha is None:
            log.warning("branch_diff_paths: branch %r not found in %s", branch, repo)
            return None
        rc, out, err = await _git(
            "diff",
            "--name-only",
            "-z",
            f"{base_sha}...{branch_sha}",
            repo=repo,
            check=False,
        )
        if rc != 0:
            log.warning(
                "branch_diff_paths: diff failed for %s...%s: %s",
                base_sha,
                branch_sha,
                (err or "").strip(),
            )
            return None
        return [p for p in (out or "").split("\0") if p.strip()]

    async def auto_commit(
        self,
        task_id: int,
        title: str = "",
        message: str | None = None,
        repo: str | None = None,
        expected_branch: str | None = None,
    ) -> bool:
        """Commit the task's work. Refuses to commit onto a foreign branch.

        ``expected_branch`` closes the other half of #361 I1. The done-pipeline
        checks out the task branch and then calls this — but it never looked at
        whether the checkout succeeded, so a failed checkout (a dirty tree is
        enough) left the commit landing on whichever branch was still current.
        With the branch named, a mismatch refuses instead.

        The blanket ``git add -A`` below stages whatever is dirty. Do NOT read
        that as "everything dirty belongs to the task": create_branch's refusal
        to start on a dirty tree (#361 I2) only proves the tree was clean at
        t=0, and a headless task then shares the main clone for its whole run.
        Attribution lives one level up, in the commit-scope gate
        (hub/commit_scope.py), which compares the dirty set against
        the task's declared affected_areas before this is called.
        """
        repo = repo or _repo_root()

        if expected_branch:
            rc, current, _ = await _git(
                "branch", "--show-current", repo=repo, check=False
            )
            current = (current or "").strip()
            if rc != 0 or current != expected_branch:
                raise WorkspaceBranchMismatchError(
                    f"refusing to commit #{task_id} onto {current!r} — "
                    f"expected {expected_branch!r} in {repo}"
                )

        reverted = await _reject_broken_files(repo)
        if reverted:
            log.warning(
                "auto_commit: reverted %d broken file(s) for task #%d: %s",
                len(reverted),
                task_id,
                ", ".join(reverted),
            )

        rc, status, _ = await _git("status", "--porcelain", repo=repo, check=False)
        if not status.strip():
            log.info("auto_commit: no changes for task #%d", task_id)
            return False

        if message:
            msg = message
        else:
            ctype = _conv_commit_type(title) if title else "feat"
            msg = f"{ctype}(task): auto-commit for #{task_id}"

        await _git("add", "-A", repo=repo)
        rc, _, err = await _git(
            "commit", "-m", msg, "--no-verify", repo=repo, check=False
        )
        if rc == 0:
            log.info("auto_commit: committed for task #%d", task_id)
            return True
        log.warning("auto_commit failed for task #%d: %s", task_id, err)
        return False

    async def pull_main(
        self, repo: str | None = None, base_branch: str | None = None
    ) -> bool:
        """Return the shared workspace to its base branch and fast-forward it.

        The branch is the task's base, not the literal "main" this used to
        name (#552). Since #362 a task's work is cut from PAIR_BASE_BRANCH, so
        naming main here left the clone parked on whatever branch was current
        and pulled a branch nobody builds on — on a repository without main it
        simply did nothing at all. Reproduced: after a merge the workspace was
        still sitting on the task branch.

        A failed checkout raises rather than falling through to the pull. The
        old code discarded that rc; git happened to refuse the pull too, so no
        damage was reachable, but the caller could not tell the difference
        between "returned to base" and "still on someone's branch".
        """
        repo = repo or _repo_root()
        base = _resolve_base(base_branch)
        rc, _, err = await _git("checkout", base, repo=repo, check=False)
        if rc != 0:
            raise WorkspaceNotReadyError(
                f"cannot return {repo} to {base!r}: "
                + ((err or "").strip() or "git checkout failed")
            )
        rc, _, _ = await _git(
            "pull", "origin", base, "--ff-only", repo=repo, check=False
        )
        return rc == 0

    async def squash_branch(
        self,
        task_id: int,
        title: str,
        branch: str,
        repo: str | None = None,
        base_branch: str | None = None,
    ) -> bool:
        """Collapse the task's commits into one, measured from its own base.

        The squash point must be the branch's base — the same one create_branch
        cut from and create_pr targets. It used to be hardcoded to origin/main,
        which agreed with create_branch only while that also defaulted to main.
        Once #362 moved the cut to the integration branch, merge-base against
        origin/main became the point where the two branches forked, so the
        reset swallowed every commit the base had gained since: the PR then
        showed the base's own work as the task's, and merging it raised
        add/add conflicts on files the task never touched. Reproduced on a
        real repository before this was changed.
        """
        repo = repo or _repo_root()
        base = _resolve_base(base_branch)

        rc, _, err = await _git("checkout", branch, repo=repo, check=False)
        if rc != 0:
            # Everything below rewrites history. Running it on whatever branch
            # stayed current is the one outcome worth refusing outright (#552).
            log.error(
                "squash_branch: cannot check out %s in %s: %s",
                branch,
                repo,
                (err or "").strip() or "git checkout failed",
            )
            return False
        await _git("fetch", "origin", base, repo=repo, check=False)

        rc, merge_base, _ = await _git(
            "merge-base",
            f"origin/{base}",
            branch,
            repo=repo,
            check=False,
        )
        if rc != 0 or not merge_base.strip():
            log.warning(
                "squash_branch: cannot find merge-base for %s against origin/%s",
                branch,
                base,
            )
            return False

        rc, rev_count, _ = await _git(
            "rev-list",
            "--count",
            f"{merge_base.strip()}..HEAD",
            repo=repo,
            check=False,
        )
        if rc != 0 or int(rev_count.strip() or "0") <= 1:
            log.info("squash_branch: %s has <=1 commit, skip squash", branch)
            return True

        rc, _, err = await _git(
            "reset",
            "--soft",
            merge_base.strip(),
            repo=repo,
            check=False,
        )
        if rc != 0:
            log.error("squash_branch: reset failed for %s: %s", branch, err)
            return False

        ctype = _conv_commit_type(title) if title else "feat"
        slug = _slugify(title, max_len=60)
        msg = f"{ctype}(task): {slug} (#{task_id})"

        await _git("add", "-A", repo=repo)
        rc, _, err = await _git(
            "commit",
            "-m",
            msg,
            "--no-verify",
            repo=repo,
            check=False,
        )
        if rc != 0:
            log.error("squash_branch: commit failed for %s: %s", branch, err)
            return False

        log.info("squash_branch: squashed %s commits on %s", rev_count.strip(), branch)
        return True

    async def push_branch(
        self, branch: str, repo: str | None = None, force: bool = False
    ) -> bool:
        args = ["push", "-u", "origin", branch]
        if force:
            args.insert(1, "--force-with-lease")
        rc, _, err = await _git(*args, repo=repo, check=False, timeout=60)
        if rc == 0:
            log.info(
                "Pushed branch %s to origin%s", branch, " (force)" if force else ""
            )
            return True
        log.error("Failed to push %s: %s", branch, err)
        return False

    async def create_pr(
        self,
        task_id: int,
        title: str,
        description: str,
        branch: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        base_branch: str | None = None,
    ) -> int | None:
        ctype = _conv_commit_type(title)
        pr_title = f"{ctype}(task): {title} (#{task_id})"
        body = (
            f"## Task #{task_id}\n\n"
            f"{description or 'No description'}\n\n"
            "---\n*Created automatically by Haiplane Hub*"
        )

        rc, out, err = await _gh(
            "pr",
            "create",
            "--repo",
            gh_repo or REPO_NAME,
            "--base",
            _resolve_base(base_branch),
            "--head",
            branch,
            "--title",
            pr_title,
            "--body",
            body,
            repo=repo,
            check=False,
            timeout=30,
        )
        if rc != 0:
            if "already exists" in err:
                return await _find_pr_for_branch(branch, repo, gh_repo=gh_repo)
            log.error("Failed to create PR for %s: %s", branch, err)
            return None

        pr_number = _parse_pr_number(out)
        if pr_number:
            log.info("Created PR #%d for branch %s", pr_number, branch)
        return pr_number

    async def get_ci_failure_logs(
        self,
        pr_number: int,
        branch: str,
        max_log_chars: int = 12000,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"failed_checks": [], "log_summary": "", "run_url": ""}

        rc, out, _ = await _gh(
            "pr",
            "checks",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--json",
            "name,state",
            repo=repo,
            check=False,
        )
        if rc == 0 and out:
            try:
                checks = json.loads(out)
                result["failed_checks"] = [
                    c["name"]
                    for c in checks
                    if c.get("state", "").upper()
                    in ("FAILURE", "ERROR", "ACTION_REQUIRED")
                ]
            except (json.JSONDecodeError, KeyError):
                pass

        rc, out, _ = await _gh(
            "run",
            "list",
            "--repo",
            gh_repo or REPO_NAME,
            "--branch",
            branch,
            "--limit",
            "1",
            "--json",
            "databaseId,url,status",
            repo=repo,
            check=False,
        )
        run_id = None
        if rc == 0 and out:
            try:
                runs = json.loads(out)
                if runs:
                    run_id = runs[0].get("databaseId")
                    result["run_url"] = runs[0].get("url", "")
            except (json.JSONDecodeError, KeyError, IndexError):
                pass

        if run_id:
            rc, out, _ = await _gh(
                "run",
                "view",
                str(run_id),
                "--repo",
                gh_repo or REPO_NAME,
                "--log-failed",
                repo=repo,
                check=False,
                timeout=90,
            )
            if rc == 0 and out:
                if len(out) > max_log_chars:
                    out = out[-max_log_chars:]
                    out = "... (truncated) ...\n" + out
                result["log_summary"] = out

        return result

    async def _repo_has_workflows(
        self, *, repo: str | None, gh_repo: str | None
    ) -> bool | None:
        """Whether the repository defines any Actions workflow (#1041).

        Called only on the branch that used to return ``absent``: empty
        ``gh pr checks`` or empty ``actions/runs?head_sha=``. A working
        probe never pays this extra GitHub call.
        """
        rc, out, _err = await _gh(
            "api",
            f"repos/{gh_repo or REPO_NAME}/actions/workflows",
            repo=repo,
            check=False,
        )
        if rc != 0 or not (out or "").strip():
            return None
        try:
            payload = json.loads(out)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        workflows = payload.get("workflows")
        if isinstance(workflows, list) and workflows:
            return True
        try:
            return int(payload.get("total_count") or 0) > 0
        except (TypeError, ValueError):
            return None

    async def _pr_head_sha(
        self, pr_number: int, *, repo: str | None, gh_repo: str | None
    ) -> str:
        rc, out, _err = await _gh(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--json",
            "headRefOid",
            repo=repo,
            check=False,
        )
        if rc != 0 or not (out or "").strip():
            return ""
        try:
            return str(json.loads(out).get("headRefOid") or "").strip()
        except (json.JSONDecodeError, AttributeError, TypeError):
            return ""

    async def _absent_or_missing_run(
        self,
        pr_number: int,
        reason: str,
        *,
        repo: str | None,
        gh_repo: str | None,
        sha: str = "",
    ) -> CIProbeResult:
        """Split 'no CI in the repo' from 'this SHA has no run yet' (#1041)."""
        has = await self._repo_has_workflows(repo=repo, gh_repo=gh_repo)
        if has is None:
            return CIProbeResult(CIProbeOutcome.unavailable, "workflows_unavailable")
        if not has:
            return CIProbeResult(CIProbeOutcome.absent, reason)
        if not sha:
            sha = await self._pr_head_sha(pr_number, repo=repo, gh_repo=gh_repo)
        return CIProbeResult(CIProbeOutcome.missing_run, reason, details=sha or None)

    async def _workflow_runs_probe(
        self, pr_number: int, repo: str | None, gh_repo: str | None
    ) -> CIProbeResult:
        """CI verdict from Actions workflow runs, by the PR's head SHA (#606).

        The primary probe (``gh pr checks``) needs ``checks:read`` — a
        permission GitHub removed from the fine-grained token picker while
        the API still demands it (verified 2026-08-03: the picker's search
        for "check" answers "No items available", the API answers 403). This
        path uses only permissions a token can actually be granted: Pull
        requests (headRefOid) and Actions (workflow runs).

        The mapping is conservative — unknown is never ok: an unrecognised
        conclusion reads as unavailable, not as passed. A workflow's
        conclusion is GitHub's own aggregate over its jobs, so skipped jobs
        (Deploy on a PR) are already accounted for.
        """
        rc, out, err = await _gh(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--json",
            "headRefOid",
            repo=repo,
            check=False,
        )
        if rc != 0 or not (out or "").strip():
            return CIProbeResult(
                CIProbeOutcome.unavailable,
                "workflow_runs_unavailable",
                details=f"could not read headRefOid: {(err or '').strip()}",
            )
        try:
            head_sha = str(json.loads(out).get("headRefOid") or "").strip()
        except json.JSONDecodeError:
            head_sha = ""
        if not head_sha:
            return CIProbeResult(
                CIProbeOutcome.unavailable,
                "workflow_runs_unavailable",
                details="PR carries no readable headRefOid",
            )

        rc, out, err = await _gh(
            "api",
            f"repos/{gh_repo or REPO_NAME}/actions/runs?head_sha={head_sha}",
            repo=repo,
            check=False,
        )
        if rc != 0 or not (out or "").strip():
            return CIProbeResult(
                CIProbeOutcome.unavailable,
                "workflow_runs_unavailable",
                details=(err or "").strip(),
            )
        try:
            runs = json.loads(out).get("workflow_runs") or []
        except (json.JSONDecodeError, AttributeError):
            return CIProbeResult(
                CIProbeOutcome.unavailable, "workflow_runs_invalid_json"
            )

        # No runs for this SHA used to mean "the repository has no CI"
        # (#419). That collapsed two facts: no workflows at all, and
        # workflows that have not yet produced a run for this commit.
        if not runs:
            return await self._absent_or_missing_run(
                pr_number,
                "no_workflow_runs",
                repo=repo,
                gh_repo=gh_repo,
                sha=head_sha,
            )

        statuses = [str(r.get("status") or "").lower() for r in runs]
        conclusions = [str(r.get("conclusion") or "").lower() for r in runs]
        if any(
            s in ("queued", "in_progress", "waiting", "requested", "pending")
            for s in statuses
        ):
            return CIProbeResult(CIProbeOutcome.pending, "workflow_runs_running")
        if any(
            c
            in (
                "failure",
                "cancelled",
                "timed_out",
                "startup_failure",
                "action_required",
            )
            for c in conclusions
        ):
            return CIProbeResult(CIProbeOutcome.failed, "workflow_runs_failed")
        if all(s == "completed" for s in statuses) and all(
            c in ("success", "neutral", "skipped") for c in conclusions
        ):
            return CIProbeResult(CIProbeOutcome.passed, "workflow_runs_passed")
        return CIProbeResult(
            CIProbeOutcome.unavailable,
            "workflow_runs_unknown_state",
            details=",".join(sorted(set(conclusions) | set(statuses))),
        )

    async def branch_ci_runs(
        self,
        branch: str,
        limit: int = 20,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> list[dict[str, Any]] | None:
        """Push runs on ``branch``, newest first — or None if unreadable (#929).

        The PR probe above asks about one commit; this asks about the branch's
        own history, which is what says whether the BASE is green right now
        and, if not, which commits arrived since it last was.

        None rather than an empty list when the question could not be asked:
        "no runs" and "could not look" lead to opposite conclusions, and the
        caller must not be able to confuse them by accident (#725).
        """
        rc, out, err = await _gh(
            "api",
            f"repos/{gh_repo or REPO_NAME}/actions/runs"
            f"?branch={branch}&event=push&per_page={max(1, min(limit, 100))}",
            repo=repo,
            check=False,
        )
        if rc != 0 or not (out or "").strip():
            log.warning(
                "branch CI history for %s unavailable: %s", branch, (err or "").strip()
            )
            return None
        try:
            runs = json.loads(out).get("workflow_runs")
        except (json.JSONDecodeError, AttributeError):
            log.warning("branch CI history for %s: invalid json", branch)
            return None
        if runs is None:
            return None
        return [
            {
                "sha": str(r.get("head_sha") or ""),
                "status": str(r.get("status") or "").lower(),
                "conclusion": str(r.get("conclusion") or "").lower(),
                "created_at": str(r.get("created_at") or ""),
                "name": str(r.get("name") or ""),
            }
            for r in runs
        ]

    async def check_pr_ci(
        self, pr_number: int, repo: str | None = None, gh_repo: str | None = None
    ) -> CIProbeResult:
        rc, out, err = await _gh(
            "pr",
            "checks",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--json",
            "name,state",
            repo=repo,
            check=False,
        )
        # rc error / empty output — the probe itself could not run. Before
        # answering "unavailable", try the workflow-runs fallback (#606): the
        # primary path needs checks:read, which GitHub's token picker no
        # longer offers while the API still demands it. A WORKING primary
        # path never reaches this line, so its behaviour is untouched.
        if rc != 0 or not out or not out.strip():
            fallback = await self._workflow_runs_probe(pr_number, repo, gh_repo)
            if fallback.outcome != CIProbeOutcome.unavailable:
                return fallback
            # Both paths failed: name the primary error — it is the one an
            # operator can act on (#419: not pending, nothing known in flight).
            return CIProbeResult(
                CIProbeOutcome.unavailable,
                "gh_error",
                details=(err or "").strip() or fallback.details,
            )
        try:
            checks = json.loads(out)
        except json.JSONDecodeError:
            return CIProbeResult(CIProbeOutcome.unavailable, "invalid_json")

        # An empty check set used to skip the conveyor. It is still "no
        # checks", but if the repo has workflows this SHA simply has no run
        # yet — GitHub's registration lag, not "there is nothing to check".
        if not checks:
            return await self._absent_or_missing_run(
                pr_number, "no_checks", repo=repo, gh_repo=gh_repo
            )

        states = [c.get("state", "").upper() for c in checks]
        if any(
            s in ("PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "")
            for s in states
        ):
            return CIProbeResult(CIProbeOutcome.pending, "checks_running")
        if any(s in ("FAILURE", "ERROR", "ACTION_REQUIRED") for s in states):
            return CIProbeResult(CIProbeOutcome.failed, "checks_failed")
        if all(s in ("SUCCESS", "NEUTRAL", "SKIPPED") for s in states):
            return CIProbeResult(CIProbeOutcome.passed, "checks_passed")
        # Reached only for states gh reports that we do not recognise — treat as
        # unavailable (a stable reason) rather than silently waiting.
        return CIProbeResult(
            CIProbeOutcome.unavailable, "unknown_state", details=",".join(states)
        )

    async def pr_for_branch(
        self,
        branch: str,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> int | None:
        """The open PR whose head is ``branch``, or None (#605).

        The pair flow never records pr_number — only the headless create_pr
        paths do — so the delivery gate would have keyed on a field nobody
        sets. Discovery at submission time closes that: the hub looks the PR
        up itself instead of asking anyone to remember a number.
        """
        return await _find_pr_for_branch(branch, repo, gh_repo=gh_repo)

    async def content_differs(
        self,
        base: str,
        head: str,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> bool | None:
        """Does ``head`` hold content ``base`` does not? (#968)

        Three answers, like every other question asked of git here. ``True`` —
        the branches differ and there is something to release. ``False`` — the
        content is identical, whatever the commit graph says. ``None`` — the
        question could not be asked, and the caller must not read that as
        "nothing to release".

        Exists because counting commits stopped answering this question. A
        squash release writes a new commit on the release branch instead of
        carrying the originals, so ``base..head`` never empties and every
        cycle looked like undelivered work. On 26.08.2026 that opened twenty
        release PRs in ninety minutes, nineteen of them empty, each one
        redeploying production. Trees are how git states "the same content",
        so this compares them directly rather than counting what the graph
        happens to contain.
        """
        base = _resolve_base(base)
        # The clone may be behind, and the question is about upstream, not
        # about this checkout — so refresh the two ends first. A fetch that
        # fails is not fatal: the local refs may still answer, and only a
        # failure to COMPARE is "could not ask".
        await _git(
            "fetch",
            "origin",
            f"+{base}:refs/remotes/origin/{base}",
            f"+{head}:refs/remotes/origin/{head}",
            repo=repo,
            check=False,
        )
        # Order matters, and it was learned the hard way (#991 review round 2).
        # The done pipeline asks this BEFORE pushing, so origin/<head> does not
        # exist yet and the first pair cannot answer. The second pair is the one
        # that does: a local branch against the base as it stands UPSTREAM. The
        # local base is asked last and only as a fallback, because it drifts —
        # in a per-task worktree it may not be checked out at all, and in a
        # clone it may be stale or already carry the task's commits, which is
        # how "nothing to deliver" gets said about work that never shipped.
        for left, right in (
            (f"origin/{base}", f"origin/{head}"),
            (f"origin/{base}", head),
            (base, head),
        ):
            rc, _, _ = await _git(
                "diff", "--quiet", left, right, repo=repo, check=False
            )
            if rc == 0:
                return False
            if rc == 1:
                return True
        return None

    async def check_pr_mergeable(
        self,
        pr_number: int,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> tuple[MergeabilityOutcome, str]:
        """Can this PR be merged, and if not, why (#970).

        The release asked GitHub one question before merging — is CI green —
        and learned the rest by being refused. On 26.08.2026 PR #83 answered
        both at once: «Ruff and pytest» pass 4m2s, and mergeable=CONFLICTING.
        The poller called ``merge_pr`` every cycle, was refused every cycle,
        and reported «GitHub отказал» — a sentence that names who said no.
        A conflict, a revoked token and a deleted base branch all produce it,
        and none of them is fixed the same way.

        UNKNOWN is passed through as itself rather than folded into a
        conflict. GitHub computes mergeability asynchronously and honestly
        says UNKNOWN for the first seconds of a pull request's life — the
        release PR the poller just opened is exactly that case, every time.
        The next cycle asks again, the same way it already does for CI.
        """
        rc, out, err = await _gh(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--json",
            "mergeable,mergeStateStatus",
            repo=repo,
            check=False,
        )
        if rc != 0 or not (out or "").strip():
            detail = (err or "").strip() or "gh молчит"
            return (MergeabilityOutcome.unavailable, detail[:200])
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return (MergeabilityOutcome.unavailable, "ответ gh не разобран")

        mergeable = str(data.get("mergeable") or "").upper()
        state = str(data.get("mergeStateStatus") or "").upper()
        if mergeable == "MERGEABLE":
            return (MergeabilityOutcome.mergeable, state.lower() or "mergeable")
        if mergeable == "CONFLICTING":
            # The 409 GitHub would answer names no files, and the reason has
            # to lead somewhere. #969 already reads them out of the clone —
            # the same question, so the same answer, not a second one.
            files = await self._conflicting_files_of_pr(
                pr_number, repo=repo, gh_repo=gh_repo
            )
            named = f": {', '.join(files)}" if files else ""
            return (
                MergeabilityOutcome.conflicting,
                f"конфликт с базовой веткой{named}",
            )
        if mergeable == "UNKNOWN" or not mergeable:
            return (
                MergeabilityOutcome.unknown,
                f"GitHub ещё не посчитал слияние ({state.lower() or 'без статуса'})",
            )
        return (MergeabilityOutcome.unavailable, f"нераспознанный ответ {mergeable}")

    async def _conflicting_files_of_pr(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> list[str]:
        """Files this PR collides with its base on, when git can name them.

        Best effort by the same contract as ``_conflicting_files`` (#969):
        empty means "could not name them", never "there were none". The
        branch names come from the PR itself, so this works for a task PR as
        well as for the release one.
        """
        rc, out, _ = await _gh(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--json",
            "baseRefName,headRefName",
            repo=repo,
            check=False,
        )
        if rc != 0 or not (out or "").strip():
            return []
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return []
        base = str(data.get("baseRefName") or "").strip()
        head = str(data.get("headRefName") or "").strip()
        if not base or not head:
            return []
        return await self._conflicting_files(head, base, repo=repo)

    async def return_release_into_base(
        self,
        base: str,
        head: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> tuple[str, str]:
        """Merge the release branch back into the integration branch (#969).

        ``(returned <sha> | nothing | conflict | unavailable, detail)``. Four
        names rather than three, because a conflict and a git that could not
        be asked need different hands: one is a merge somebody has to resolve,
        the other is a question to ask again next cycle. Collapsing them is
        how #725 gets repeated with new words.

        Called the moment the release merge lands, and the moment matters. A
        squash release writes a NEW commit on the release branch instead of
        carrying the originals, so the branch it came from does not contain
        it, and the two diverge by one commit per release. Right here that
        commit holds EXACTLY the tree the integration branch already has and
        the merge base is fresh, so the merge is trivial by construction.
        Left to age it stops being trivial: on 26.08.2026 five releases'
        worth of them collided in ``hub/db.py`` and release PR #83 stood
        conflicted with 13 tasks undelivered.

        Asks GitHub to do the merge rather than driving the workspace clone.
        The clone is shared, may sit on someone else's branch with a dirty
        tree, and carries an armed pre-push hook — three ways for a
        bookkeeping merge to damage work in progress (#949 was one of them).
        The merges endpoint has no such surface: it answers 201 with the new
        commit, 204 when there is nothing to merge, 409 on a conflict.
        """
        if not gh_repo and not REPO_NAME:
            return ("unavailable", "не названо, в каком репозитории возвращать")
        rc, out, err = await _gh(
            "api",
            "--method",
            "POST",
            f"repos/{gh_repo or REPO_NAME}/merges",
            "-f",
            f"base={head}",
            "-f",
            f"head={base}",
            "-f",
            f"commit_message=chore: return {base} into {head} after the release",
            repo=repo,
            check=False,
        )
        if rc == 0:
            # 204 — «уже содержит», и gh печатает пустоту. Это ответ, а не
            # промах: возвращать нечего.
            body = (out or "").strip()
            if not body:
                return ("nothing", f"{head} уже содержит {base}")
            try:
                sha = str(json.loads(body).get("sha") or "").strip()
            except json.JSONDecodeError:
                return ("unavailable", f"ответ GitHub не разобран: {body[:150]}")
            if not sha:
                return ("unavailable", "GitHub не назвал коммит возврата")
            return ("returned", sha)

        detail = (err or "").strip() or "gh молчит"
        if "409" in detail or "conflict" in detail.lower():
            files = await self._conflicting_files(base, head, repo=repo)
            named = f": {', '.join(files)}" if files else ""
            return ("conflict", f"{base} не сливается с {head} без конфликта{named}")
        return ("unavailable", detail[:200])

    async def _conflicting_files(
        self, base: str, head: str, repo: str | None = None
    ) -> list[str]:
        """Which files a conflicting back-merge collides on, if git can say.

        Best effort by contract: GitHub's 409 names nothing, and the local
        clone may be absent, stale, or running a git too old for
        ``merge-tree --write-tree``. Empty means "could not name them", never
        "there were none" — the caller reports the conflict either way, since
        a conflict without a file list is still a conflict somebody must go
        and resolve.
        """
        if not repo:
            return []
        await _git(
            "fetch",
            "origin",
            f"+{base}:refs/remotes/origin/{base}",
            f"+{head}:refs/remotes/origin/{head}",
            repo=repo,
            check=False,
        )
        rc, out, _ = await _git(
            "merge-tree",
            "--write-tree",
            "--name-only",
            f"origin/{head}",
            f"origin/{base}",
            repo=repo,
            check=False,
        )
        # rc 1 is the conflicted case; the block after the tree oid lists the
        # paths. rc 0 means it merged cleanly here — the clone is behind what
        # GitHub just refused, so nothing is claimed.
        if rc != 1:
            return []
        lines = [ln.strip() for ln in (out or "").splitlines()[1:] if ln.strip()]
        return lines[:10]

    async def release_range(
        self,
        base: str,
        head: str,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> list[str]:
        """Commit subjects that ``head`` carries over ``base``, newest first.

        The subjects are how the release learns what it is carrying: the
        delivery gate writes «feat(task): …-(#NNN)», so the numbers can be
        read back out. Empty means nothing to release — or that git could not
        answer, and the caller treats those the same way it treats an empty
        range: by doing nothing.

        The subject is cut by JQ, not here (#963). Asking for the whole
        message and splitting the reply by lines counts LINES, not commits:
        jq prints a multi-line string as multiple output lines, so one commit
        with a body arrived as as many "commits" as it had lines. Release PR
        #40 listed 25 items — Co-authored-by among them — for a single commit,
        and #44 claimed four tasks for three: a squash message repeats the
        branch commit's subject, which also ends in «(#NNN)», so the number
        was counted twice.

        One output line = one commit is a contract, and it holds only while
        the cut happens in the query: commit boundaries cannot be recovered
        from concatenated text by anything but guessing.
        """
        rc, out, _ = await _gh(
            "api",
            f"repos/{gh_repo or REPO_NAME}/compare/{base}...{head}",
            "--jq",
            '.commits[].commit.message | split("\n")[0]',
            repo=repo,
            check=False,
        )
        if rc != 0 or not out:
            return []
        return [line.strip() for line in reversed(out.splitlines()) if line.strip()]

    async def undelivered_release_range(
        self,
        base: str,
        head: str,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> list[str] | None:
        """Subjects on ``head`` newer than the last commit whose tree matches ``base``.

        Three answers, like ``content_differs``. A list — possibly empty — is
        a successful cut: those subjects are what this release actually
        carries. ``None`` — the cut could not be asked, and the caller must
        not read that as "nothing to release" (#725).

        Lives next to ``release_range`` rather than inside it because that
        helper has a second consumer (``red_base._commits_between``) that
        still needs the full ``base...head`` interval (#972 AC-6).
        """
        workspace = repo or _repo_root()
        await _git(
            "fetch",
            "origin",
            f"+{base}:refs/remotes/origin/{base}",
            f"+{head}:refs/remotes/origin/{head}",
            repo=workspace,
            check=False,
        )
        base_sha = await _resolve_ref_remote_first(base, workspace)
        head_sha = await _resolve_ref_remote_first(head, workspace)
        if not base_sha or not head_sha:
            return None
        rc, tree_out, _ = await _git(
            "rev-parse", f"{base_sha}^{{tree}}", repo=workspace, check=False
        )
        if rc != 0 or not tree_out.strip():
            return None
        base_tree = tree_out.strip()
        rc, log_out, _ = await _git(
            "log",
            "--format=%H%x09%T%x09%s",
            f"{base_sha}..{head_sha}",
            repo=workspace,
            check=False,
        )
        if rc != 0:
            return None
        undelivered: list[str] = []
        for line in log_out.splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 3:
                continue
            _sha, tree, subject = parts
            if tree == base_tree:
                break
            if subject.strip():
                undelivered.append(subject.strip())
        return undelivered

    async def open_release_pr(
        self,
        base: str,
        head: str,
        title: str,
        body: str,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> int | None:
        """The open release PR for this range — found, updated, or created.

        Idempotent on purpose (#812 AC-5): two sessions can deliver within
        seconds of each other, and a second pull request over the same range
        would split one release into two stories about the same commits.
        """
        existing = await _find_pr_for_branch(head, repo, gh_repo=gh_repo)
        if existing:
            await _gh(
                "pr",
                "edit",
                str(existing),
                "--repo",
                gh_repo or REPO_NAME,
                "--title",
                title,
                "--body",
                body,
                repo=repo,
                check=False,
            )
            return existing
        rc, out, err = await _gh(
            "pr",
            "create",
            "--repo",
            gh_repo or REPO_NAME,
            "--base",
            base,
            "--head",
            head,
            "--title",
            title,
            "--body",
            body,
            repo=repo,
            check=False,
        )
        if rc != 0:
            log.warning("release PR not created: %s", (err or out or "").strip()[:200])
            return None
        return _parse_pr_number(out)

    async def pr_state(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> str:
        """Where this PR stands: "open", "merged", "closed", "absent", or "".

        Empty means the question could not be asked — no gh, no network, an
        unreadable answer. The caller treats that as a cause to report, never
        as an answer: "could not look" and "closed" lead to opposite decisions
        about delivery (#802, the rule #725 wrote down).

        "absent" is the fourth answer (#959), and it is an ANSWER: the number
        is not in the project's repository. Until it had a name it arrived as
        "" — the same value a blinking network produces — so the gate kept
        waiting for CI on a PR that cannot exist. That is not hypothetical: a
        project that moved between repositories carries numbers from the old
        one, and on #880 the gate offered to wait for a green CI forever.
        """
        # One field, and it is the whole answer: gh reports MERGED as a STATE,
        # and there is no `merged` flag to ask for. Asking for one made the
        # call fail outright ("Unknown JSON field"), so this method returned
        # "could not look" every single time and the gate it feeds quietly
        # fell back to the old behaviour (#803, found on the first live run).
        rc, out, _ = await _gh(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--json",
            "state",
            repo=repo,
            check=False,
        )
        if rc != 0 or not out:
            return await self._pr_absent_or_unknown(pr_number, repo, gh_repo)
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return ""
        return str(data.get("state") or "").lower()

    async def pr_is_draft(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> bool:
        """Whether GitHub still treats this PR as a draft (#1053).

        A Cloud Agent opens PRs as drafts by default. Hub ``create_pr`` never
        does. ``gh pr merge`` refuses a draft with the same boolean
        ``merge_pr`` already returns for a conflict or a revoked token, and
        that boolean used to send the task to ``needs_decision``. False here
        means "not a draft or could not look" — same #498 rule as the other
        readers: silence is not an accusation, and the merge call still runs.
        """
        rc, out, err = await _gh(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--json",
            "isDraft",
            repo=repo,
            check=False,
        )
        if rc != 0 or not (out or "").strip():
            log.info(
                "PR #%d draft probe unavailable: %s",
                pr_number,
                (err or "gh молчит").strip()[:200],
            )
            return False
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return False
        return bool(data.get("isDraft"))

    async def mark_pr_ready(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> bool:
        """Convert a draft PR to ready. Hub approval is the ready signal (#1053)."""
        rc, _, err = await _gh(
            "pr",
            "ready",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            repo=repo,
            check=False,
        )
        if rc == 0:
            log.info("Marked PR #%d ready", pr_number)
            return True
        log.warning(
            "Failed to mark PR #%d ready: %s",
            pr_number,
            (err or "").strip()[:200],
        )
        return False

    async def _pr_absent_or_unknown(
        self, pr_number: int, repo: str | None, gh_repo: str | None
    ) -> str:
        """Tell "no such PR here" from "could not ask", or "" (#959).

        Asked ONLY after ``gh pr view`` has already failed, so the working path
        pays nothing for it. Deliberately a second question rather than a
        different first one: the comment above remembers #803, where changing
        what pr_state asks broke the reading of MERGED for every PR. The cheap
        answer keeps its transport; only the failure gets a follow-up.

        The signal is the REST status in the response body, not a substring of
        gh's prose — error text drifts between versions, a 404 does not. A
        missing repository answers 404 too, and that is correct: "not reachable
        as this project's PR" is the same decision either way.
        """
        rc, out, _ = await _gh(
            "api",
            f"repos/{gh_repo or REPO_NAME}/pulls/{pr_number}",
            repo=repo,
            check=False,
        )
        if rc == 0:
            # The REST call answered where the GraphQL one did not. Nothing is
            # claimed from that: the state is read by the caller's next pass.
            return ""
        try:
            body = json.loads(out or "{}")
        except json.JSONDecodeError:
            return ""
        return "absent" if str(body.get("status") or "") == "404" else ""

    async def merge_commit_sha(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> str:
        """The SHA of the commit THIS pull request produced, or "" (#534).

        Not the tip of the base branch. The tip is whatever landed last, and
        between the merge and the read a direct push can land — which would
        write the intruder into the whitelist and mark the real merge as
        drift. The pull request knows its own merge commit, so ask it.
        """
        rc, out, err = await _gh(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--json",
            "mergeCommit",
            repo=repo,
            check=False,
        )
        if rc != 0 or not (out or "").strip():
            log.warning(
                "could not read the merge commit of PR #%d: %s",
                pr_number,
                (err or "").strip(),
            )
            return ""
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            log.warning("PR #%d returned no readable merge commit", pr_number)
            return ""
        return str((data.get("mergeCommit") or {}).get("oid") or "").strip()

    async def merge_pr(
        self,
        pr_number: int,
        task_id: int,
        title: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        delete_branch: bool = True,
    ) -> bool:
        """Merge one PR; ``delete_branch`` says what happens to its head (#949).

        Deleting the head is right for a task PR — short-lived branches, the
        repository's own rule. But this one call served the RELEASE PR too,
        whose head is the project's integration branch: every auto-release of
        24–25.08 deleted develop, three times in two days, and the repo's
        delete_branch_on_merge=false proves it was us, not GitHub. The default
        stays True so the task path is untouched; the release path passes
        False, because a release must not remove the branch work lands on.
        """
        ctype = _conv_commit_type(title)
        slug = _slugify(title, max_len=60)
        subject = f"{ctype}(task): {slug} (#{task_id})"

        args = [
            "pr",
            "merge",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--squash",
            "--admin",
        ]
        if delete_branch:
            args.append("--delete-branch")
        args += ["--subject", subject]
        rc, _, err = await _gh(
            *args,
            repo=repo,
            check=False,
            timeout=30,
        )
        if rc == 0:
            log.info("Merged PR #%d (squash, admin)", pr_number)
            return True
        log.error("Failed to merge PR #%d: %s", pr_number, err)
        return False

    async def delete_branch(
        self,
        branch: str,
        repo: str | None = None,
        base_branch: str | None = None,
    ) -> bool:
        """Remove a merged task branch. Returns whether it is actually gone.

        Two fixes (#552). The checkout named "main" by literal, so once #362
        moved task branches onto PAIR_BASE_BRANCH this could not step off the
        very branch it was asked to delete — git then refused the delete and
        the branches piled up. And the success line was logged unconditionally,
        so the log claimed a deletion that had not happened.
        """
        repo = repo or _repo_root()
        base = _resolve_base(base_branch)
        rc, _, err = await _git("checkout", base, repo=repo, check=False)
        if rc != 0:
            log.error(
                "delete_branch: cannot leave %s for %r in %s: %s",
                branch,
                base,
                repo,
                (err or "").strip() or "git checkout failed",
            )
            return False
        rc, _, err = await _git("branch", "-D", branch, repo=repo, check=False)
        if rc != 0:
            log.error(
                "delete_branch: %s not deleted: %s",
                branch,
                (err or "").strip() or "git branch -D failed",
            )
            return False
        log.info("Deleted local branch %s", branch)
        return True

    async def clone_repo(
        self, repo_url: str, workspace_path: str, base_branch: str | None = None
    ) -> tuple[bool, str]:
        """Provision a project workspace (#347, #377). Returns (ok, detail).

        Short ``owner/repo`` form tries https first — public repos clone
        anonymously with zero server setup — then falls back to ssh with
        the deploy key; the detail keeps every failed attempt so a private
        repo without a key reads as a diagnosis, not a stacktrace.
        Idempotent: an existing clone is verified against the expected
        origin (owner/repo) and fetched instead of re-cloned — the fetch
        itself validates access, no ls-remote needed.
        """
        base_branch = _resolve_base(base_branch)
        import os

        git_dir = os.path.join(workspace_path, ".git")
        if os.path.isdir(git_dir):
            rc, origin, err = await _run(
                "git",
                "-C",
                workspace_path,
                "remote",
                "get-url",
                "origin",
                check=False,
            )
            slug = repo_url.removesuffix(".git").lower()
            if rc != 0 or slug.split(":")[-1] not in origin.lower():
                return False, (
                    f"existing workspace origin mismatch: {origin or err} "
                    f"(expected {repo_url})"
                )
            rc, _, err = await _run(
                "git",
                "-C",
                workspace_path,
                "fetch",
                "origin",
                timeout=300,
                check=False,
            )
            if rc != 0:
                return False, f"fetch failed: {err[:300]}"
            # Every workspace on production already exists, so this is the
            # only path that actually runs there: arming solely after a fresh
            # clone would have changed nothing at all (#532 review).
            git_policy.activate_quietly(workspace_path, base_branch=base_branch)
            log.info("clone_repo: verified existing clone at %s", workspace_path)
            return True, "existing clone verified, origin fetched"

        if "://" in repo_url or repo_url.startswith("git@"):
            candidates = [repo_url]
        else:
            # #377: public repos need no credentials over https; ssh with
            # the deploy key is the private-repo fallback.
            candidates = [
                f"https://github.com/{repo_url}.git",
                f"git@github.com:{repo_url}.git",
            ]

        url = None
        failures: list[str] = []
        for candidate in candidates:
            rc, _, err = await _run(
                "git",
                "ls-remote",
                "--heads",
                candidate,
                base_branch,
                timeout=60,
                check=False,
            )
            if rc == 0:
                url = candidate
                break
            failures.append(f"{candidate}: {err[:150] or 'ls-remote failed'}")
        if url is None:
            return False, "remote not accessible: " + "; ".join(failures)

        os.makedirs(os.path.dirname(workspace_path) or "/", exist_ok=True)
        rc, _, err = await _run(
            "git",
            "clone",
            "--branch",
            base_branch,
            url,
            workspace_path,
            timeout=600,
            check=False,
        )
        if rc != 0:
            return False, f"clone failed ({url}): {err[:300]}"
        # Arm the hook while the hub has the clone in its hands. A setup step
        # a person must remember is the same failure as the hook nobody
        # activated — this is the one moment where nobody has to remember
        # (#532). Best effort: a hook that cannot be armed never fails a clone.
        git_policy.activate_quietly(workspace_path, base_branch=base_branch)

        transport = "https" if url.startswith("https") else "ssh"
        log.info(
            "clone_repo: cloned %s → %s (%s, %s)",
            repo_url,
            workspace_path,
            base_branch,
            transport,
        )
        return True, f"cloned {repo_url} ({base_branch}, {transport})"
