"""Git operations for the Hub branching workflow, plus the forge they pair with.

Local git operations run against the workspace repo (config.WORKSPACE_REPO_LINK).
Everything asked of the REPOSITORY HOST rather than of git — pull requests, CI
outcomes, merges — goes through ``protocols.ForgePlugin`` (#1113).
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import socket
import tarfile
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

from hub.actionable_errors import (
    pair_branch_dirty_detail,
    pair_worktree_dirty_detail,
)
from hub.config import PAIR_BASE_BRANCH
from hub.integrations import forge as forge_registry
from hub.integrations import git_history, git_rerun, proc
from hub.integrations.forge.github import GitHubForge
from hub.integrations.protocols import (
    BaseFreshness,
    BranchUpdateOutcome,
    CIProbeResult,
    CIRunRequestResult,
    FOREIGN_PR_ONLY,
    ForgePlugin,
    MergeabilityOutcome,
    SnapshotArchive,
    StackProbeOutcome,
    StackProbeResult,
)
from hub.mcp_envelope import enrich_error_payload
from hub.models import DEFAULT_FORGE

from hub import git_policy
from hub.commit_scope import parse_name_status_paths, parse_porcelain_paths

log = logging.getLogger(__name__)

# Exit code for a killed-on-timeout command: the shell convention, and distinct
# from any rc git itself returns, so a caller can tell a timeout from a refusal.
#: Имя заглушки-форжа. Хаб с ней стартует без настроенных интеграций, и
#: резолв по имени её не заменяет (см. GitOpsIntegration._forge_for).
NOOP_FORGE = "noop"

_TIMEOUT_RC = proc.TIMEOUT_RC


#: Прочитанные present-файлы по (клон, sha коммита, путь, потолок): sha
#: неизменяем, поэтому запись не стареет; missing/unreadable не кэшируются.
_BASE_FILE_CACHE: dict[tuple[str, str, str, int], dict[str, Any]] = {}


def _unreadable_file(ref: str, path: str, reason: str) -> dict[str, Any]:
    return {
        "state": "unreadable",
        "path": path,
        "ref": ref,
        "sha": "",
        "content": "",
        "truncated": False,
        "size": 0,
        "chars": 0,
        "reason": reason,
    }


async def _blob_prefix(repo: str, obj: str, nbytes: int) -> tuple[int, bytes, str]:
    """Первые ``nbytes`` байт blob; остальное git не отдаёт (#1630).

    ``head -c`` закрывает канал, git получает SIGPIPE: память ограничена
    префиксом при файле любого размера. Недобор против известного размера
    вызывающий читает как ошибку чтения.
    """
    return await proc.run_bytes(
        "sh",
        "-c",
        'git -C "$1" cat-file blob "$2" | head -c "$3"',
        "sh",
        repo,
        obj,
        str(nbytes),
        cwd=repo,
        timeout=30,
        max_bytes=nbytes + 1,
    )


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

    Distinct from returning "" ("no branch created", e.g. a hub without git
    integration, NoopGitOps): collapsing a refusal into it would turn an
    unconfigured hub into a blocked one.
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


# Тонкие обёртки над proc (#1113), а не `_run = proc.run`: присваивание
# захватывает объект функции, и подмена `proc.run` этот модуль не затронет.
# Обёртка ищет `proc.run` в момент вызова.


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

    Both halves come from one ``git status`` so they agree: the raw text
    decides whether to refuse, the paths say what to name. ``-z`` is required:
    without it git escapes non-ASCII paths, and a refusal naming a file under a
    name the person cannot find in their checkout has not named it (#555).
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
        # #475: name the base this project integrates on, not a literal
        # develop (a clone may have none): an instruction that fails reads as
        # the fix and leaves the workspace as it was.
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

    Two separate facts, kept separate (#954, #953): a branch that is on the remote
    but was never fetched here must not be read as unpushed work, nor make the
    hub push a stranger's branch on that untrue premise.
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

    "I have not looked" must not be reported as "the work is not there"
    (#767, #498): a missing tracking ref is refreshed by one targeted fetch
    before any verdict. The refspec is explicit: ``git fetch origin <branch>``
    alone is not guaranteed to write ``refs/remotes/origin/<branch>``.
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


def worktree_hint(task_id: int) -> str:
    """Where a task's worktree goes, as a template the caller can read (#1515).

    The hub does not know the folder name of the caller's clone, so it can
    name only the shape. The shape is cut from ``_worktree_path`` itself with a
    placeholder clone name: there is no second formula to drift from it.
    """
    template = _worktree_path(task_id, "/<имя вашего клона>")
    return (
        f"..{template} — шаблон относительно основного клона; "
        f"точный путь строит и создаёт `hp-hub worktree {task_id}`"
    )


def repo_slug(remote: str) -> str:
    """``owner/name`` of a remote URL or a bare ``owner/name``, lower-cased (#1515).

    Compares an origin (https, ssh or scp form) with the repo a project names.
    Case is ignored: forges treat owner and name case-insensitively.
    """
    text = (remote or "").strip().rstrip("/")
    text = text.removesuffix(".git")
    text = text.replace(":", "/") if "://" not in text else text.split("://", 1)[1]
    parts = [p for p in text.split("/") if p]
    return "/".join(parts[-2:]).lower()


async def clone_root(start: str) -> str:
    """Main clone of the git checkout at ``start``; ``""`` when there is none.

    Asked from inside a task worktree it still answers with the MAIN clone: the
    #459 path is a sibling of the clone, never of another worktree (#1515).
    """
    if not os.path.isdir(start):
        return ""
    rc, out, _ = await _git(
        "rev-parse", "--path-format=absolute", "--git-common-dir",
        repo=start, check=False,
    )  # fmt: skip
    common = out.strip()
    if rc != 0 or os.path.basename(common) != ".git":
        return ""
    return os.path.dirname(common)


async def origin_url(clone: str) -> str:
    """The clone's ``origin`` URL; ``""`` when it has none (#1515)."""
    rc, out, _ = await _git("remote", "get-url", "origin", repo=clone, check=False)
    return out.strip() if rc == 0 else ""


class WorktreeRefused(Exception):
    """A task worktree was not created or reused; nothing was touched (#1515)."""


async def _fetch_for_worktree(
    repo: str, branch: str, base: str, notes: list[str]
) -> None:
    """Best-effort fetch of the task branch and base before a new copy (#1515).

    A failure is not a refusal, but it is named in ``notes``: the copy is then
    cut from what the clone already has, which may be stale. Whether the task
    branch exists on origin is asked with ``ls-remote --heads`` (rc 0 and empty
    output: absent, as in ``_refresh_remote_ref``), never read from git's
    stderr, which follows the user's locale. An absent branch is the normal
    state of a task not yet pushed: no fetch and no note. The base is fetched
    always, so a missing base is a real warning.
    """
    for name in (branch, base):
        if name == branch:
            rc, out, err = await _git(
                "ls-remote", "--heads", "origin", f"refs/heads/{name}",
                repo=repo, check=False, timeout=30,
            )  # fmt: skip
            if rc == 0 and not out.strip():
                continue
            if rc != 0:
                notes.append(
                    f"fetch origin {name} не удался (ls-remote: "
                    f"{(err or '').strip()[:200] or rc}): "
                    f"копия строится по тому, что уже есть в клоне"
                )
                continue
        rc, _, err = await _git(
            "fetch",
            "origin",
            f"+refs/heads/{name}:refs/remotes/origin/{name}",
            repo=repo,
            check=False,
            timeout=30,
        )
        if rc != 0:
            notes.append(
                f"fetch origin {name} не удался ({(err or '').strip()[:200] or rc}): "
                f"копия строится по тому, что уже есть в клоне"
            )


async def ensure_task_worktree(
    task_id: int,
    branch: str,
    repo: str,
    base: str,
    notes: list[str] | None = None,
) -> str:
    """Create or reuse the task's worktree at the #459 path; return the path.

    A new copy takes the task branch from, in order: the local branch, the
    branch on origin (tracking it, so work already pushed is not lost),
    ``origin/<base>``, the local base. Both that and fetch failures go to ``notes``.

    Reuses a registered worktree of ``repo`` already on ``branch``; refuses, changing
    nothing, when the path holds another branch (dirty files named) or a directory
    git does not know. Never deletes and never switches a branch.
    """
    wt_path = _worktree_path(task_id, repo)
    await _git("worktree", "prune", repo=repo, check=False)
    if os.path.lexists(wt_path):
        if not (await _worktree_registered(wt_path, repo) and os.path.isdir(wt_path)):
            raise WorktreeRefused(
                f"{wt_path} существует, но не копия клона {repo}; ничего не изменено"
            )
        _, cur, _ = await _git("branch", "--show-current", repo=wt_path, check=False)
        cur = (cur or "").strip()
        if cur == branch:
            return wt_path
        dirty, files = await _dirty_state(wt_path)
        state = (
            f"с незакоммиченными правками ({_name_dirty_files(files)})"
            if dirty.strip()
            else "без правок"
        )
        raise WorktreeRefused(
            f"копия {wt_path} стоит на ветке {cur or '(detached)'!r} {state}, "
            f"а ветка задачи — {branch!r}; ничего не изменено"
        )
    os.makedirs(os.path.dirname(wt_path), exist_ok=True)
    await _fetch_for_worktree(repo, branch, base, notes if notes is not None else [])
    args: tuple[str, ...]
    rc, _, _ = await _git("rev-parse", "--verify", branch, repo=repo, check=False)
    if rc == 0:
        args = ("worktree", "add", wt_path, branch)
    else:
        remote = f"refs/remotes/origin/{branch}"
        rc, _, _ = await _git("rev-parse", "--verify", remote, repo=repo, check=False)
        if rc == 0:
            args = ("worktree", "add", "-b", branch, wt_path, remote)
        else:
            # Fetched base first, local as fallback; --no-track: develop is never upstream.
            ref = f"refs/remotes/origin/{base}"
            rc, *_ = await _git("rev-parse", "--verify", ref, repo=repo, check=False)
            start = ref if rc == 0 else base
            args = ("worktree", "add", "--no-track", "-b", branch, wt_path, start)
    rc, _, err = await _git(*args, repo=repo, check=False)
    if rc != 0:
        raise WorktreeRefused(
            f"git не создал копию {wt_path}: {(err or '').strip() or 'worktree add'}"
        )
    if "--no-track" in args and args[-1] == base and notes is not None:  # after git
        notes.append(f"origin/{base} нет: ветка начата от локальной {base}")
    return wt_path


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


# Transliteration for slugs (#607, #569): a fully Cyrillic title must not slug to ""
# ("task-601/" is an invalid branch name). One table for every _slugify
# consumer; deterministic, because the canonical-branch gate (#533) compares
# names strictly: one title, one slug.
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

    A slug already carrying ``task-<id>/`` (as a work plan writes it) must not
    double into ``task-818/task-818/...``: the gate then cannot find the PR and
    delivered code is reported undelivered. One function, not inline copies,
    so the rule cannot drift (#607).

    The prefix is stripped only when it matches THIS task exactly, slash
    included — "task-runner-fix" is a name, not a prefix. The strip repeats,
    so an already doubled branch normalises on its next pair-start.
    """
    slug = (branch_slug or "").strip()
    prefix = f"task-{task_id}/"
    # Strip BEFORE trimming slashes: "task-601/" is a prefix only while its
    # trailing slash is still there.
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


#: Тип записи tar, которую снимок несёт: обычные файлы и каталоги (#1599).
_SNAPSHOT_KEPT = (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE)


def _snapshot_is_empty(raw: bytes) -> bool:
    """``git archive`` пустого дерева — один pax_global_header без записей."""
    return raw.startswith(b"pax_global_header") and not raw[1024:].strip(b"\x00")


def _snapshot_repack(raw: bytes) -> tuple[bytes, str, int]:
    """Перепаковать ``git archive`` в канонический tar снимка: ``(tar, note, n)``.

    Политика, названная явно (#1599). Защищённый распаковщик принимает только
    обычные файлы и каталоги в ustar без PAX-переопределений, поэтому хаб
    сам убирает то, что распаковщик отверг бы целиком: символьные ссылки
    (их цель вне снимка, ревьюеру они не нужны — отброшены и посчитаны) и
    записи, чьё имя не умещается в ustar. Submodule git archive отдаёт
    пустым каталогом, он не раскрывается. ``export-ignore`` и
    ``export-subst`` — как у ``git archive``: действует то, что записано в
    дереве на sha. Режимы, uid/gid, время не переносятся — это не данные.
    """
    if _snapshot_is_empty(raw):
        return b"", "", 0
    out = io.BytesIO()
    dropped: dict[str, int] = {}
    count = 0
    with (
        tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as source,
        tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as target,
    ):
        for member in source:
            if member.type not in _SNAPSHOT_KEPT:
                kind = "ссылок" if member.issym() or member.islnk() else "прочих"
                dropped[kind] = dropped.get(kind, 0) + 1
                continue
            entry = tarfile.TarInfo(member.name)
            entry.type = tarfile.DIRTYPE if member.isdir() else tarfile.REGTYPE
            entry.mode = 0o755 if member.isdir() else 0o644
            entry.size = 0 if member.isdir() else member.size
            try:
                if member.isdir():
                    target.addfile(entry)
                else:
                    target.addfile(entry, source.extractfile(member))
            except ValueError:
                dropped["длинных путей"] = dropped.get("длинных путей", 0) + 1
                continue
            count += 1
    note = ", ".join(f"исключено {n} {kind}" for kind, n in sorted(dropped.items()))
    return out.getvalue(), note, count


async def _git(*args: str, repo: str | None = None, **kw) -> tuple[int, str, str]:
    repo = repo or _repo_root()
    return await _run("git", "-C", repo, *args, cwd=repo, **kw)


# #1332: код возврата shell для «команда не найдена». Проверка, не запустившая
# свой инструмент, ничего не сказала о дереве: читатель должен увидеть дефект
# среды хоста и имя команды, а не «валидация упала».
_COMMAND_NOT_FOUND_RC = 127


def _automerge_check_refusal(rc: int, log_tail: str) -> str:
    """Отказ автомержа по проверке сложенного: среда или дерево (#1332)."""
    said = " ".join((log_tail or "").split())[:400]
    if rc == _COMMAND_NOT_FOUND_RC:
        return (
            "дефект среды, а не кода: инструмент проверки сложенного не найден "
            f"в окружении сервиса (код возврата {rc}) — {said or 'команда не названа'}"
        )
    return (
        f"валидация после автомержа упала (код возврата {rc}): {said or 'вывода нет'}"
    )


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


async def _refresh_remote_ref(name: str, repo: str) -> tuple[str, str]:
    """One targeted refresh of ``origin/<name>``, failure kept apart from fact.

    ``(state, detail)``: ``refreshed`` — origin has the head and it is now a
    local tracking ref; ``absent`` — origin answered and has no such head;
    ``unreachable`` — origin did not answer (timeout, lock of a parallel
    fetch, auth, a blink), with the git error in ``detail``.

    Two commands rather than one fetch, because a fetch's non-zero rc does not
    separate "couldn't find remote ref" from "could not connect" without
    parsing localized stderr. ``ls-remote --heads`` does: rc != 0 is no
    answer, empty output is no such head — the same reading
    ``ensure_default_branch`` relies on (#1204).
    """
    rc, out, err = await _git(
        "ls-remote",
        "--heads",
        "origin",
        f"refs/heads/{name}",
        repo=repo,
        check=False,
        timeout=60,
    )
    if rc != 0:
        return (
            "unreachable",
            f"ls-remote rc={rc}: {err.strip()[:150] or 'git молчит'}",
        )
    if not out.strip():
        return ("absent", "")
    rc, _, err = await _git(
        "fetch",
        "origin",
        f"+refs/heads/{name}:refs/remotes/origin/{name}",
        repo=repo,
        check=False,
        timeout=60,
    )
    if rc != 0:
        return ("unreachable", f"fetch rc={rc}: {err.strip()[:150] or 'git молчит'}")
    return ("refreshed", "")


async def _resolve_ref_remote_first(name: str, repo: str) -> str | None:
    """Resolve a branch to a sha, preferring ``origin/<name>`` (#762).

    The mirror image of ``_resolve_ref``. For a comparison the hub will judge,
    the pushed branch is the subject: a local ref can trail origin by every
    commit under review (a submission of real work came back as "дифф пуст",
    #759, #756).

    Falls back to the local ref when ``origin/<name>`` does not exist at all
    (a workspace with no remote): refusing would turn a local-only repository
    into an error.
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


async def _diff_ends(base: str, sha: str, repo: str) -> tuple[str, str] | None:
    """Both ends of a ``base...sha`` comparison, as SHAs, origin-first (#1239).

    ``None`` when either end resolves nowhere — the caller then says "could not
    read", never "nothing changed". No fallback to the bare names on that path:
    a comparison shown to a human must not silently degrade into one against an
    old checkout.

    Reuses ``_resolve_ref_remote_first``: its local fallback keeps a local-only
    clone working, and a pinned sha resolves too (``origin/<sha>`` misses, the
    bare sha hits).
    """
    base_sha = await _resolve_ref_remote_first(base, repo)
    head_sha = await _resolve_ref_remote_first(sha, repo)
    if base_sha is None or head_sha is None:
        log.warning(
            "_diff_ends: %r not found in %s",
            base if base_sha is None else sha,
            repo,
        )
        return None
    return base_sha, head_sha


# Ancestry between two task branches (#1184). The names say which side of the
# pair the HEAD branch — the one being submitted — sits on, so a caller never
# has to remember argument order to read the answer.
STACK_ANCESTRY_HEAD_IS_DESCENDANT = "head_is_descendant"
STACK_ANCESTRY_HEAD_IS_ANCESTOR = "head_is_ancestor"
STACK_ANCESTRY_UNRELATED = "unrelated"
# One commit under two branch names (#1193). Kept apart from "unrelated": there
# each branch is outside the other's history, here each IS the other's. Both
# name no merge order; the caller words them differently.
STACK_ANCESTRY_SAME_TIP = "same_tip"
STACK_ANCESTRY_UNKNOWN = "unknown"


def _resolve_base(base_branch: str | None) -> str:
    """The base a task's work is cut from AND its PR targets (#362 I4).

    One function on purpose: two defaults that disagree cut a branch from main
    while its PR aims at develop, and a stale merge-base turns a change develop
    made to a line the task never touched into a conflict.
    """
    return base_branch or PAIR_BASE_BRANCH


# --- memory for the stacking probe (#1205) ---------------------------------
#
# The stacking walk is a DELIVERY CONDITION (#1186) that the poller re-runs
# every POLL_INTERVAL while a hold lasts (hours), so its answer is remembered.
#
# The key is the three SHAs, never branch names or a clock: the probe is two
# rev-lists over three resolved commits, so the same commits give the same
# answer. A name- or time-keyed memory could hand a delivery a ``clear`` taken
# before the branch moved, which is the merge #1186 exists to prevent.
#
# Only definite answers are remembered. ``unavailable`` is retryable (#1197),
# so caching it would glue one failed git call to a pair of commits forever.
_STACK_PROBE_CACHE: dict[tuple[str, str, str, str], StackProbeResult] = {}

#: The walk is bounded by the number of tasks with live branches, and the SHAs
#: move as work lands, so entries go stale rather than growing without limit.
#: A plain cap with a full clear is enough here and needs no eviction policy
#: to reason about: the next walk re-fills exactly what it asks for.
_STACK_PROBE_CACHE_MAX = 2048


def _stack_probe_cache_clear() -> None:
    """Forget every remembered probe answer (tests, and the size cap)."""
    _STACK_PROBE_CACHE.clear()


def _stack_probe_cache_get(
    key: tuple[str, str, str, str],
) -> StackProbeResult | None:
    return _STACK_PROBE_CACHE.get(key)


def _stack_probe_cache_put(
    key: tuple[str, str, str, str], result: StackProbeResult
) -> None:
    """Remember a DEFINITE answer for this exact triple of commits.

    Only ``stacked``/``clear``: ``unavailable`` is retryable (#1197), and a
    remembered failed git call would stick to the pair. An unresolved SHA has no
    key, and the key IS the freshness check, so it is asked again (#1205 AC-4).
    """
    if result.outcome not in (StackProbeOutcome.stacked, StackProbeOutcome.clear):
        return
    if len(_STACK_PROBE_CACHE) >= _STACK_PROBE_CACHE_MAX:
        _STACK_PROBE_CACHE.clear()
    _STACK_PROBE_CACHE[key] = result


async def _push_tree_head(
    repo: str, path: str, temp: str, target: str, lease: str | None = None
) -> tuple[int, str]:
    """Запушить HEAD одноразового дерева в ``target`` через временную ветку.

    pre-push хук (.githooks/pre-push) читает ЛОКАЛЬНЫЙ ref строки пуша, а ни
    «HEAD», ни голого sha в его списке нет (#1438); хаб вооружает хуком каждый
    клон воркспейса (#532).

    Поэтому коммит кладётся во временную локальную ветку с именем по шаблону
    хука (``temp``) и пушится полным refspec — хук видит обычную ветку, обхода
    нет. Настоящую локальную ветку ``target`` не трогаем: её может держать
    живое дерево исполнителя, и ``checkout -B`` переставил бы ref под ним
    (e74f63e646dd4078). ``lease`` — ожидаемая вершина ``target`` на origin:
    пуш пройдёт, только если ветка всё ещё там (``None`` — без аренды; пустая
    строка — аренда «ветки нет», как у git). Временная ветка удаляется при
    любом исходе.
    """
    rc, _, err = await _git("branch", "-f", temp, "HEAD", repo=path, check=False)
    if rc != 0:
        return rc, f"временная ветка {temp} не создана: {err}"
    args = ["push"]
    if lease is not None:
        args.append(f"--force-with-lease=refs/heads/{target}:{lease.strip()}")
    args += ["origin", f"refs/heads/{temp}:refs/heads/{target}"]
    try:
        rc, _, err = await _git(*args, repo=path, check=False, timeout=60)
    finally:
        await _git("branch", "-D", temp, repo=repo, check=False)
    return rc, err


_PUSH_STATUS_RE = re.compile(
    r"^\s*!\s*\[(remote rejected|rejected)\]\s+\S+\s+->\s+\S+(?:\s+\((.*)\))?\s*$"
)
_PUSH_URL_RE = re.compile(r"\S+://\S+|'[^']*'")
# Код 403 — только в ответе сервера, не любое число внутри пути, имени или sha.
_PUSH_HTTP_403_RE = re.compile(r"error: 403|\bhttp(?:/[\d.]+)?\s+403\b|\b403 forbidden")
_PUSH_DENIAL_WORDS = ("permission", "denied", "protected", "declined")


def _push_diagnostics(err: str) -> tuple[str, bool]:
    """Только диагностика git push: ``(текст, аренда_отказала)``.

    Имена веток, пути и URL в выводе — не причина отказа: в строке отказа аренды
    стоит имя временной ветки ``task-N/gate-update-<sha>``, а путь или имя
    ветки может содержать «protected» или «403». Поэтому разбираются строки
    статуса (`` ! [rejected] SRC -> DST (ПРИЧИНА)``) — берётся только причина, —
    а остальные строки читаются без URL и кавычек; строки «To <путь>», «hint:» и
    «failed to push some refs» не читаются вовсе.
    """
    parts: list[str] = []
    lease_rejected = False
    for raw in (err or "").splitlines():
        line = raw.strip()
        if not line or line.lower().startswith(("to ", "hint:", "everything ")):
            continue
        status = _PUSH_STATUS_RE.match(raw)
        if status:
            lease_rejected = lease_rejected or status.group(1) == "rejected"
            parts.append(status.group(2) or "")
            continue
        for prefix in ("remote:", "fatal:", "error:"):
            if line.lower().startswith(prefix):
                line = line[len(prefix) :].strip()
                break
        if line.lower().startswith("failed to push"):
            continue
        parts.append(_PUSH_URL_RE.sub("", line))
    return "\n".join(parts).lower(), lease_rejected


def _push_refusal(rc: int, err: str) -> BranchUpdateOutcome:
    """Отказ пуша обновлённой ветки — чьи руки его лечат (#1419).

    Как у пути API: аренда (голова уехала) — head_moved, повтор; таймаут и
    сеть — unavailable, повтор; к человеку (refused) — только отказ прав или
    защиты ветки на стороне сервера. Решает диагностика git
    (``_push_diagnostics``), а не весь stderr.
    """
    if rc == _TIMEOUT_RC:
        return BranchUpdateOutcome.unavailable
    low, lease_rejected = _push_diagnostics(err)
    if any(w in low for w in _PUSH_DENIAL_WORDS) or _PUSH_HTTP_403_RE.search(low):
        return BranchUpdateOutcome.refused
    if lease_rejected or any(
        w in low for w in ("stale info", "non-fast-forward", "fetch first")
    ):
        return BranchUpdateOutcome.head_moved
    return BranchUpdateOutcome.unavailable


class GitOpsIntegration(git_history.HistoryReads, git_rerun.RerunCalls):
    """Concrete git_ops plugin: local git here, the repository host behind a forge.

    ``forge`` is an attribute, not a module-level singleton, so a project on
    another host gets its own adapter without touching a call site (#1112).
    """

    def __init__(self, forge: ForgePlugin | None = None) -> None:
        self.forge: ForgePlugin = forge or GitHubForge()

    def _forge_for(self, forge: str = "") -> ForgePlugin:
        """Адаптер ДЛЯ ЭТОГО ВЫЗОВА (#1146).

        Колонка ``projects.forge`` (#1114) раньше не читалась: форж выбирался
        один раз при старте и обслуживал все проекты.

        * Имя не названо ИЛИ настроенный адаптер обслуживает именно этот форж —
          отвечает настроенный: поведение github-проектов не меняется, а ПОДМЕНА
          адаптера в тесте продолжает получать вызовы.
        * Проект объявил другой форж — вызов уходит к адаптеру этого форжа.

        Отдельно ненастроенный хаб. ``NoopForge`` означает «хостинга нет вовсе»,
        и резолвить поверх него нельзя: это включило бы интеграцию, которую
        никто не настраивал, и заменило бы честное «спросить не удалось» на
        содержательный ответ — ровно тот дефект, который разбирали #419 и #725.
        """
        configured = self.forge
        if not forge or forge == configured.name or configured.name == NOOP_FORGE:
            return configured
        return forge_registry.client_for(forge)

    async def current_branch(self, repo: str | None = None) -> str:
        rc, out, _ = await _git("branch", "--show-current", repo=repo, check=False)
        return out if rc == 0 else "unknown"

    async def branch_ancestry(
        self,
        branch: str,
        other_branch: str,
        repo: str | None = None,
    ) -> str:
        """Which of two branches stands on the other (#1184).

        ``branch_contains_unmerged_commits_of`` is symmetric for a stacked pair
        (both branches carry the ancestor's commits), so an advisory built on it
        named the merge order from whichever task was submitting. The order is a
        property of the branches: ask git about it directly.

        Returns one of ``STACK_ANCESTRY_*``, each named by its value:

        - ``head_is_descendant`` — the head stands on the other branch; the
          other branch merges first.
        - ``head_is_ancestor`` — the other branch stands on the head; the head
          merges first.
        - ``same_tip`` — both names point at one commit (#1193): each branch
          IS the other's history, so there is no order to name, and the caller
          must not word it as ``unrelated``, whose fact is the opposite.
        - ``unrelated`` — neither branch is in the other's history; no order
          to name.
        - ``unknown`` — the question could not be answered. Never raises and
          never guesses: an unresolvable ref, an unreadable repository or a
          commit this clone does not carry lands here, and the caller must
          report "order not determined" rather than fall back to a side.
        """
        if repo is None:
            reason = await _default_workspace_error()
            if reason:
                return STACK_ANCESTRY_UNKNOWN
        repo = repo or _repo_root()
        # Remote-first for the same reason as the stacking check itself
        # (#1046 / #762): a stale local ref is not the branch under judgement.
        head = await _resolve_ref_remote_first(branch, repo)
        other = await _resolve_ref_remote_first(other_branch, repo)
        if not (head and other):
            return STACK_ANCESTRY_UNKNOWN

        # is_ancestor (#497) is the one primitive for this question; a second
        # one lets two answers disagree.
        head_is_descendant = await self.is_ancestor(repo, other, head)
        head_is_ancestor = await self.is_ancestor(repo, head, other)
        if head_is_descendant is None or head_is_ancestor is None:
            return STACK_ANCESTRY_UNKNOWN
        if head_is_descendant and head_is_ancestor:
            # Both yes is possible only for one commit: two DIFFERENT commits
            # cannot each contain the other.
            return STACK_ANCESTRY_SAME_TIP
        if head_is_descendant:
            return STACK_ANCESTRY_HEAD_IS_DESCENDANT
        if head_is_ancestor:
            return STACK_ANCESTRY_HEAD_IS_ANCESTOR
        return STACK_ANCESTRY_UNRELATED

    async def branch_contains_unmerged_commits_of(
        self,
        branch: str,
        other_branch: str,
        base_branch: str | None = None,
        repo: str | None = None,
    ) -> bool:
        """True when ``branch`` carries commits unique to ``other_branch`` (#438).

        The advisory predicate: a stack is True, everything else — including
        every way the question could not be answered — is False. It reads the
        judgement off ``branch_stacking_probe`` (#1186), which spells out what
        "False" hides.
        """
        probe = await self.branch_stacking_probe(
            branch, other_branch, base_branch=base_branch, repo=repo
        )
        return probe.outcome is StackProbeOutcome.stacked

    async def branch_stacking_probe(
        self,
        branch: str,
        other_branch: str,
        base_branch: str | None = None,
        repo: str | None = None,
    ) -> StackProbeResult:
        """Does ``branch`` carry commits unique to ``other_branch`` (#438, #1186)?

        A walk of one over ``branch_stacking_probe_batch``: one body does the
        git work and holds the memory.
        """
        async for _, result in self.branch_stacking_probe_batch(
            branch, (other_branch,), base_branch=base_branch, repo=repo
        ):
            return result
        # A one-element walk always yields exactly one answer; this line only
        # exists so the function has no implicit ``None`` return.
        return StackProbeResult(
            outcome=StackProbeOutcome.unavailable,
            reason="no_candidates",
        )

    async def branch_stacking_probe_batch(
        self,
        branch: str,
        other_branches: Sequence[str],
        base_branch: str | None = None,
        repo: str | None = None,
    ) -> AsyncIterator[tuple[str, StackProbeResult]]:
        """One branch against a LIST of candidates, answered lazily (#1205).

        Merge-base analysis against ``base_branch``: each candidate owns the
        commits reachable from it but not from base; if ``branch`` contains
        any of them, the two are stacked and ``branch`` cannot be verified
        against base independently. Refs are resolved remote-first (#1046 /
        #762): a stale local develop must not invent a stack.

        Every way of NOT getting an answer is ``unavailable`` with the reason
        named, never ``clear``. The distinction is free for the advisory
        callers and load-bearing for the delivery gate, which merges on it.

        A LIST, NOT A PAIR (#1205): ``branch`` and the base resolve once for
        the whole walk, not per candidate (two extra subprocesses per row on
        every poll cycle while a delivery is held).

        LAZY ON PURPOSE: the gate stops at the first dangerous stack (#1186),
        so yielding pairs spares rev-lists on rows nobody reads.

        A ref that resolves nowhere is REFRESHED ONCE before the answer is
        formed (#1204): otherwise ``ref_unresolved`` conflated a branch deleted
        on the server with one this clone never fetched, and the delivery gate
        told a human "nobody will bring that branch back" about a branch that
        was on origin all along. The refspec is the targeted one from #954.

        A refresh that gets NO answer (timeout, a parallel fetch holding the
        lock, auth, origin blinking) is NOT ``ref_unresolved``: it is
        ``remote_unreachable`` — retryable, never a ground for calling a human.
        The refresh asks via ``ls-remote --heads`` as ``ensure_default_branch``
        does (rc != 0: no answer; empty output: no such head).

        IN A BATCHED WALK (#1204 x #1205) ``branch`` and the base resolve once,
        so their refresh is attempted once, and an unanswered one is not a fact
        about any single candidate: every row gets the same
        ``remote_unreachable``, like ``workspace_unavailable``. A candidate's
        own ref is resolved, and refreshed, per row. Refreshing the first row
        only would tell the later rows ``ref_unresolved`` — the one reason the
        delivery gate may call a human with.
        """
        if repo is None:
            reason = await _default_workspace_error()
            if reason:
                for other_branch in other_branches:
                    yield (
                        other_branch,
                        StackProbeResult(
                            outcome=StackProbeOutcome.unavailable,
                            reason="workspace_unavailable",
                            details=reason,
                        ),
                    )
                return
        repo_path = repo or _repo_root()
        # #1046: judge the pushed refs (a stale local develop made a false stack).
        # Resolved ONCE for the whole walk: neither the branch under judgement
        # nor the base changes between candidates.
        head = await _resolve_ref_remote_first(branch, repo_path)
        base_name = _resolve_base(base_branch)
        base = await _resolve_ref_remote_first(base_name, repo_path)
        # One targeted refresh per unresolved name, then ask again (#1204). An
        # unanswered refresh is a fact about this machine and the network, not
        # about the branch; for these two walk-wide names it goes to EVERY row.
        shared_unreachable = ""
        if not head:
            state, detail = await _refresh_remote_ref(branch, repo_path)
            if state == "unreachable":
                shared_unreachable = f"{branch}: {detail}"
            else:
                head = await _resolve_ref_remote_first(branch, repo_path)
        if not shared_unreachable and not base:
            state, detail = await _refresh_remote_ref(base_name, repo_path)
            if state == "unreachable":
                shared_unreachable = f"{base_name}: {detail}"
            else:
                base = await _resolve_ref_remote_first(base_name, repo_path)
        for other_branch in other_branches:
            if shared_unreachable:
                yield (
                    other_branch,
                    StackProbeResult(
                        outcome=StackProbeOutcome.unavailable,
                        reason="remote_unreachable",
                        details=shared_unreachable,
                    ),
                )
                continue
            other = await _resolve_ref_remote_first(other_branch, repo_path)
            if not other:
                state, detail = await _refresh_remote_ref(other_branch, repo_path)
                if state == "unreachable":
                    yield (
                        other_branch,
                        StackProbeResult(
                            outcome=StackProbeOutcome.unavailable,
                            reason="remote_unreachable",
                            details=f"{other_branch}: {detail}",
                        ),
                    )
                    continue
                other = await _resolve_ref_remote_first(other_branch, repo_path)
            if not (head and other and base):
                unresolved = [
                    name
                    for name, ref in (
                        (branch, head),
                        (other_branch, other),
                        (base_name, base),
                    )
                    if not ref
                ]
                yield (
                    other_branch,
                    StackProbeResult(
                        outcome=StackProbeOutcome.unavailable,
                        reason="ref_unresolved",
                        details=", ".join(unresolved),
                    ),
                )
                continue
            # The three SHAs ARE the question: a remembered answer is reused only
            # while all three still point where they did (#1205).
            key = (repo_path, head, other, base)
            remembered = _stack_probe_cache_get(key)
            if remembered is not None:
                yield other_branch, remembered
                continue
            result = await self._stacking_rev_lists(head, other, base, repo_path)
            _stack_probe_cache_put(key, result)
            yield other_branch, result

    async def _stacking_rev_lists(
        self, head: str, other: str, base: str, repo: str
    ) -> StackProbeResult:
        """The two rev-lists behind every stacking answer (#438, #1186).

        Split out of the walk so the memory above has exactly one thing to
        remember: a function of three commits and nothing else.
        """
        rc, total, _ = await _git(
            "rev-list", "--count", other, f"^{base}", repo=repo, check=False
        )
        rc2, excluded, _ = await _git(
            "rev-list", "--count", other, f"^{base}", f"^{head}", repo=repo, check=False
        )
        if rc != 0 or rc2 != 0:
            return StackProbeResult(
                outcome=StackProbeOutcome.unavailable,
                reason="rev_list_failed",
                details=f"rc={rc}/{rc2} for {other} ^{base}",
            )
        try:
            total_n = int(total.strip() or "0")
            excluded_n = int(excluded.strip() or "0")
        except ValueError:
            return StackProbeResult(
                outcome=StackProbeOutcome.unavailable,
                reason="rev_list_unparseable",
                details=f"{total.strip()!r}/{excluded.strip()!r}",
            )
        # other_branch has unmerged commits, and at least one is inside branch.
        if total_n > 0 and excluded_n < total_n:
            return StackProbeResult(
                outcome=StackProbeOutcome.stacked,
                reason="shares_unmerged_commits",
                details=f"{total_n - excluded_n} of {total_n} commits shared",
            )
        return StackProbeResult(
            outcome=StackProbeOutcome.clear,
            reason="no_shared_unmerged_commits",
        )

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

        # Refuse before touching anything (#361 I2). The old shape never read the
        # checkout's rc and ran `checkout .` + `clean -fd` on a dirty tree,
        # destroying uncommitted work (on whatever branch was still checked out).
        # docs/workspace-safety-policy.md invariant 4: when git state cannot be
        # interpreted reliably, escalate rather than proceed.
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

        # Confirm the checkout actually landed: rc alone is not enough.
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
                # #966: отказ «зайди на сервер и запушь» невыполним для вызывающего
                # (доступа к общему клону у агентов нет). Push обычной task-ветки
                # безопасен и обратим, поэтому хаб делает его сам и отказывает,
                # только если не вышло. Только ветки task-<id>/*: публиковать
                # произвольные ветки — не наше решение.
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

        Three states, never two: ``resolved`` with the sha, ``missing`` when the
        repository was readable and the ref is not in it, ``unavailable`` when
        there was nothing to look in. Collapsing the last two would print "we
        could not check" as "that branch does not exist".
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
        merge, which can end with the integration branch deleted (GitHub
        offers it, automation takes it), leaving nothing to deliver into —
        unnoticed, since a release that just succeeded looks like the opposite
        of an outage.

        Restores from the release branch, not from a local ref (whatever an
        earlier checkout left behind; after the merge both carry the same
        content). Never forces: if the branch is there, it is left as it is.
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
        # #949 (record #4612): a push of ``<sha>:refs/heads/<branch>`` is refused by the
        # pre-push hook, which reads the LOCAL ref of every push line, and a bare
        # sha matches no allowed branch name (every workspace clone is armed,
        # #532). So push a real local branch under the restored name: the hook
        # sees ``refs/heads/<branch>``, no bypass. By where the clone stands:
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

    async def branch_diff(
        self, repo: str, base: str, branch: str, context: int = 0
    ) -> str | None:
        """``git diff -U<context> base...branch``, or None when unreadable (#601).

        ``context`` (#1582) is the unchanged lines around each change: 0 for
        the hub's own measurements, more for a reviewer that has no clone.

        None rather than "" on failure: an empty diff and an unreadable one are
        different answers, and the section must be able to say which.

        #1055: on a pair task the branch is written on a developer's machine and
        arrives as ``origin/<branch>`` only, with no local head; interpolating
        the names verbatim gave "unknown revision" and every caller lost its
        subject (the brief, the review profile sizing).

        Both ends go through ``_resolve_ref_remote_first`` (as
        ``branch_diff_paths``, #762), and a ref that resolves nowhere is fetched
        once before the answer becomes None. A sha passed instead of a branch
        name still resolves: ``origin/<sha>`` misses, the bare sha hits.
        """
        return await self._branch_diff(repo, base, branch, context, fetch=True)

    async def branch_diff_local(
        self, repo: str, base: str, branch: str, context: int = 0
    ) -> str | None:
        """``branch_diff`` без сети (#1913): ref не нашёлся — ``None``, а не fetch."""
        return await self._branch_diff(repo, base, branch, context, fetch=False)

    async def _branch_diff(
        self, repo: str, base: str, branch: str, context: int, *, fetch: bool
    ) -> str | None:
        base_sha = await _resolve_ref_remote_first(base, repo)
        branch_sha = await _resolve_ref_remote_first(branch, repo)
        if fetch and (base_sha is None or branch_sha is None):
            # Best effort, как в branch_diff_paths: сбой сети не ломает чтение.
            await _git("fetch", "origin", base, branch, repo=repo, check=False)
            base_sha = base_sha or await _resolve_ref_remote_first(base, repo)
            branch_sha = branch_sha or await _resolve_ref_remote_first(branch, repo)
        if base_sha is None or branch_sha is None:
            name = base if base_sha is None else branch
            log.warning("branch_diff: %r not found in %s", name, repo)
            return None
        spec = f"{base_sha}...{branch_sha}"
        rc, out, _ = await _git(
            "diff", f"-U{max(0, int(context))}", spec, repo=repo, check=False
        )
        return out if rc == 0 else None

    async def integrity_read(
        self, repo: str, base: str, sha: str
    ) -> dict[str, str] | None:
        """The diff ``base...sha`` for the CI integrity scan, of EXACTLY that commit (#1688).

        ``{"diff", "base_sha", "head_sha"}``, or None when it cannot be read;
        ``base_sha`` is the MERGE-BASE of the base branch and the head.
        The head is resolved as an OBJECT (``<sha>^{commit}``), never through
        ``_resolve_ref_remote_first``: that tries ``origin/<name>`` first, so a
        branch NAMED with the full sha of a malicious commit B and pointing at
        a clean A would have A scanned and stored as the scan of B. The result
        must equal the requested sha or nothing is returned. The base is a
        branch name and is resolved by full ref name (``refs/remotes/origin/``,
        then ``refs/heads/``), so no other ref can shadow it.

        The text is forced (``--text --no-textconv --no-ext-diff``): a
        ``-diff`` attribute would otherwise turn a hook into «Binary files differ».
        """
        wanted = (sha or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", wanted):
            return None

        async def _object() -> str | None:
            rc, out, _ = await _git(
                "rev-parse",
                "--verify",
                "--quiet",
                f"{wanted}^{{commit}}",
                repo=repo,
                check=False,
            )
            return out.strip().lower() if rc == 0 and out.strip() else None

        head = await _object()
        await _git("fetch", "origin", base, repo=repo, check=False)
        if head is None:
            await _git("fetch", "origin", wanted, repo=repo, check=False)
            head = await _object()
        if head != wanted:
            return None
        base_sha = None
        for ref in (f"refs/remotes/origin/{base}", f"refs/heads/{base}"):
            rc, out, _ = await _git(
                "rev-parse",
                "--verify",
                "--quiet",
                f"{ref}^{{commit}}",
                repo=repo,
                check=False,
            )
            if rc == 0 and out.strip():
                base_sha = out.strip()
                break
        if base_sha is None:
            return None
        # The comparison point is the MERGE-BASE, not the tip of the base: the
        # diff and the base-side files must describe the same commit, and a
        # base that moved since the branch was cut is none of the author's work.
        rc, merge_base, _ = await _git(
            "merge-base", base_sha, head, repo=repo, check=False
        )
        if rc != 0 or not merge_base.strip():
            return None
        base_sha = merge_base.strip()
        rc, diff, _ = await _git(
            "diff",
            "--text",
            "--no-textconv",
            "--no-ext-diff",
            "--no-color",
            "-U0",
            f"{base_sha}..{head}",
            repo=repo,
            check=False,
        )
        if rc != 0:
            return None
        return {"diff": diff, "base_sha": base_sha, "head_sha": head}

    async def delta_without_base(
        self, repo: str, base: str, prev: str, current: str
    ) -> str | None:
        """The author's OWN patch series inside ``prev..current`` (#1249).

        ``None`` when the question could not be asked — an unknown base ref, a
        commit this checkout does not carry, a git that refused. The caller
        turns that into "origin unknown, read everything", never into "nothing
        came from the base": a silent narrowing of the subject is the one
        failure this must not have.

        Why it exists. The previous submission is an ANCESTOR of the current
        tip, so ``git diff prev current`` also carries everything the author
        pulled in by merging the base branch (#1172: 23 of 25 files).

        Origin is decided by REACHABILITY, which is the only fact git will
        state: ``--not origin/<base>`` drops every commit the base branch
        already contains. Commit messages and author names are not consulted —
        a merge commit says "Merge ..." whoever wrote the code inside it.

        ``--cc`` rather than ``--no-merges``: on a merge commit the combined
        diff shows the hunks that match NEITHER parent, i.e. the conflict
        resolution the author typed by hand, which is the author's work.
        """
        base_sha = await _resolve_ref_remote_first(base, repo)
        if base_sha is None:
            # One best-effort fetch, exactly as ``branch_diff`` does: a stale
            # clone must not turn a readable answer into an unreadable one.
            await _git("fetch", "origin", base, repo=repo, check=False)
            base_sha = await _resolve_ref_remote_first(base, repo)
        if base_sha is None:
            log.warning("delta_without_base: base %r not found in %s", base, repo)
            return None
        for sha in (prev, current):
            rc, _, _ = await _git(
                "cat-file", "-e", f"{sha}^{{commit}}", repo=repo, check=False
            )
            if rc != 0:
                log.warning("delta_without_base: %r not found in %s", sha, repo)
                return None
        rc, out, _ = await _git(
            "log",
            "-p",
            "-U0",
            "--cc",
            "--format=",
            current,
            "--not",
            prev,
            base_sha,
            "--",
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

    async def read_file_at_ref(
        self, repo: str, ref: str, path: str, *, limit_chars: int = 30000
    ) -> dict[str, Any]:
        """Общий дедлайн на чтение (45 с); просрочка — unreadable, а не зависание."""
        try:
            return await asyncio.wait_for(
                self._read_file_at_ref(repo, ref, path, limit_chars), timeout=45
            )
        except (TimeoutError, asyncio.TimeoutError):
            return _unreadable_file(ref, path, "чтение не уложилось в 45 с")

    async def _read_file_at_ref(
        self, repo: str, ref: str, path: str, limit_chars: int
    ) -> dict[str, Any]:
        """Файл на ``ref`` с честным состоянием: present | missing | unreadable (#1630).

        ``file_at_ref`` схлопывает «файла нет» и «не смогли прочитать» в None —
        для правил рецензента это нарочно. Здесь различие и есть ответ: пустой
        файл это present с пустым content, отсутствие — missing, ошибка ref или
        клона — unreadable с причиной. Читается blob по объекту, не ``strip()``.
        """
        out: dict[str, Any] = {
            "state": "unreadable",
            "path": path,
            "ref": ref,
            "sha": "",
            "content": "",
            "truncated": False,
            "size": 0,
            "chars": 0,
            "reason": "",
        }
        rc, sha, err = await _git(
            "rev-parse",
            "--verify",
            f"{ref}^{{commit}}",
            repo=repo,
            check=False,
            timeout=30,
        )
        if rc != 0:
            out["reason"] = f"ref {ref!r} не прочитан: {err[:120] or f'rc={rc}'}"
            return out
        out["sha"] = sha.strip()
        key = (repo, out["sha"], path, limit_chars)
        if key in _BASE_FILE_CACHE:
            return dict(_BASE_FILE_CACHE[key])
        rc, tree, err = await _git(
            "ls-tree", "-z", out["sha"], "--", path, repo=repo, check=False, timeout=30
        )
        if rc != 0:
            out["reason"] = f"дерево {ref!r} не прочитано: {err[:120] or f'rc={rc}'}"
            return out
        entry = tree.split("\0", 1)[0]
        meta, _, name = entry.partition("\t")
        fields = meta.split()
        if not entry or name != path or len(fields) != 3:
            out["state"] = "missing"
            return out
        mode, kind, obj = fields
        if kind != "blob" or mode not in ("100644", "100755"):
            out["reason"] = f"{path} не обычный файл (mode {mode})"
            return out
        rc, size_text, err = await _git(
            "cat-file", "-s", obj, repo=repo, check=False, timeout=30
        )
        if rc != 0 or not size_text.strip().isdigit():
            out["reason"] = f"размер {path} не прочитан: {err[:120] or f'rc={rc}'}"
            return out
        size = int(size_text.strip())
        out["size"] = size
        # Префикс blob, а не весь: память ограничена потолком, файл любого
        # размера даёт present + truncated + size (AC-2).
        want = min(size, limit_chars * 4 + 8)
        rc, body, err = await _blob_prefix(repo, obj, want) if want else (0, b"", "")
        if rc != 0 or len(body) < want:
            out["reason"] = f"содержимое {path} не прочитано: {err[:120] or f'rc={rc}'}"
            return out
        text = body.decode("utf-8", errors="replace")
        whole = len(body) >= size
        out["state"] = "present"
        out["chars"] = len(text) if whole else 0
        out["truncated"] = (not whole) or len(text) > limit_chars
        out["content"] = text[:limit_chars]
        if len(_BASE_FILE_CACHE) >= 64:
            _BASE_FILE_CACHE.pop(next(iter(_BASE_FILE_CACHE)))
        _BASE_FILE_CACHE[key] = dict(out)
        return out

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

    async def files_naming_at_ref(
        self, repo: str, ref: str, word: str, pathspec: str = "*.py"
    ) -> set[str] | None:
        """Files of ``ref`` where ``word`` occurs as a WHOLE WORD (#1287).

        ``-w`` is git's own word rule (``ledger_row`` does not match inside
        ``ledger_rows``): a substring hit must never read as presence, or it
        would open a task whose subject does not exist under a similar name.
        ``-F`` keeps regex characters in a name from being read as a pattern.

        Three answers, not two (#725): the empty set means "looked and it is
        nowhere", ``None`` means the look did not happen. Git says the first
        with exit code 1 and the second with anything above it.
        """
        rc, out, _ = await _git(
            "grep",
            "-l",
            "-w",
            "-F",
            "-e",
            word,
            ref,
            "--",
            pathspec,
            repo=repo,
            check=False,
        )
        if rc == 1:
            return set()
        if rc != 0:
            return None
        prefix = f"{ref}:"
        return {
            line[len(prefix) :].strip()
            for line in out.splitlines()
            if line.startswith(prefix) and line[len(prefix) :].strip()
        }

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

        Both ends go through ``_diff_ends`` (#1239): the bare base name is not
        the base the hub judges against (the shared clone's local ``develop``
        trails ``origin/develop``), so the card showed the submission against a
        point in the past and did not say which one the reader saw.
        """
        ends = await _diff_ends(base, sha, repo)
        if ends is None:
            return None
        base_sha, head_sha = ends
        rc, out, _ = await _git(
            "diff",
            f"-U{int(context)}",
            f"{base_sha}...{head_sha}",
            repo=repo,
            check=False,
        )
        return out if rc == 0 else None

    async def commit_in_base_history(
        self, repo: str, base: str, sha: str
    ) -> bool | None:
        """Is ``sha`` already inside the history of the base? (#1239)

        THE question to ask before reading an empty three-dot diff as "this
        changed nothing": once the commit lands in the base's history the merge
        base IS the commit and ``base...sha`` is empty ALWAYS, whatever the
        submission touched.

        Three answers, never two, as ``is_ancestor``: ``None`` (could not ask)
        must not collapse into "no" — "we did not check" printed as "not merged"
        sends the reader back to trusting an empty screen.

        Ancestry is asked of the SAME tip the diff was computed against (both
        via ``_diff_ends``): on a bare name they disagree (``merge-base
        --is-ancestor <sha> develop`` gives rc=1 while ``origin/develop`` gives 0).

        One detector, shared by the card (``task_diff``) and the live evidence
        packet (``steward_evidence``).
        """
        ends = await _diff_ends(base, sha, repo)
        if ends is None:
            return None
        base_sha, head_sha = ends
        return await self.is_ancestor(repo, head_sha, base_sha)

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

        Paths and counts only. The card needs WHICH files a submission touched
        to lay criteria against them; the full diff is loaded on demand (#824).

        Both ends resolved origin-first through ``_diff_ends`` (#1239), for the
        same reason ``commit_diff`` does it: the change map and the diff below
        it must describe one and the same comparison, and until #1239 they
        agreed only by accident — both were wrong in the same way.
        """
        ends = await _diff_ends(base, sha, repo)
        if ends is None:
            return None
        base_sha, head_sha = ends
        rc, out, _ = await _git(
            "diff", "--numstat", f"{base_sha}...{head_sha}", repo=repo, check=False
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

    async def snapshot_archive(
        self, repo: str, sha: str, max_bytes: int, ref: str = ""
    ) -> SnapshotArchive:
        """Снимок дерева на закреплённом ``sha`` для ревьюера без клона (#1599).

        ``git archive --format=tar <sha>``, прочитанный ПОТОКОМ с потолком
        (``proc.run_capped``): превышение останавливает и убивает процесс, а не
        копит вывод. Вывод — сырые байты, без decode и strip. Объект ``sha``
        при необходимости доставляется (``commit_exists`` → ``fetch_commit``).
        Ни одна ветка не бросает: отсутствие снимка — это ``SnapshotArchive``
        с названной причиной, и вызывающий не принимает его за «чисто».
        Политика содержимого — в ``_snapshot_repack``.
        """
        if not sha or not repo:
            return SnapshotArchive(sha=sha, data=None, reason="нет sha или клона")
        exists = await self.commit_exists(repo, sha)
        if exists is None:
            return SnapshotArchive(sha=sha, data=None, reason="клон не читается")
        if not exists:
            fetched, why = await self.fetch_commit(repo, sha, ref)
            if not fetched:
                return SnapshotArchive(
                    sha=sha,
                    data=None,
                    reason=f"коммит {sha[:12]} не найден в клоне хаба и не "
                    f"доставлен: {why or 'причина не названа'}"[:300],
                )
        rc, raw, err = await proc.run_capped(
            "git",
            "-C",
            repo,
            "archive",
            "--format=tar",
            sha,
            cwd=repo,
            timeout=120,
            max_bytes=max_bytes,
        )
        if rc != 0:
            reason = (
                f"архив больше потолка {max_bytes} байт"
                if rc == -2
                else f"git archive не удался (rc={rc}): {err}"
            )
            return SnapshotArchive(sha=sha, data=None, reason=reason[:300])
        try:
            data, note, count = await asyncio.to_thread(_snapshot_repack, raw)
        except (tarfile.TarError, OSError, ValueError) as exc:
            return SnapshotArchive(
                sha=sha, data=None, reason=f"архив не разобран: {exc}"[:300]
            )
        if count == 0:
            return await self._nothing_to_snapshot(repo, sha, note)
        return SnapshotArchive(sha=sha, data=data, note=note)

    async def _nothing_to_snapshot(
        self, repo: str, sha: str, note: str
    ) -> SnapshotArchive:
        """В снимке нет ни одного файла: ПУСТОЕ ДЕРЕВО или «всё исключено».

        Это разные факты, и ревьюеру нельзя говорить первое, когда верно второе
        (находка Codex): коммит из одной символьной ссылки или дерево целиком
        под ``export-ignore`` не пусты, а отфильтрованы.
        """
        if note:
            return SnapshotArchive(
                sha=sha,
                data=None,
                note=note,
                reason=f"в снимке не осталось файлов: {note}",
            )
        rc, listed, _ = await proc.run_capped(
            "git",
            "-C",
            repo,
            "ls-tree",
            "-r",
            "--name-only",
            "-z",
            sha,
            cwd=repo,
            timeout=30,
            max_bytes=1 << 20,
        )
        if rc == 0 and listed:
            return SnapshotArchive(
                sha=sha,
                data=None,
                reason="в дереве коммита есть файлы, но git archive исключил их "
                "все (export-ignore): снимок пуст не потому, что пусто дерево",
            )
        if rc != 0:
            return SnapshotArchive(
                sha=sha, data=None, reason="дерево коммита не прочитано (ls-tree)"
            )
        return SnapshotArchive(
            sha=sha, data=None, empty=True, reason="дерево на закреплённом sha пусто"
        )

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

    def _history_run(self, repo: str) -> git_history.GitRun:
        """``_git`` on ``repo`` for the history reads of HistoryReads (#1865)."""
        return partial(_git, repo=repo, check=False)

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

    async def unpushed_commits(self, branch: str, repo: str | None = None) -> int:
        """Локальные коммиты ветки, которых нет в origin/<ветке> (0 — нет или не прочитать)."""
        return await _count_commits(f"origin/{branch}..{branch}", repo or _repo_root())

    async def branch_diff_paths(
        self,
        branch: str,
        base_branch: str | None = None,
        repo: str | None = None,
    ) -> list[str] | None:
        """Files the branch changes relative to its base, or None if unknowable.

        None and [] are deliberately different: [] means "the branch changes
        nothing", None means "we could not find out"; collapsing them is how a
        check reports agreement it never established.

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

    async def branch_touched_paths(
        self,
        branch: str,
        base_branch: str | None = None,
        repo: str | None = None,
        head_sha: str = "",
    ) -> tuple[list[str], str] | None:
        """Every path the branch touches, BOTH ends of a rename (#1589).

        A separate call from ``branch_diff_paths`` on purpose: that one feeds
        the surface check and the risk recompute, which compare the NEW name
        only, and must not change behaviour. This one answers a different
        question — "which watched path did the change touch" — and a file
        moved out of a watched directory touches it, as does a deletion.

        Returns ``(paths, sha_read)`` or None when unknowable. ``sha_read`` is
        the commit the paths were actually read for: ``head_sha`` when the
        submission pinned one, else the branch tip resolved origin-first. No
        fetch here: the caller has just run ``branch_diff_paths``, which does.
        ``-M`` is explicit so the answer does not hang on ``diff.renames``;
        ``-z`` for the same reason as everywhere else (#555).
        """
        repo = repo or _repo_root()
        base = _resolve_base(base_branch)
        if not (branch or "").strip():
            return None
        base_sha = await _resolve_ref_remote_first(base, repo)
        if base_sha is None:
            return None
        head = (head_sha or "").strip() or await _resolve_ref_remote_first(branch, repo)
        if not head:
            return None
        rc, out, err = await _git(
            "diff",
            "--name-status",
            "-z",
            "-M",
            f"{base_sha}...{head}",
            repo=repo,
            check=False,
        )
        if rc != 0:
            log.warning(
                "branch_touched_paths: diff failed for %s...%s: %s",
                base_sha,
                head,
                (err or "").strip(),
            )
            return None
        return parse_name_status_paths(out or ""), head

    async def auto_commit(
        self,
        task_id: int,
        title: str = "",
        message: str | None = None,
        repo: str | None = None,
        expected_branch: str | None = None,
    ) -> bool:
        """Commit the task's work. Refuses to commit onto a foreign branch.

        ``expected_branch`` (#361 I1): the done-pipeline checks out the task
        branch and then calls this; a failed checkout (a dirty tree is enough)
        left the commit landing on whichever branch was still current. With the
        branch named, a mismatch refuses instead.

        The blanket ``git add -A`` stages whatever is dirty; do NOT read that as
        "everything dirty belongs to the task" (the tree was clean only at t=0,
        and a headless task shares the main clone). Attribution lives in the
        commit-scope gate (hub/commit_scope.py), which compares the dirty set
        against the task's declared affected_areas before this is called.
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

        The branch is the task's base, not the literal "main" (#552): since
        #362 work is cut from PAIR_BASE_BRANCH, so naming main left the clone
        parked on the task branch and pulled a branch nobody builds on.

        A failed checkout raises rather than falling through to the pull, so
        the caller can tell "returned to base" from "still on someone's branch".
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

        The squash point must be the branch's base — the one create_branch cut
        from and create_pr targets, not a hardcoded origin/main: with the cut on
        the integration branch (#362), merge-base against origin/main is where
        the two forked, so the reset swallowed every commit the base had gained
        since, showing the base's work as the task's and raising add/add
        conflicts on merge.
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

        # #1865: строки отката переживают сжатие (до reset коммиты ещё видны).
        trailer = await git_history.revert_lines(
            self._history_run(repo), f"{merge_base.strip()}..HEAD"
        )
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
            *(["-m", trailer] if trailer else []),
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
        forge: str = "",
        base_branch: str | None = None,
    ) -> int | None:
        ctype = _conv_commit_type(title)
        pr_title = f"{ctype}(task): {title} (#{task_id})"
        body = (
            f"## Task #{task_id}\n\n"
            f"{description or 'No description'}\n\n"
            "---\n*Created automatically by Haiplane Hub*"
        )
        return await self._forge_for(forge).create_pr(
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
        forge: str = "",
    ) -> dict[str, Any]:
        return await self._forge_for(forge).ci_failure_logs(
            pr_number, branch, max_log_chars, repo=repo, gh_repo=gh_repo
        )

    async def branch_ci_runs(
        self,
        branch: str,
        limit: int = 20,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> list[dict[str, Any]] | None:
        return await self._forge_for(forge).branch_ci_runs(
            branch, limit, repo=repo, gh_repo=gh_repo
        )

    async def ci_run_attempt(
        self,
        run_id: str,
        attempt: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> dict[str, Any] | None:
        return await self._forge_for(forge).ci_run_attempt(
            run_id, attempt, repo=repo, gh_repo=gh_repo
        )

    async def ci_runs_for_sha(
        self,
        head_sha: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
        workflow_path: str = ".github/workflows/ci.yml",
    ) -> list[dict[str, Any]] | None:
        return await self._forge_for(forge).ci_runs_for_sha(
            head_sha, repo=repo, gh_repo=gh_repo, workflow_path=workflow_path
        )

    async def ci_run_artifacts(
        self,
        run_id: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> list[dict[str, Any]] | None:
        return await self._forge_for(forge).ci_run_artifacts(
            run_id, repo=repo, gh_repo=gh_repo
        )

    async def ci_artifact_zip(
        self,
        artifact_id: str,
        max_bytes: int,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> bytes | None:
        return await self._forge_for(forge).ci_artifact_zip(
            artifact_id, max_bytes=max_bytes, repo=repo, gh_repo=gh_repo
        )

    async def check_pr_ci(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> CIProbeResult:
        return await self._forge_for(forge).check_pr_ci(
            pr_number, repo=repo, gh_repo=gh_repo
        )

    async def request_ci_run(
        self,
        branch: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
        workflow_path: str | None = None,
        on_send: Callable[[], None] | None = None,
    ) -> CIRunRequestResult:
        """Попросить форж о прогоне на вершине ``branch`` (#1197).

        ``on_send`` (#1914) вызывается ровно перед отправкой платного запроса:
        до него отказ или таймаут не тратят попытку.

        Роутинг тот же, что у всех остальных вопросов к форжу: кто умеет,
        тот отвечает, а кто не умеет — говорит об этом словом
        ``unsupported``. Ветвления по имени форжа здесь нет намеренно —
        оно жило бы в вызывающем и разъезжалось бы с реализациями.
        """
        return await self._forge_for(forge).request_ci_run(
            branch,
            repo=repo,
            gh_repo=gh_repo,
            workflow_path=workflow_path,
            on_send=on_send,
        )

    async def remote_heads(
        self, branches: Sequence[str], repo: str | None = None
    ) -> dict[str, str] | None:
        """Heads of ``origin`` branches in ONE ``ls-remote`` (#1862).

        ``{branch: sha}`` for the branches origin has; a branch it lacks is
        simply absent. None when origin did not answer: "could not look" is not
        "no branches", and the caller must tell the two apart.
        """
        names = [b for b in dict.fromkeys(branches) if b]
        if not names:
            return {}
        rc, out, _err = await _git(
            "ls-remote",
            "--heads",
            "origin",
            *[f"refs/heads/{b}" for b in names],
            repo=repo or _repo_root(),
            check=False,
            timeout=60,
        )
        if rc != 0:
            return None
        found: dict[str, str] = {}
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/") and sha.strip():
                found[ref[len("refs/heads/") :]] = sha.strip()
        return found

    async def pr_for_branch(
        self,
        branch: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> int | None:
        """The open PR whose head is ``branch``, or None (#605).

        The pair flow never records pr_number (only the headless create_pr
        paths do), so the hub looks the PR up itself at submission time rather
        than key the delivery gate on a field nobody sets.
        """
        return await self._forge_for(forge).pr_for_branch(
            branch, repo=repo, gh_repo=gh_repo
        )

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

        Exists because counting commits stopped answering this: a squash
        release writes a new commit on the release branch, so ``base..head``
        never empties and every cycle looked like undelivered work (twenty
        release PRs in ninety minutes, nineteen empty, each redeploying
        production). Trees are how git states "the same content": compare them.
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
        forge: str = "",
    ) -> tuple[MergeabilityOutcome, str]:
        """Can this PR be merged, and if not, why (#970).

        The forge answers the outcome; the FILES of a conflict come from the
        clone, because only git can name them: the forge's 409 names nothing
        and a reason has to lead somewhere (same question as #969).
        """
        outcome, detail = await self._forge_for(forge).pr_mergeability(
            pr_number, repo=repo, gh_repo=gh_repo
        )
        # Форж, который не умеет мержить, обычно не умеет и предсказывать мерж
        # (#1116). Тогда вопрос задаётся git: пробное слияние в одноразовом
        # рабочем дереве — единственный способ узнать ответ, не тронув ничего.
        if outcome is MergeabilityOutcome.unavailable and not (
            self._forge_for(forge).can_merge_via_api
        ):
            return await self._mergeable_by_trial(
                pr_number, repo=repo, gh_repo=gh_repo, forge=forge
            )
        if outcome is not MergeabilityOutcome.conflicting:
            return (outcome, detail)
        files = await self._conflicting_files_of_pr(
            pr_number, repo=repo, gh_repo=gh_repo, forge=forge
        )
        named = f": {', '.join(files)}" if files else ""
        return (MergeabilityOutcome.conflicting, f"конфликт с базовой веткой{named}")

    async def _mergeable_by_trial(
        self,
        pr_number: int,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
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
        base, head = await self._forge_for(forge).pr_refs(
            pr_number, repo=repo, gh_repo=gh_repo
        )
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
        forge: str = "",
    ) -> list[str]:
        """Files this PR collides with its base on, when git can name them.

        Best effort by the same contract as ``_conflicting_files`` (#969):
        empty means "could not name them", never "there were none". The
        branch names come from the PR itself, so this works for a task PR as
        well as for the release one.
        """
        base, head = await self._forge_for(forge).pr_refs(
            pr_number, repo=repo, gh_repo=gh_repo
        )
        if not base or not head:
            return []
        return await self._conflicting_files(head, base, repo=repo)

    async def open_pr_between(
        self,
        base: str,
        head: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> tuple[int | None, str]:
        """The open PR ``head`` → ``base`` from this repository (#1426).

        ``(number, "")``, ``(None, "")`` when there is none, ``(None, reason)``
        when the list could not be read or only a stranger's PR matched. The
        forge filters by base AND head, and accepts only a head that lives in
        this repository: a fork's ``main`` is not the release branch, and a PR
        from main into anything but ``base`` is not the return.
        """
        return await self._forge_for(forge).pr_between(
            base, head, repo=repo, gh_repo=gh_repo
        )

    async def open_return_pr(
        self,
        base: str,
        head: str,
        title: str,
        body: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> tuple[int | None, str]:
        """Find the return PR ``head`` → ``base``, or create exactly that one (#1426).

        Not ``open_release_pr``: that one finds a PR by head alone and edits
        it, so an open PR main → staging would have been renamed into "the
        return" while main → develop was never created. Here nobody else's PR
        is touched. A lookup that failed stops the call — creating blindly on
        "could not read" is how a second PR appears (#516).

        The number ``create_pr`` answers is not trusted either: on "already
        exists" it falls back to a head-only lookup. The PR is looked up again
        by base and head instead, and that answer is the result.
        """
        adapter = self._forge_for(forge)
        found, why = await adapter.pr_between(base, head, repo=repo, gh_repo=gh_repo)
        # Чужой PR с той же парой — не сбой чтения: свой всё равно создаётся,
        # иначе любой форк с веткой main останавливает возврат.
        if found or (why and not why.startswith(FOREIGN_PR_ONLY)):
            return (found, why)
        await adapter.create_pr(title, body, head, base, repo=repo, gh_repo=gh_repo)
        found, why = await adapter.pr_between(base, head, repo=repo, gh_repo=gh_repo)
        if found or why:
            return (found, why)
        return (None, "форж не создал PR")

    async def merge_return_pr(
        self,
        pr_number: int,
        subject: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> tuple[bool, str]:
        """Merge the release-return PR: ``(True, merge sha)`` or ``(False, why)``.

        Replaces the direct branch merge of #969 (#1426). That merge went
        through ``POST /merges`` with no PR, and the ruleset's pull_request
        rule let it through only because the Write role could bypass it — a
        bypass every writer had, not only the hub. That was true before
        07.10.2026, when the Write role was dropped from the bypass with no
        replacement (#1426) — the ruleset was left with no bypass at all.
        Separately and later, on 16.10.2026 (#1708), a narrower ruleset was
        added whose only bypass is the gate's own identity (``haiplane-gate``),
        and only on that ruleset's update rule, not on
        ``pull_request``/``required_status_checks`` — see
        ``docs/agent-context/invariants.md``, «Личность вливания». As a PR
        merged at green CI the return needs no bypass of the PR/CI
        requirements.

        Two things differ from a task merge, and both are load-bearing:

        * a MERGE COMMIT, not a squash. The point of the return is to make
          the release commit an ancestor of the integration branch; a squash
          would write one more new commit and leave the drift in place;
        * the head is never deleted. The head of this PR is the release
          branch — main.

        A forge that cannot merge through its API merges by push, which is a
        ``--no-ff`` merge already and never deletes a branch (#1116).
        The merge sha may come back empty when the forge would not name it;
        the merge still happened, and the caller records it keyed by the PR.
        """
        adapter = self._forge_for(forge)
        if not adapter.can_merge_via_api:
            return await self.merge_pr_by_push(
                pr_number, subject, repo=repo, gh_repo=gh_repo, forge=forge
            )
        merged = await adapter.merge_pr(
            pr_number,
            subject,
            delete_branch=False,
            repo=repo,
            gh_repo=gh_repo,
            method="merge",
        )
        if not merged:
            return (False, "")
        sha = await adapter.merge_commit_sha(pr_number, repo=repo, gh_repo=gh_repo)
        return (True, sha)

    async def _conflicting_files(
        self, base: str, head: str, repo: str | None = None
    ) -> list[str]:
        """Which files a conflicting back-merge collides on, if git can say.

        Best effort by contract: GitHub's 409 names nothing, and the local
        clone may be absent, stale, or running a git too old for
        ``merge-tree --write-tree``. Empty means "could not name them", never
        "there were none" — the caller reports the conflict either way: a
        conflict without a file list is still one somebody must resolve.
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

    async def _prepare_base_merge_tree(
        self, repo: str, base: str, branch: str, task_id: int, tip: str = ""
    ) -> tuple[str, str]:
        """Одноразовое дерево на ЗАКРЕПЛЁННОМ коммите со слитой в него базой (#1233).

        Возвращает ``(путь, причина)``: путь непустой — мерж выполнен (чисто
        или с конфликтами, это спрашивают отдельно), непустая причина — спросить
        не удалось. Разметка diff3 включена НАРОЧНО: без секции общего предка
        нельзя доказать, что ни одна сторона не переписала существующую строку,
        а без этого доказательства автомерж не имеет права срабатывать.

        ``tip`` — коммит, НА КОТОРОМ строится дерево, и он обязан быть тем самым
        закреплённым коммитом сдачи, а не ``origin/<branch>``: вершина ветки
        «какой она стала» тащила пуш из окна ожидания в слитый коммит и в
        перезакрепление, и человеческий вердикт переезжал на код, которого
        человек не видел (tests/test_delivery_gate.py).
        """
        rc, _, err = await _git(
            "fetch",
            "origin",
            f"+{base}:refs/remotes/origin/{base}",
            f"+{branch}:refs/remotes/origin/{branch}",
            repo=repo,
            check=False,
        )
        if rc != 0:
            return "", f"не удалось получить ветки из origin: {err[:150]}"
        if not (tip or "").strip():
            # Без закрепления опереться не на что: строить на вершине ветки
            # значило бы согласиться слить что угодно, что туда приехало.
            return "", (
                "автомерж не на чем закрепить: коммит сдачи не закреплён, "
                "а вершину ветки автомерж не берёт (#1233)"
            )
        tip = tip.strip()
        rc, moved, _ = await _git(
            "rev-parse", f"origin/{branch}", repo=repo, check=False
        )
        if rc != 0:
            return "", f"не удалось прочитать вершину ветки {branch}"
        moved = (moved or "").strip()
        if moved != tip:
            # Ветка уехала с закреплённого коммита. Слить базу в закрепление и
            # запушить значило бы затереть чужой пуш, а слить в вершину —
            # перенести вердикт на неодобренное. Оба выхода хуже человека.
            return "", (
                f"ветка {branch} ушла с закреплённого коммита {tip[:12]} на "
                f"{moved[:12]} — автомерж не трогает то, чего не одобряли"
            )
        path = _scratch_worktree(repo, "basemerge", task_id)
        await _git("worktree", "remove", "--force", path, repo=repo, check=False)
        rc, _, err = await _git(
            "worktree",
            "add",
            "--force",
            "--detach",
            path,
            tip,
            repo=repo,
            check=False,
        )
        if rc != 0:
            return "", f"не удалось подготовить дерево для мержа базы: {err[:150]}"
        try:
            rc, _, err = await _git(
                "-c",
                "merge.conflictStyle=diff3",
                "merge",
                "--no-commit",
                "--no-ff",
                f"origin/{base}",
                repo=path,
                check=False,
            )
        except BaseException:
            # Вызывающие чистят дерево, только когда путь им вернули: отмена
            # посреди мержа (гашение поллера) иначе бросила бы его с недомержем.
            await _git("worktree", "remove", "--force", path, repo=repo, check=False)
            raise
        if rc == _TIMEOUT_RC or rc >= 128:
            # Таймаут и падение самого git — «спросить не удалось», а не
            # конфликт: лечится повтором, а не человеком (#970, #1116).
            await _git("worktree", "remove", "--force", path, repo=repo, check=False)
            return "", f"мерж базы не состоялся (git rc={rc}): {err[:150]}"
        return path, ""

    @staticmethod
    async def _conflicting_texts(path: str) -> tuple[dict[str, str] | None, str]:
        """Конфликтующие файлы готового дерева и их текст С МАРКЕРАМИ diff3.

        Один вход для пробы и для пуша: класс конфликта судится по одной и той
        же разметке в обоих местах, и сверять их между собой можно байт в байт
        (#1233, находка 2327bd9255c601cc). ``None`` — спросить не удалось.
        """
        rc, out, _ = await _git(
            "diff", "--name-only", "--diff-filter=U", repo=path, check=False
        )
        if rc != 0:
            return None, "не удалось перечислить конфликтующие файлы"
        paths = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
        files: dict[str, str] = {}
        for rel in paths[:20]:
            try:
                with open(os.path.join(path, rel), encoding="utf-8") as fh:
                    files[rel] = fh.read()
            except (OSError, UnicodeDecodeError) as exc:
                # Двоичный или нечитаемый файл — не наш класс, и молчать
                # об этом нельзя: пустой словарь читается как «чисто».
                return None, f"конфликтующий файл {rel} не прочитан: {exc}"
        return files, ""

    async def pr_base_freshness(
        self,
        pr_number: int,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> BaseFreshness:
        """Отстала ли голова PR от базы — вопрос форжу, роутинг как у всех (#1419)."""
        return await self._forge_for(forge).pr_base_freshness(
            pr_number, repo=repo, gh_repo=gh_repo
        )

    async def update_pr_branch(
        self,
        pr_number: int,
        expected_head_sha: str,
        *,
        task_id: int = 0,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> tuple[BranchUpdateOutcome, str]:
        """Слить базу в ветку PR с арендой головы (#1419).

        Форж с вызовом (GitHub update-branch) делает это сам. Форж без него
        отвечает ``unsupported``, и тогда база сливается здесь локальным git в
        одноразовом дереве на ``expected_head_sha`` и пушится с арендой на неё
        же — та же механика, что у автомержа #1233, без разрешения конфликтов:
        конфликт здесь не разрешается, а называется.
        """
        adapter = self._forge_for(forge)
        outcome, detail = await adapter.update_pr_branch(
            pr_number, expected_head_sha, repo=repo, gh_repo=gh_repo
        )
        if outcome is BranchUpdateOutcome.conflict:
            # Форж конфликт не расписывает по файлам — их называет клон (#970).
            files = await self._conflicting_files_of_pr(
                pr_number, repo=repo, gh_repo=gh_repo, forge=forge
            )
            return (outcome, ", ".join(files) if files else detail)
        if outcome is not BranchUpdateOutcome.unsupported or not repo:
            return (outcome, detail)
        base, head = await adapter.pr_refs(pr_number, repo=repo, gh_repo=gh_repo)
        if not base or not head:
            return (
                BranchUpdateOutcome.unavailable,
                f"ветки PR #{pr_number} не прочитаны",
            )
        return await self._update_branch_by_push(
            repo, base, head, task_id, expected_head_sha
        )

    async def _update_branch_by_push(
        self, repo: str, base: str, branch: str, task_id: int, tip: str
    ) -> tuple[BranchUpdateOutcome, str]:
        """Слить ``origin/<base>`` в ветку на ``tip`` и запушить с арендой (#1419)."""
        path, why = await self._prepare_base_merge_tree(
            repo, base, branch, task_id, tip
        )
        if not path:
            return (BranchUpdateOutcome.unavailable, why)
        try:
            files, why = await self._conflicting_texts(path)
            if files is None:
                return (BranchUpdateOutcome.unavailable, why)
            if files:
                return (BranchUpdateOutcome.conflict, ", ".join(sorted(files)))
            rc, _, err = await _git(
                "commit",
                "-m",
                f"chore(task-{task_id}): merge {base} into {branch}",
                "--no-verify",
                repo=path,
                check=False,
            )
            if rc != 0:
                return (BranchUpdateOutcome.unavailable, f"коммит мержа: {err[:150]}")
            # #949/#1438: пуш через временную ветку по шаблону хука, аренда на
            # закреплённую голову; временная убирается всегда.
            _, merged, _ = await _git("rev-parse", "HEAD", repo=path, check=False)
            temp = f"task-{task_id}/gate-update-{(merged or '').strip()[:8]}"
            rc, err = await _push_tree_head(repo, path, temp, branch, lease=tip)
            if rc != 0:
                return (_push_refusal(rc, err), f"пуш ветки: {(err or '')[:150]}")
            return (BranchUpdateOutcome.updated, f"база {base} слита в {branch}")
        finally:
            await _git("merge", "--abort", repo=path, check=False)
            await _git("worktree", "remove", "--force", path, repo=repo, check=False)

    async def base_merge_conflicts(
        self, repo: str, base: str, branch: str, task_id: int, tip: str = ""
    ) -> tuple[dict[str, str] | None, str]:
        """Что помешает слить базу в ветку — пробой, которая ничего не меняет (#1233).

        ``({}, "")`` — база сливается чисто. Непустой словарь — путь файла и его
        содержимое С МАРКЕРАМИ diff3, чтобы класс конфликта судили по разметке
        git, а не по догадке. ``None`` — спросить не удалось, и это НЕ «конфликта
        нет»: одно чинится повтором, другое руками человека (#725).

        ``tip`` — закреплённый коммит сдачи: пробу задают о ТОМ коде, который
        одобряли, а не о вершине ветки.
        """
        path, why = await self._prepare_base_merge_tree(
            repo, base, branch, task_id, tip
        )
        if not path:
            return None, why
        try:
            return await self._conflicting_texts(path)
        finally:
            await _git("merge", "--abort", repo=path, check=False)
            await _git("worktree", "remove", "--force", path, repo=repo, check=False)

    async def push_resolved_base_merge(
        self,
        repo: str,
        base: str,
        branch: str,
        task_id: int,
        resolutions: dict[str, str],
        validate: Any = None,
        tip: str = "",
        probed: dict[str, str] | None = None,
    ) -> tuple[bool, str]:
        """Слить базу в ветку с готовым разрешением и запушить (#1233, AC-3).

        ``validate`` — асинхронный вызов ``(путь) -> (rc, лог) | None``, который
        гонится в том же дереве ПОСЛЕ разрешения. Судим по коду возврата: 09.09
        дважды наблюдали «All checks passed» при коде 2, и хвост вывода здесь
        не доказательство. Валидации нет или она не запустилась — не пушим:
        сложить два блока текста мало, они могут конфликтовать по именам.

        ``tip`` — закреплённый коммит сдачи. Дерево строится на нём, и пуш идёт
        с арендой (``--force-with-lease``) на то же значение: если ветка уехала
        между пробой и пушем, пуш обязан отказать, а не затереть чужой коммит.

        ``probed`` — конфликтующие файлы ТОЙ САМОЙ пробы, по которой считалось
        ``resolutions``. Аренда стережёт ветку задачи, но не базу, а дерево
        строится заново и сливает уже НОВЫЙ ``origin/<base>``: без сверки байты
        разрешения, посчитанные на старой базе, легли бы поверх свежих файлов,
        ``git add`` снял бы с них признак U, и строки, приехавшие в базу между
        пробой и пушем, пропали бы молча (#1233). Поэтому конфликт
        пересчитывается и сверяется байт в байт; разошёлся — отказ, а не догадка.
        """
        path, why = await self._prepare_base_merge_tree(
            repo, base, branch, task_id, tip
        )
        if not path:
            return False, why
        try:
            if probed is None:
                # Разрешение без пробы сверить не с чем, а применять его
                # вслепую — ровно тот отказ, ради которого сверка и заведена.
                return False, "разрешение конфликта не с чем сверить: пробы нет"
            fresh, why = await self._conflicting_texts(path)
            if fresh is None:
                return False, f"конфликт не пересчитан: {why}"
            if fresh != probed:
                gone = sorted(set(probed) - set(fresh))
                new = sorted(set(fresh) - set(probed))
                changed = sorted(
                    rel for rel in set(fresh) & set(probed) if fresh[rel] != probed[rel]
                )
                return False, (
                    "база сдвинулась между пробой и пушем: конфликт пересчитан "
                    "и не совпал с тем, по которому считалось разрешение "
                    f"(ушли: {', '.join(gone) or '—'}; появились: "
                    f"{', '.join(new) or '—'}; изменились: "
                    f"{', '.join(changed) or '—'}) — старые байты на новое "
                    "дерево не кладём"
                )
            for rel, text in resolutions.items():
                target = os.path.join(path, rel)
                if not os.path.realpath(target).startswith(
                    os.path.realpath(path) + os.sep
                ):
                    return False, f"путь {rel} ведёт наружу рабочего дерева"
                with open(target, "w", encoding="utf-8") as fh:
                    fh.write(text)
                await _git("add", "--", rel, repo=path, check=False)
            rc, out, _ = await _git(
                "diff", "--name-only", "--diff-filter=U", repo=path, check=False
            )
            left = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
            if rc != 0 or left:
                return False, (
                    "после разрешения остались конфликтующие файлы: "
                    + (", ".join(left) or "перечислить не удалось")
                )
            if validate is None:
                return False, "валидацию после автомержа гонять нечем"
            result = await validate(path)
            if result is None:
                return False, "валидация после автомержа не запустилась"
            rc, log_tail = result
            if rc != 0:
                return False, _automerge_check_refusal(rc, log_tail)
            rc, _, err = await _git(
                "commit",
                "-m",
                f"chore(task-{task_id}): merge {base} into {branch}",
                "--no-verify",
                repo=path,
                check=False,
            )
            if rc != 0:
                return False, f"коммит мержа не состоялся: {err[:150]}"
            # Коммит слитой ветки читается ДО пуша: закрепление сдачи будет
            # переставлено именно на него, и переставлять его вслепую нельзя.
            rc, sha, _ = await _git("rev-parse", "HEAD", repo=path, check=False)
            sha = (sha or "").strip()
            if rc != 0 or not sha:
                # Пустой sha стёр бы закрепление в submission_sha, а пустое
                # закрепление гейт читает как «сверка не проводилась» и доставляет
                # без неё (#572): потеря закрепления хуже несостоявшегося автомержа.
                return False, "коммит слитой ветки не прочитан — закрепление не трогаем"
            # Аренда — это сравнение-и-запись: пуш пройдёт, только если ветка
            # ВСЁ ЕЩЁ стоит на закреплённом коммите. Дерево тоже построено на
            # нём, так что это обычный fast-forward; аренда закрывает окно между
            # пробой и пушем, где чужой коммит успел бы лечь под наш вердикт.
            # Пустой ``tip`` сюда не доходит: дерево на нём и строится.
            # #1438: не ``HEAD:refs/heads/…`` — его режет вооружённый хук.
            rc, err = await _push_tree_head(
                repo, path, f"task-{task_id}/gate-merge-{sha[:8]}", branch, lease=tip
            )
            if rc != 0:
                return False, f"пуш слитой ветки не прошёл: {err[:150]}"
            return True, sha
        finally:
            await _git("merge", "--abort", repo=path, check=False)
            await _git("worktree", "remove", "--force", path, repo=repo, check=False)

    async def release_range(
        self,
        base: str,
        head: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> list[str]:
        """Commit subjects that ``head`` carries over ``base``, newest first.

        The subjects are how the release learns what it is carrying: the
        delivery gate writes «feat(task): …-(#NNN)», so the numbers can be
        read back out. Empty means nothing to release — or that git could not
        answer, and the caller treats those the same way it treats an empty
        range: by doing nothing.
        """
        return await self._forge_for(forge).compare_subjects(
            base, head, repo=repo, gh_repo=gh_repo
        )

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

        Not inside ``release_range``: its other consumer
        (``red_base._commits_between``) needs the full interval (#972 AC-6).
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
        forge: str = "",
    ) -> int | None:
        """The open release PR for this range — found, updated, or created."""
        return await self._forge_for(forge).open_or_update_pr(
            base, head, title, body, repo=repo, gh_repo=gh_repo
        )

    async def pr_state(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> str:
        """Where this PR stands: "open", "merged", "closed", "absent", or "".

        Empty means the question could not be asked — no forge, no network, an
        unreadable answer. The caller treats that as a cause to report, never
        as an answer: "could not look" and "closed" lead to opposite decisions
        about delivery (#802, the rule #725 wrote down).
        """
        return await self._forge_for(forge).pr_state(
            pr_number, repo=repo, gh_repo=gh_repo
        )

    async def pr_is_draft(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> bool:
        """Whether the forge still treats this PR as a draft (#1053).

        False means "not a draft or could not look" — the #498 rule: silence
        is not an accusation, and the merge call still runs.
        """
        return await self._forge_for(forge).pr_is_draft(
            pr_number, repo=repo, gh_repo=gh_repo
        )

    async def mark_pr_ready(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> bool:
        """Convert a draft PR to ready. Hub approval is the ready signal (#1053)."""
        return await self._forge_for(forge).mark_pr_ready(
            pr_number, repo=repo, gh_repo=gh_repo
        )

    async def merge_commit_sha(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> str:
        """The SHA of the commit THIS pull request produced, or "" (#534).

        Not the tip of the base branch. The tip is whatever landed last, and
        between the merge and the read a direct push can land — which would
        write the intruder into the whitelist and mark the real merge as
        drift. The pull request knows its own merge commit, so ask it.
        """
        return await self._forge_for(forge).merge_commit_sha(
            pr_number, repo=repo, gh_repo=gh_repo
        )

    async def pr_for_merge_commit(
        self,
        sha: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> dict[str, Any] | None:
        """The merged PR whose merge commit IS ``sha``, or None (#1367).

        Read-only. Returns ``{"number", "head", "merge_sha"}``. Raises when
        the provider could not be asked — the caller names that as a reason
        rather than reading it as "no such PR". A forge that cannot answer
        this question (GitVerse, none configured) raises too, for that reason.
        """
        return await self._forge_for(forge).pr_for_merge_commit(
            sha, repo=repo, gh_repo=gh_repo
        )

    async def pr_head_sha(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> str:
        """Голова PR на форже; пусто, когда спросить не удалось (#1591)."""
        return await self._forge_for(forge).pr_head_sha(
            pr_number, repo=repo, gh_repo=gh_repo
        )

    def merge_preserves_ancestry(self, forge: str = "") -> bool:
        """Останется ли сдаточный коммит предком базовой ветки после доставки.

        Спрашивается у ТОГО ЖЕ адаптера, который будет мержить (``_forge_for``
        с тем же именем форжа, что у ``merge_pr``), а не у настройки рядом и не
        у имени в строке: два источника правды разъехались бы.

        Синхронный и без обращений наружу: объявленное свойство конвейера, а
        не наблюдение, поэтому потребитель спрашивает ПЕРЕД git-вызовом (#1214).
        """
        return bool(self._forge_for(forge).merge_preserves_ancestry)

    async def merge_pr(
        self,
        pr_number: int,
        task_id: int,
        title: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
        delete_branch: bool = True,
        expected_head_sha: str = "",
    ) -> bool:
        """Merge one PR; ``delete_branch`` says what happens to its head (#949).

        ``expected_head_sha`` (#1591) делает merge УСЛОВНЫМ: форж сливает только
        если голова PR всё ещё этот коммит. Пусто — как раньше.

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
            forge=forge,
            delete_branch=delete_branch,
            expected_head_sha=expected_head_sha,
        )
        return ok

    async def merge_pr_with_detail(
        self,
        pr_number: int,
        task_id: int,
        title: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
        delete_branch: bool = True,
        expected_head_sha: str = "",
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
        if self._forge_for(forge).can_merge_via_api:
            ok = await self._forge_for(forge).merge_pr(
                pr_number,
                subject,
                delete_branch=delete_branch,
                repo=repo,
                gh_repo=gh_repo,
                expected_head_sha=expected_head_sha,
            )
            return (ok, "")
        ok, detail = await self.merge_pr_by_push(
            pr_number,
            subject,
            repo=repo,
            gh_repo=gh_repo,
            forge=forge,
            expected_head_sha=expected_head_sha,
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
        forge: str = "",
        expected_head_sha: str = "",
    ) -> tuple[bool, str]:
        """Слить PR локальным git и ДОКАЗАТЬ доставку базовой веткой (#1116).

        Для форжей, которые не умеют мержить сами. Возвращает ``(ok, detail)``
        — деталь называет ПРИЧИНУ, а не актора: защита базовой ветки, конфликт,
        отсутствие клона и недоступная сеть чинятся разными руками, и «форж
        отказал» не ведёт никуда (#970).

        Порядок шагов не произволен, каждый закрывает свой способ соврать:

        1. Ветки берутся у PR, а не у вызывающего: имя ветки задачи могло
           разойтись с тем, что в PR на самом деле.
        2. Мерж делается в ОДНОРАЗОВОМ worktree, а не в рабочем клоне: клон
           общий, может стоять на чужой ветке с грязным деревом и несёт
           взведённый pre-push хук (#949).
        3. Доставка подтверждается достижимостью полученного SHA в базовой
           ветке НА REMOTE, а не кодом возврата push: GitVerse после такого
           мержа оставляет PR open, merged=False, а GET /pulls/{n}/merge
           отвечает 404 и до, и после — об успехе форж не сообщает вовсе.
        4. Только после подтверждения PR закрывается явно: незакрытый висел бы
           открытым, и pr_for_branch находил бы его на доставленной ветке.
        """
        workspace = repo or _repo_root()
        base, head = await self._forge_for(forge).pr_refs(
            pr_number, repo=repo, gh_repo=gh_repo
        )
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
        if expected_head_sha:
            _, fetched, _ = await _git(
                "rev-parse", f"refs/remotes/origin/{head}", repo=workspace, check=False
            )
            if (fetched or "").strip() != expected_head_sha:
                return (
                    False,
                    f"голова {head} ушла с {expected_head_sha[:12]} (#1591): "
                    "мерж отменён, сверка повторится",
                )

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
                # Конфликт РАСПОЗНАЁТСЯ по индексу, а не по коду возврата (rc=1
                # отдают и другие отказы) и не по тексту (git локализован:
                # «КОНФЛИКТ» слово conflict не найдёт). Неслитые пути в индексе
                # бывают только у настоящего конфликта и они же — список файлов.
                _, unmerged, _ = await _git(
                    "diff",
                    "--name-only",
                    "--diff-filter=U",
                    repo=path,
                    check=False,
                )
                files = [f for f in (unmerged or "").split("\n") if f.strip()]
                if files:
                    return (
                        False,
                        f"{head} не сливается с {base} без конфликта: "
                        f"{', '.join(files)}",
                    )
                # Любой другой отказ мержа НЕ конфликт, и звать его конфликтом
                # дороже, чем не назвать причину вовсе: #1192 встал на том, что
                # `git merge` падал с «identity unknown» (rc=128), а хаб час
                # отправлял человека искать конфликт, которого не было. Вывод
                # git тут единственный, кто причину знает, — он и едет в деталь.
                said = (err or "").strip() or "git ничего не написал в stderr"
                return (
                    False,
                    f"мерж {head} в {base} не прошёл (git rc={rc}): {said[:150]}",
                )

            rc, merged_sha, _ = await _git("rev-parse", "HEAD", repo=path, check=False)
            merged_sha = (merged_sha or "").strip()
            if rc != 0 or not merged_sha:
                return (False, "git не назвал коммит мержа")

            # #1438: не ``HEAD:refs/heads/<база>`` — его режет вооружённый хук.
            rc, err = await _push_tree_head(
                workspace,
                path,
                f"chore/gate-merge-pr{pr_number}-{merged_sha[:8]}",
                base,
            )
            if rc != 0:
                detail = (err or "").strip()
                low, _ = _push_diagnostics(detail)
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

        landed = await self._forge_for(forge).branch_contains(
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
        if not await self._forge_for(forge).close_pr(
            pr_number, repo=repo, gh_repo=gh_repo
        ):
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

        The checkout must name the task's base, not the literal "main" (#552):
        git refuses to delete the branch it stands on. The success line is
        logged only when the deletion happened.
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

        ``forge`` НАЗЫВАЕТ ПЛОЩАДКУ (#1118): раньше обе строки-кандидата были
        захардкожены на github.com, и проект с ``forge=gitverse`` клонировался
        с GitHub и отчитывался ``provision_status=ok``.

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
            # Совпадения owner/name НЕ ХВАТАЕТ (#1118): «mrpda/snip-portal»
            # содержится и в github-адресе, и в gitverse-адресе, так что клон с
            # чужой площадки проходил проверку как годный, а статус оставался
            # зелёным навсегда.
            foreign = forge_registry.forge_of_host(_origin_host(origin))
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
            candidates = forge_registry.clone_urls(forge, repo_url)

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
        # activated (#532). Best effort: an unarmable hook never fails a clone.
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
        # Форж в детали не украшение (#1118): по строке без него нельзя понять,
        # с какой площадки взялся репозиторий.
        return True, f"cloned {repo_url} from {forge} ({base_branch}, {transport})"
