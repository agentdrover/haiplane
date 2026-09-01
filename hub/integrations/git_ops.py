"""Git operations for the Hub branching workflow, plus the forge they pair with.

Local git operations run against the workspace repo (config.WORKSPACE_REPO_LINK).
Everything that is asked of the REPOSITORY HOST rather than of git — pull
requests, CI outcomes, merges — goes through ``protocols.ForgePlugin`` (#1113).
Until then those nineteen methods called ``gh`` here directly, and "which
forge" was spread across nineteen call sites with no place to plug a second
one in.
"""

from __future__ import annotations

import logging
import os
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from hub.actionable_errors import (
    pair_branch_dirty_detail,
    pair_worktree_dirty_detail,
)
from hub.config import PAIR_BASE_BRANCH
from hub.integrations import forge as forge_urls
from hub.integrations import proc
from hub.integrations.forge.github import GitHubForge
from hub.integrations.protocols import (
    CIProbeResult,
    ForgePlugin,
    MergeabilityOutcome,
)
from hub.mcp_envelope import enrich_error_payload
from hub.models import DEFAULT_FORGE

from hub import git_policy
from hub.commit_scope import parse_porcelain_paths

log = logging.getLogger(__name__)

# Exit code for a killed-on-timeout command: the shell convention, and distinct
# from any rc git itself returns, so a caller can tell a timeout from a refusal.
_TIMEOUT_RC = proc.TIMEOUT_RC


#: Причина, когда git отказал, но словами, которых мы не знаем (#1118, AC-3).
#: Не «неизвестная ошибка»: отказ ЕСТЬ и его текст сохранён рядом — неизвестно
#: только имя, под которым его учитывать.
CLONE_CAUSE_UNNAMED = "cause_unnamed"

#: Текст git → имя причины, от частного к общему (#1118, AC-3).
#:
#: Порядок значим: «Permission denied (publickey)» содержит и «denied», и
#: «permission», и общее правило, поставленное выше частного, съело бы разницу
#: между «ключа нет» и «ключ есть, прав нет» — а это разные руки и разные
#: действия. Ровно та же ошибка, что чинила #419 для исходов CI-пробы.
_CLONE_CAUSES: tuple[tuple[str, str], ...] = (
    ("host key verification failed", "host_key_unpinned"),
    ("permission denied (publickey", "no_deploy_key"),
    ("could not read username", "no_git_credentials"),
    ("could not read password", "no_git_credentials"),
    ("authentication failed", "bad_git_credentials"),
    ("invalid username or password", "bad_git_credentials"),
    ("repository not found", "repo_not_found_or_no_access"),
    ("does not appear to be a git repository", "repo_not_found_or_no_access"),
    ("not found", "repo_not_found_or_no_access"),
    ("access denied", "access_denied"),
    ("could not resolve host", "host_unreachable"),
    ("connection timed out", "host_unreachable"),
    ("connection refused", "host_unreachable"),
)


def _clone_cause(err: str) -> str:
    """Имя причины отказа git, или "" если ни одно правило не подошло."""
    low = (err or "").lower()
    for needle, cause in _CLONE_CAUSES:
        if needle in low:
            return cause
    return ""


def _origin_host(url: str) -> str:
    """Хост из git-адреса в любой из трёх форм, или "" если хоста в нём нет.

    Разбирается ``https://host/owner/repo.git``, scp-подобная
    ``git@host:owner/repo.git`` и ``ssh://git@host/owner/repo.git``.
    Локальный путь хоста не несёт вовсе и честно даёт "" — вызывающий обязан
    прочесть это как «не знаю», а не как «чужой».
    """
    url = (url or "").strip()
    if not url:
        return ""
    if "://" in url:
        rest = url.split("://", 1)[1]
        authority = rest.split("/", 1)[0]
    elif ":" in url:
        head = url.split(":", 1)[0]
        if os.sep in head or head.startswith("."):
            return ""
        authority = head
    else:
        return ""
    return authority.rsplit("@", 1)[-1].split("?", 1)[0].lower()


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


# Тонкие обёртки над общим слоем запуска процессов (#1113). Именно обёртки, а
# не присваивание `_run = proc.run`: присваивание захватывает объект функции, и
# тогда подмена `proc.run` этот модуль уже не затрагивает — сема ломается
# ровно там, где её и проверяют. Обёртка ищет `proc.run` в момент вызова, так
# что работают обе точки подмены.


def _repo_root() -> str:
    return proc.repo_root()


def _git_env() -> dict[str, str]:
    return proc.git_env()


async def _run(*cmd: str, **kw) -> tuple[int, str, str]:
    return await proc.run(*cmd, **kw)


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


#: Префикс детали, по которому вызывающий узнаёт: слить УДАЛОСЬ, а
#: подтвердить — нет. Это состояние лечится повтором, а не человеком.
MERGE_UNCONFIRMED = "merge_unconfirmed"


def _scratch_worktree(workspace: str, kind: str, pr_number: int) -> str:
    """Путь одноразового рабочего дерева, уникальный по КЛОНУ и номеру PR."""
    parent = os.path.dirname(workspace.rstrip("/")) or "/"
    clone = os.path.basename(workspace.rstrip("/")) or "repo"
    return os.path.join(parent, f".hub-{kind}-{clone}-{pr_number}")


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


async def _git(*args: str, repo: str | None = None, **kw) -> tuple[int, str, str]:
    repo = repo or _repo_root()
    return await _run("git", "-C", repo, *args, cwd=repo, **kw)


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


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class GitOpsIntegration:
    """Concrete git_ops plugin: local git here, the repository host behind a forge.

    ``forge`` is an attribute rather than a module-level singleton so a
    project on another host can be given its own adapter without touching a
    single call site (#1112). Today every project is on GitHub, so the
    default is the GitHub adapter and nothing observable changes.
    """

    def __init__(self, forge: ForgePlugin | None = None) -> None:
        self.forge: ForgePlugin = forge or GitHubForge()

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

        #1055: the names used to be interpolated verbatim, and on a pair task
        that is a ref the workspace does not have. The branch is written on a
        developer's machine and arrives as ``origin/<branch>`` only — no local
        head is ever created — so ``git diff develop...task-1042/queue`` came
        back "unknown revision", rc was not 0, and every caller lost its
        subject while saying, honestly and uselessly, that it could not look:
        the brief dropped call sites and diff volume, and the review dispatcher
        sized its profile against nothing. Verified on production 29.08 in
        the hub's own workspace clone: the commit was there and so was
        origin/<branch>; the local head was not, and never is.

        Both ends go through ``_resolve_ref_remote_first`` — the resolver
        ``branch_diff_paths`` has used since #762, not a fourth chain of its
        own — and a ref that resolves nowhere is fetched once before the answer
        becomes None. A sha passed instead of a branch name still resolves:
        ``origin/<sha>`` misses, the bare sha hits.
        """
        base_sha = await _resolve_ref_remote_first(base, repo)
        branch_sha = await _resolve_ref_remote_first(branch, repo)
        if base_sha is None or branch_sha is None:
            # Best effort, exactly as in branch_diff_paths: a workspace that
            # cannot reach origin still has refs, and a network blip must not
            # turn a readable diff into an unreadable one.
            await _git("fetch", "origin", base, branch, repo=repo, check=False)
            base_sha = base_sha or await _resolve_ref_remote_first(base, repo)
            branch_sha = branch_sha or await _resolve_ref_remote_first(branch, repo)
        if base_sha is None or branch_sha is None:
            log.warning(
                "branch_diff: %r not found in %s",
                base if base_sha is None else branch,
                repo,
            )
            return None
        rc, out, _ = await _git(
            "diff",
            "-U0",
            f"{base_sha}...{branch_sha}",
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
        return await self.forge.create_pr(
            pr_title,
            body,
            branch,
            _resolve_base(base_branch),
            repo=repo,
            gh_repo=gh_repo,
        )

    async def get_ci_failure_logs(
        self,
        pr_number: int,
        branch: str,
        max_log_chars: int = 12000,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> dict[str, Any]:
        return await self.forge.ci_failure_logs(
            pr_number, branch, max_log_chars, repo=repo, gh_repo=gh_repo
        )

    async def branch_ci_runs(
        self,
        branch: str,
        limit: int = 20,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> list[dict[str, Any]] | None:
        return await self.forge.branch_ci_runs(
            branch, limit, repo=repo, gh_repo=gh_repo
        )

    async def check_pr_ci(
        self, pr_number: int, repo: str | None = None, gh_repo: str | None = None
    ) -> CIProbeResult:
        return await self.forge.check_pr_ci(pr_number, repo=repo, gh_repo=gh_repo)

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
        return await self.forge.pr_for_branch(branch, repo=repo, gh_repo=gh_repo)

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

        The forge answers the outcome; the FILES of a conflict come from the
        clone, because only git can name them. The 409 a forge returns names
        nothing, and a reason has to lead somewhere — #969 already reads them
        out of the clone, so this is the same question with the same answer,
        not a second one.
        """
        outcome, detail = await self.forge.pr_mergeability(
            pr_number, repo=repo, gh_repo=gh_repo
        )
        # Форж, который не умеет мержить, обычно не умеет и предсказывать мерж
        # (#1116). Тогда вопрос задаётся git: пробное слияние в одноразовом
        # рабочем дереве — единственный способ узнать ответ, не тронув ничего.
        if outcome is MergeabilityOutcome.unavailable and not (
            self.forge.can_merge_via_api
        ):
            return await self._mergeable_by_trial(pr_number, repo=repo, gh_repo=gh_repo)
        if outcome is not MergeabilityOutcome.conflicting:
            return (outcome, detail)
        files = await self._conflicting_files_of_pr(
            pr_number, repo=repo, gh_repo=gh_repo
        )
        named = f": {', '.join(files)}" if files else ""
        return (MergeabilityOutcome.conflicting, f"конфликт с базовой веткой{named}")

    async def _mergeable_by_trial(
        self,
        pr_number: int,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> tuple[MergeabilityOutcome, str]:
        """Сольётся ли PR — по пробному мержу, который ничего не меняет (#1116).

        Исходы те же четыре, что и у форжа, и различие между ними сохраняется
        целиком (#970). ``unknown`` здесь не бывает: git отвечает определённо,
        и притворяться, что «ещё не посчитал», было бы враньём. А вот
        ``unavailable`` бывает часто — нет клона, не дотянулись до origin, не
        разрешились ветки, — и это НЕ конфликт: одно чинится сетью, другое
        руками человека.
        """
        workspace = repo or _repo_root()
        base, head = await self.forge.pr_refs(pr_number, repo=repo, gh_repo=gh_repo)
        if not base or not head:
            return (
                MergeabilityOutcome.unavailable,
                f"PR #{pr_number}: форж не назвал базовую и головную ветки",
            )
        rc, _, err = await _git(
            "fetch",
            "origin",
            f"+{base}:refs/remotes/origin/{base}",
            f"+{head}:refs/remotes/origin/{head}",
            repo=workspace,
            check=False,
        )
        if rc != 0:
            return (
                MergeabilityOutcome.unavailable,
                f"не удалось получить ветки из origin: {err[:150]}",
            )
        path = _scratch_worktree(workspace, "trial", pr_number)
        await _git("worktree", "remove", "--force", path, repo=workspace, check=False)
        rc, _, err = await _git(
            "worktree",
            "add",
            "--force",
            "--detach",
            path,
            f"origin/{base}",
            repo=workspace,
            check=False,
        )
        if rc != 0:
            return (
                MergeabilityOutcome.unavailable,
                f"не удалось подготовить дерево для пробы: {err[:150]}",
            )
        try:
            rc, _, _ = await _git(
                "merge",
                "--no-commit",
                "--no-ff",
                f"origin/{head}",
                repo=path,
                check=False,
            )
            await _git("merge", "--abort", repo=path, check=False)
        finally:
            await _git(
                "worktree", "remove", "--force", path, repo=workspace, check=False
            )
        if rc == 0:
            return (MergeabilityOutcome.mergeable, "пробное слияние прошло")
        if rc == _TIMEOUT_RC or rc >= 128:
            # Таймаут и падение самого git — это «спросить не удалось», а не
            # конфликт: первое лечится повтором, второе руками человека.
            # Схлопывать их в conflicting значит поднимать ложную тревогу
            # ровно того рода, который разбирал #970.
            return (
                MergeabilityOutcome.unavailable,
                f"пробное слияние не состоялось (git rc={rc})",
            )
        files = await self._conflicting_files(head, base, repo=workspace)
        named = f": {', '.join(files)}" if files else ""
        return (
            MergeabilityOutcome.conflicting,
            f"конфликт с базовой веткой{named}",
        )

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
        base, head = await self.forge.pr_refs(pr_number, repo=repo, gh_repo=gh_repo)
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

        The merge itself is the forge's job — the clone is shared, may sit on
        someone else's branch with a dirty tree, and carries an armed pre-push
        hook (#949). Naming the conflicting files is git's job, and stays here.
        """
        state, detail = await self.forge.merge_branches(
            head,
            base,
            f"chore: return {base} into {head} after the release",
            repo=repo,
            gh_repo=gh_repo,
        )
        if state != "conflict":
            return (state, detail)
        files = await self._conflicting_files(base, head, repo=repo)
        named = f": {', '.join(files)}" if files else ""
        return ("conflict", f"{base} не сливается с {head} без конфликта{named}")

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
        """
        return await self.forge.compare_subjects(base, head, repo=repo, gh_repo=gh_repo)

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
        """The open release PR for this range — found, updated, or created."""
        return await self.forge.open_or_update_pr(
            base, head, title, body, repo=repo, gh_repo=gh_repo
        )

    async def pr_state(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> str:
        """Where this PR stands: "open", "merged", "closed", "absent", or "".

        Empty means the question could not be asked — no forge, no network, an
        unreadable answer. The caller treats that as a cause to report, never
        as an answer: "could not look" and "closed" lead to opposite decisions
        about delivery (#802, the rule #725 wrote down).
        """
        return await self.forge.pr_state(pr_number, repo=repo, gh_repo=gh_repo)

    async def pr_is_draft(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> bool:
        """Whether the forge still treats this PR as a draft (#1053).

        False means "not a draft or could not look" — the #498 rule: silence
        is not an accusation, and the merge call still runs.
        """
        return await self.forge.pr_is_draft(pr_number, repo=repo, gh_repo=gh_repo)

    async def mark_pr_ready(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> bool:
        """Convert a draft PR to ready. Hub approval is the ready signal (#1053)."""
        return await self.forge.mark_pr_ready(pr_number, repo=repo, gh_repo=gh_repo)

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
        return await self.forge.merge_commit_sha(pr_number, repo=repo, gh_repo=gh_repo)

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

        Ветвится по ОБЪЯВЛЕННОЙ способности форжа, а не по попытке (#1116).
        GitHub сливает сам одним вызовом. У GitVerse такого вызова нет вовсе,
        и слить можно только локальным git — а git живёт здесь, не в адаптере.

        Deleting the head is right for a task PR — short-lived branches, the
        repository's own rule. But this one call served the RELEASE PR too,
        whose head is the project's integration branch: every auto-release of
        24–25.08 deleted develop, three times in two days. The default stays
        True so the task path is untouched; the release path passes False,
        because a release must not remove the branch work lands on.
        """
        ok, _detail = await self.merge_pr_with_detail(
            pr_number,
            task_id,
            title,
            repo=repo,
            gh_repo=gh_repo,
            delete_branch=delete_branch,
        )
        return ok

    async def merge_pr_with_detail(
        self,
        pr_number: int,
        task_id: int,
        title: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        delete_branch: bool = True,
    ) -> tuple[bool, str]:
        """Слить PR и НАЗВАТЬ причину, если не вышло (#1116, по ревью).

        Пустая деталь при неудаче означает «форж отказал и причины не дал» —
        путь GitHub, где её и раньше не было. Непустая приходит с пути
        мержа пушем, и вызывающий обязан её различать: «не смогли слить» и
        «слили, но не подтвердили» ведут к противоположным действиям.
        """
        ctype = _conv_commit_type(title)
        slug = _slugify(title, max_len=60)
        subject = f"{ctype}(task): {slug} (#{task_id})"
        if self.forge.can_merge_via_api:
            ok = await self.forge.merge_pr(
                pr_number,
                subject,
                delete_branch=delete_branch,
                repo=repo,
                gh_repo=gh_repo,
            )
            return (ok, "")
        ok, detail = await self.merge_pr_by_push(
            pr_number, subject, repo=repo, gh_repo=gh_repo
        )
        if not ok:
            log.error("merge by push failed for PR #%d: %s", pr_number, detail)
        return (ok, detail)

    async def merge_pr_by_push(
        self,
        pr_number: int,
        subject: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> tuple[bool, str]:
        """Слить PR локальным git и ДОКАЗАТЬ доставку базовой веткой (#1116).

        Для форжей, которые не умеют мержить сами. Возвращает ``(ok, detail)``
        — деталь называет ПРИЧИНУ, а не актора: защита базовой ветки, конфликт,
        отсутствие клона и недоступная сеть чинятся разными руками, и «форж
        отказал» не ведёт никуда (#970).

        Порядок шагов не произволен, каждый закрывает свой способ соврать:

        1. Ветки берутся у PR, а не у вызывающего: имя ветки задачи могло
           разойтись с тем, что в PR на самом деле.
        2. Мерж делается в ОДНОРАЗОВОМ worktree, а не в рабочем клоне. Клон
           общий, может стоять на чужой ветке с грязным деревом и несёт
           взведённый pre-push хук — три способа повредить чужую работу
           бухгалтерским мержем (#949 был одним из них).
        3. Доставка подтверждается достижимостью полученного SHA в базовой
           ветке НА REMOTE, а не кодом возврата push. Измерено 01.09.2026:
           GitVerse после такого мержа оставляет PR в состоянии open,
           merged=False, а GET /pulls/{n}/merge отвечает 404 и до, и после —
           то есть форж об успехе не сообщает вовсе.
        4. Только после подтверждения PR закрывается явно. Незакрытый висел бы
           открытым вечно, и pr_for_branch находил бы его на уже доставленной
           ветке.
        """
        workspace = repo or _repo_root()
        base, head = await self.forge.pr_refs(pr_number, repo=repo, gh_repo=gh_repo)
        if not base or not head:
            return (False, f"PR #{pr_number}: форж не назвал базовую и головную ветки")

        rc, _, err = await _git(
            "fetch",
            "origin",
            f"+{base}:refs/remotes/origin/{base}",
            f"+{head}:refs/remotes/origin/{head}",
            repo=workspace,
            check=False,
        )
        if rc != 0:
            return (False, f"не удалось получить ветки из origin: {err[:150]}")

        # Имя включает клон, а не только номер PR: два проекта на одном
        # форже легко имеют PR №1 каждый, и общий путь свёл бы их мержи в
        # одно дерево.
        path = _scratch_worktree(workspace, "merge", pr_number)
        await _git("worktree", "remove", "--force", path, repo=workspace, check=False)
        rc, _, err = await _git(
            "worktree",
            "add",
            "--force",
            "-B",
            f"hub-merge/{pr_number}",
            path,
            f"origin/{base}",
            repo=workspace,
            check=False,
        )
        if rc != 0:
            return (
                False,
                f"не удалось подготовить рабочее дерево для мержа: {err[:150]}",
            )

        try:
            rc, _, err = await _git(
                "merge",
                "--no-ff",
                "-m",
                subject,
                f"origin/{head}",
                repo=path,
                check=False,
            )
            if rc != 0:
                files = await self._conflicting_files(head, base, repo=workspace)
                named = f": {', '.join(files)}" if files else ""
                return (False, f"{head} не сливается с {base} без конфликта{named}")

            rc, merged_sha, _ = await _git("rev-parse", "HEAD", repo=path, check=False)
            merged_sha = (merged_sha or "").strip()
            if rc != 0 or not merged_sha:
                return (False, "git не назвал коммит мержа")

            rc, _, err = await _git(
                "push", "origin", f"HEAD:refs/heads/{base}", repo=path, check=False
            )
            if rc != 0:
                detail = (err or "").strip()
                low = detail.lower()
                # Отказ ДОСТУПА проверяется первым и отдельно: «Permission
                # denied (publickey)» содержит слово denied и попадал в ветку
                # про защиту базы — то есть отказ называл причину, которой
                # нет, и человек шёл снимать защиту вместо починки ключа.
                if "publickey" in low or "authentication failed" in low:
                    return (
                        False,
                        f"push в {base} отвергнут по доступу — ключ или токен: "
                        f"{detail[:150]}",
                    )
                if "protected" in low or "pre-receive" in low or "denied" in low:
                    return (
                        False,
                        f"базовая ветка {base} закрыта от прямого push — "
                        f"на этом форже доставка иначе невозможна: {detail[:150]}",
                    )
                return (False, f"push в {base} не прошёл: {detail[:150]}")
        finally:
            await _git(
                "worktree", "remove", "--force", path, repo=workspace, check=False
            )

        landed = await self.forge.branch_contains(
            base, merged_sha, repo=repo, gh_repo=gh_repo
        )
        if landed is None:
            # Машинная метка, а не только слова: вызывающий классифицирует по
            # префиксу, и без него «спросите снова» читалось человеком, но не
            # гейтом — и работа уходила в needs_decision уже лёжа в базе.
            return (
                False,
                f"{MERGE_UNCONFIRMED}: push прошёл, но подтвердить попадание "
                f"{merged_sha[:12]} в {base} не удалось — спросите снова",
            )
        if not landed:
            return (
                False,
                f"push прошёл, а {merged_sha[:12]} в {base} не появился — "
                "доставку засчитывать нельзя",
            )

        # Закрытие — уже ПОСЛЕ доказательства: незакрытый PR неприятен, но
        # закрытый без мержа врёт сильнее.
        if not await self.forge.close_pr(pr_number, repo=repo, gh_repo=gh_repo):
            log.warning(
                "PR #%d влит, но не закрыт — он останется открытым на форже",
                pr_number,
            )
        log.info("Merged PR #%d by push (%s)", pr_number, merged_sha[:12])
        return (True, merged_sha)

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
        self,
        repo_url: str,
        workspace_path: str,
        base_branch: str | None = None,
        forge: str = DEFAULT_FORGE,
    ) -> tuple[bool, str]:
        """Provision a project workspace (#347, #377). Returns (ok, detail).

        Short ``owner/repo`` form tries https first — public repos clone
        anonymously with zero server setup — then falls back to ssh with
        the deploy key; the detail keeps every failed attempt so a private
        repo without a key reads as a diagnosis, not a stacktrace.
        Idempotent: an existing clone is verified against the expected
        origin and fetched instead of re-cloned — the fetch itself validates
        access, no ls-remote needed.

        ``forge`` НАЗЫВАЕТ ПЛОЩАДКУ, и до #1118 его здесь не было вовсе: обе
        строки-кандидата были захардкожены на github.com. 01.09.2026 проект
        #8 с ``forge=gitverse`` склонировался с GitHub и отчитался
        ``provision_status=ok`` — содержимое совпало лишь потому, что
        репозитории пока зеркалят друг друга.

        Значение по умолчанию — github, как у единственного читателя (#1114):
        вызывающий без форжа получает ровно прежнее поведение.
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
            # Совпадения owner/name НЕ ХВАТАЕТ, и это вторая половина дефекта
            # #1118. Проверка выше сравнивает только слаг, а «mrpda/snip-portal»
            # содержится в github-адресе ровно так же, как в gitverse-адресе.
            # Значит клон с чужой площадки проходил её как годный — и прошёл бы
            # даже после того, как клонирование научили форжу: каталог на месте,
            # новый клон не создаётся, статус остаётся зелёным навсегда.
            foreign = forge_urls.forge_of_host(_origin_host(origin))
            if foreign and foreign != forge:
                return False, (
                    f"existing workspace clones {foreign}, project declares "
                    f"{forge}: origin {origin.strip()}"
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
            # the deploy key is the private-repo fallback. #1118: хост берётся
            # у форжа, а не вписан литералом.
            candidates = forge_urls.clone_urls(forge, repo_url)

        url = None
        failures: list[str] = []
        causes: list[str] = []
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
            causes.append(_clone_cause(err))
        if url is None:
            named = next((c for c in causes if c), CLONE_CAUSE_UNNAMED)
            return False, (
                f"remote not accessible ({named}, forge {forge}): "
                + "; ".join(failures)
            )

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
            "clone_repo: cloned %s → %s (%s, %s, %s)",
            repo_url,
            workspace_path,
            base_branch,
            forge,
            transport,
        )
        # Форж в детали не украшение: до #1118 строка «cloned mrpda/snip-portal
        # (main, https)» одинаково описывала клон с любой площадки, и по ней
        # нельзя было понять, откуда взялся репозиторий.
        return True, f"cloned {repo_url} from {forge} ({base_branch}, {transport})"
