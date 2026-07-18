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
from typing import Any

from hub.config import GH_BIN, PAIR_BASE_BRANCH, REPO_NAME, WORKSPACE_REPO_LINK
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
from hub.mcp_envelope import enrich_error_payload

log = logging.getLogger(__name__)


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
    ) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason
        self.hint = hint
        self.workspace_path = workspace_path
        self.hostname = hostname
        self.suggested_tool = suggested_tool

    def to_detail(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "reason": self.reason,
            "message": self.message,
        }
        if self.hint:
            payload["hint"] = self.hint
        if self.workspace_path:
            payload["workspace_path"] = self.workspace_path
        if self.hostname:
            payload["hostname"] = self.hostname
        if self.suggested_tool:
            payload["suggested_tool"] = self.suggested_tool
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


def _pair_branch_conflict(
    message: str,
    *,
    repo: str,
    reason: str = "pair_branch_conflict",
    hint: str | None = None,
    suggested_tool: str | None = "hub_pair_start",
    current_branch: str | None = None,
    task_id: int | None = None,
) -> PairBranchConflictError:
    host = _hostname()
    if hint is None and current_branch and task_id is not None:
        hint = (
            f"SSH to {host}, then: cd {repo} && git checkout develop "
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
    )


async def _branch_is_fully_pushed(branch: str, repo: str) -> bool:
    """True when every local commit on HEAD is reachable from origin/branch."""
    rc, _, _ = await _git(
        "rev-parse",
        "--verify",
        f"origin/{branch}^{{commit}}",
        repo=repo,
        check=False,
    )
    if rc != 0:
        return False
    rc, count, _ = await _git(
        "rev-list",
        "--count",
        f"origin/{branch}..HEAD",
        repo=repo,
        check=False,
    )
    if rc != 0:
        return False
    try:
        return int(count.strip() or "0") == 0
    except ValueError:
        return False


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
        "set OPENCLAW_WORKSPACE_REPO to a git clone or use a project "
        "with workspace_path"
    )


def _slugify(title: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()
    return slug[:max_len].rstrip("-")


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
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
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
    """Resolve a branch name to a commit sha, falling back to origin/<name>."""
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


def _parse_pr_number(gh_output: str) -> int | None:
    m = re.search(r"/pull/(\d+)", gh_output)
    return int(m.group(1)) if m else None


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
        base_branch: str = "develop",
        repo: str | None = None,
    ) -> bool:
        """True when ``branch`` carries commits unique to ``other_branch`` (#438).

        Merge-base analysis against ``base_branch``: ``other_branch`` owns the
        commits reachable from it but not from base; if ``branch`` contains
        any of them, the branches are stacked and ``branch`` cannot be
        verified against base independently. Best-effort: unresolvable refs
        or any git failure return False (advisory check, never an error).
        """
        if repo is None:
            reason = await _default_workspace_error()
            if reason:
                return False
        repo = repo or _repo_root()
        head = await _resolve_ref(branch, repo)
        other = await _resolve_ref(other_branch, repo)
        base = await _resolve_ref(base_branch, repo)
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
        base = base_branch or "main"
        slug = _slugify(title)
        branch = f"task-{task_id}/{slug}"

        await _git("checkout", base, repo=repo, check=False)

        rc, dirty, _ = await _git("status", "--porcelain", repo=repo, check=False)
        if dirty.strip():
            log.warning("create_branch: dirty worktree on main, cleaning up")
            await _git("checkout", ".", repo=repo, check=False)
            await _git("clean", "-fd", repo=repo, check=False)

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
    ) -> str:
        """Safe branch setup for pair mode: never git-clean a dirty worktree."""
        if repo is None:
            reason = await _default_workspace_error()
            if reason:
                raise PairBranchConflictError(reason)  # readable 422 (#378)
        repo = repo or _repo_root()
        base = base_branch or PAIR_BASE_BRANCH

        rc, dirty, _ = await _git("status", "--porcelain", repo=repo, check=False)
        if dirty.strip():
            raise _pair_branch_conflict(
                "Uncommitted changes in workspace; commit or stash before pair-start",
                repo=repo,
                reason="pair_branch_dirty",
                hint=(
                    f"Commit or stash uncommitted changes in {repo} "
                    f"(host {_hostname()}), then retry hub_pair_start."
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
            if not await _branch_is_fully_pushed(current, repo):
                raise _pair_branch_conflict(
                    f"Branch {current!r} has unpushed commits; "
                    f"push before pair-start for #{task_id}",
                    repo=repo,
                    reason="pair_branch_unpushed",
                    hint=(
                        f"On {_hostname()}: cd {repo} && "
                        f"git push -u origin {current}, then retry "
                        f"hub_pair_start for #{task_id}."
                    ),
                    current_branch=current,
                    task_id=task_id,
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
                )

        slug = (branch_slug or "").strip() or _slugify(title)
        branch = f"task-{task_id}/{slug}"

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
        base = base_branch or PAIR_BASE_BRANCH

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
        base = base_branch or PAIR_BASE_BRANCH

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

    def worktree_path(self, task_id: int, repo: str | None = None) -> str:
        """Deterministic worktree path for a task (#459); where its branch lives."""
        return _worktree_path(task_id, repo or _repo_root())

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
        base = base_branch or PAIR_BASE_BRANCH
        slug = (branch_slug or "").strip() or _slugify(title)
        branch = f"task-{task_id}/{slug}"
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
            rc, dirty, _ = await _git(
                "status", "--porcelain", repo=wt_path, check=False
            )
            if dirty.strip():
                raise _pair_branch_conflict(
                    f"Worktree {wt_path} is on {cur!r} with uncommitted changes; "
                    f"refusing to switch it to {branch!r}",
                    repo=wt_path,
                    reason="pair_worktree_dirty",
                    hint=(
                        f"Commit or stash changes in {wt_path} (host {_hostname()}), "
                        f"then retry pair-start for #{task_id}."
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

    async def auto_commit(
        self,
        task_id: int,
        title: str = "",
        message: str | None = None,
        repo: str | None = None,
    ) -> bool:
        repo = repo or _repo_root()

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

    async def pull_main(self, repo: str | None = None) -> bool:
        repo = repo or _repo_root()
        await _git("checkout", "main", repo=repo, check=False)
        rc, _, _ = await _git(
            "pull", "origin", "main", "--ff-only", repo=repo, check=False
        )
        return rc == 0

    async def squash_branch(
        self, task_id: int, title: str, branch: str, repo: str | None = None
    ) -> bool:
        repo = repo or _repo_root()

        await _git("checkout", branch, repo=repo, check=False)
        await _git("fetch", "origin", "main", repo=repo, check=False)

        rc, merge_base, _ = await _git(
            "merge-base",
            "origin/main",
            branch,
            repo=repo,
            check=False,
        )
        if rc != 0 or not merge_base.strip():
            log.warning("squash_branch: cannot find merge-base for %s", branch)
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
            "---\n*Created automatically by OpenClaw Hub*"
        )

        rc, out, err = await _gh(
            "pr",
            "create",
            "--repo",
            gh_repo or REPO_NAME,
            "--base",
            base_branch or PAIR_BASE_BRANCH,
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
        # rc error / empty output — the probe itself could not run. Not pending:
        # nothing is known to be in flight (#419).
        if rc != 0 or not out or not out.strip():
            return CIProbeResult(
                CIProbeOutcome.unavailable, "gh_error", details=(err or "").strip()
            )
        try:
            checks = json.loads(out)
        except json.JSONDecodeError:
            return CIProbeResult(CIProbeOutcome.unavailable, "invalid_json")

        # An empty check set is a definite answer, not a wait: the PR has no CI.
        if not checks:
            return CIProbeResult(CIProbeOutcome.absent, "no_checks")

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

    async def merge_pr(
        self,
        pr_number: int,
        task_id: int,
        title: str,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> bool:
        ctype = _conv_commit_type(title)
        slug = _slugify(title, max_len=60)
        subject = f"{ctype}(task): {slug} (#{task_id})"

        rc, _, err = await _gh(
            "pr",
            "merge",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--squash",
            "--admin",
            "--delete-branch",
            "--subject",
            subject,
            repo=repo,
            check=False,
            timeout=30,
        )
        if rc == 0:
            log.info("Merged PR #%d (squash, admin)", pr_number)
            return True
        log.error("Failed to merge PR #%d: %s", pr_number, err)
        return False

    async def delete_branch(self, branch: str, repo: str | None = None) -> None:
        await _git("checkout", "main", repo=repo, check=False)
        await _git("branch", "-D", branch, repo=repo, check=False)
        log.info("Deleted local branch %s", branch)

    async def clone_repo(
        self, repo_url: str, workspace_path: str, base_branch: str = "develop"
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
        transport = "https" if url.startswith("https") else "ssh"
        log.info(
            "clone_repo: cloned %s → %s (%s, %s)",
            repo_url,
            workspace_path,
            base_branch,
            transport,
        )
        return True, f"cloned {repo_url} ({base_branch}, {transport})"
