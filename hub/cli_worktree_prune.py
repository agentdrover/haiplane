"""``hp-hub worktree prune`` - уборка локальных копий доставленных задач (#2038).

Копию (git worktree) задачи создаёт ``hp-hub worktree N`` (#1515), убирать её
было нечем: на Mac набралось 363 копии на 69 ГБ. Здесь живёт вся уборка, чтобы
``hub/cli.py`` не рос.

Правило удаления - одно, и оно только про факты, которые можно проверить:
статус задачи ``completed``; доставку подтвердил сам хаб (``delivered`` из
``GET /api/tasks/N/delivery``, а не git cherry, равенство дерева или
merged_into_base); HEAD копии равен ``submission_sha`` или его предок; ``git
status`` ответил rc 0 и пусто. Всё остальное - «оставить» с названной причиной.

Что не прочиталось, то не «нет»: ошибка хаба или git даёт «статус не прочитан»
или «чистота не определена», копия остаётся, код возврата ненулевой. Ответы хаба
по ВСЕМ копиям собираются до первого удаления; прямо перед каждым удалением
ветка, HEAD и чистота проверяются заново. Удаляет только ``git worktree remove``
без ``--force``; локальные ветки и блокировки не трогаются.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess  # nosec B404 - только git, список аргументов, без shell
import sys
from dataclasses import dataclass
from typing import Any, Callable

GIT_TIMEOUT_S = 60
_TASK_DIR = re.compile(r"^task-(\d+)$")
_TASK_BRANCH = re.compile(r"^refs/heads/task-(\d+)/.+")
_STRAY_SIBLING = re.compile(r"-task-\d+$")

Api = Callable[..., Any]
Refusal = Callable[[dict[str, Any], dict[str, Any], str], str]


@dataclass
class Entry:
    """Одна запись ``git worktree list --porcelain``."""

    path: str
    branch: str = ""  # refs/heads/..., пусто у detached
    detached: bool = False
    locked: bool = False
    prunable: bool = False


@dataclass
class Verdict:
    """Решение по одной копии."""

    path: str
    task_id: int | None = None
    branch: str = ""
    sha: str = ""
    remove: bool = False
    reason: str = ""
    unread: bool = False  # причина - «не прочитано», а не «условие не выполнено»


def _git(cwd: Any, *args: str, **_kw: Any) -> tuple[int, str, str]:
    """``git -C cwd args``: (rc, stdout, stderr); сбой запуска - rc 127."""
    try:
        done = subprocess.run(  # nosec B603 B607
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return done.returncode, done.stdout, done.stderr


def _real(path: str) -> str:
    return os.path.realpath(path)


def _parse_porcelain(text: str) -> list[Entry]:
    entries: list[Entry] = []
    for block in text.split("\n\n"):
        entry: Entry | None = None
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                entry = Entry(path=value.strip())
            elif entry is None:
                continue
            elif key == "branch":
                entry.branch = value.strip()
            elif key == "detached":
                entry.detached = True
            elif key == "locked":
                entry.locked = True
            elif key == "prunable":
                entry.prunable = True
        if entry is not None:
            entries.append(entry)
    return entries


def _current_toplevel() -> str:
    rc, out, _ = _git(os.getcwd(), "rev-parse", "--show-toplevel")
    return _real(out.strip()) if rc == 0 and out.strip() else ""


def _canonical_id(path: str, clone: str) -> tuple[int | None, str]:
    """ID задачи из пути и причина, если путь не канонический (#459)."""
    from hub.integrations.git_ops import task_worktree_path

    match = _TASK_DIR.match(os.path.basename(path.rstrip("/")))
    expected_dir = os.path.dirname(task_worktree_path(0, clone))
    if match and _real(task_worktree_path(int(match.group(1)), clone)) == _real(path):
        return int(match.group(1)), ""
    return None, f"вне канонического пути {expected_dir}/task-N"


def _local_verdict(entry: Entry, clone: str, current: str, is_main: bool) -> Verdict:
    """Фаза 1: то, что видно без хаба. ``reason`` пусто - кандидат."""
    v = Verdict(path=entry.path)
    if is_main:
        v.reason = "основной worktree клона"
    elif _real(entry.path) == current:
        v.reason = "текущий worktree (из него запущена команда)"
    elif entry.prunable:
        v.reason = "git считает путь потерянным (prunable): нужен git worktree prune"
    elif entry.locked:
        v.reason = "заблокирован (git worktree lock); блокировку не снимаю"
    else:
        _identify(v, entry, clone)
    return v


def _identify(v: Verdict, entry: Entry, clone: str) -> None:
    v.task_id, v.reason = _canonical_id(entry.path, clone)
    if v.reason:
        return
    rc, out, _ = _git(
        entry.path, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    if rc != 0 or _real(out.strip()) != _real(os.path.join(clone, ".git")):
        v.reason = "чужой клон: common-dir не совпадает с клоном"
        return
    if entry.detached or not entry.branch:
        v.reason = "detached HEAD: у копии нет ветки задачи"
        return
    v.branch = entry.branch.removeprefix("refs/heads/")
    match = _TASK_BRANCH.match(entry.branch)
    if not match:
        v.reason = f"ветка {v.branch} не вида task-N/..."
    elif int(match.group(1)) != v.task_id:
        v.reason = f"ID в пути ({v.task_id}) и в ветке ({match.group(1)}) расходятся"


def _read(api: Api, path: str) -> tuple[Any, str]:
    """GET к хабу: (данные, "") или (None, причина). Сбой не равен пустому ответу."""
    try:
        data = api("GET", path)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - _api завершает процесс
        return None, f"GET {path} не ответил ({type(exc).__name__})"
    if data is None:
        return None, f"GET {path}: пустой ответ"
    return data, ""


class _Hub:
    """Ответы хаба: каждый запрос один раз, ошибка запоминается."""

    def __init__(self, api: Api, refusal: Refusal, clone: str) -> None:
        self.api, self.refusal, self.clone = api, refusal, clone
        self._tasks: dict[int, tuple[Any, str]] = {}
        self._projects: tuple[Any, str] | None = None

    def task(self, task_id: int) -> tuple[dict[str, Any] | None, str]:
        if task_id not in self._tasks:
            self._tasks[task_id] = _read(self.api, f"/api/tasks/{task_id}")
        data, err = self._tasks[task_id]
        if err or not isinstance(data, dict) or data.get("id") != task_id:
            return None, err or f"GET /api/tasks/{task_id}: чужой или пустой ответ"
        return data, ""

    def project(self, task: dict[str, Any]) -> tuple[dict[str, Any], str]:
        if self._projects is None:
            self._projects = _read(self.api, "/api/projects?include_archived=true")
        data, err = self._projects
        if err or not isinstance(data, list):
            return {}, err or "GET /api/projects: ответ не список"
        pid = (task.get("project") or {}).get("id")
        return next((p for p in data if p.get("id") == pid), {}), ""

    def delivery(self, task_id: int) -> tuple[str, str]:
        data, err = _read(self.api, f"/api/tasks/{task_id}/delivery")
        if err or not isinstance(data, dict):
            return "", err or f"GET /api/tasks/{task_id}/delivery: ответ не объект"
        return str(data.get("state") or ""), ""


def _unread(v: Verdict, err: str) -> None:
    v.reason, v.unread = f"статус не прочитан: {err}", True


def _hub_verdict(v: Verdict, hub: _Hub) -> None:
    """Фаза 2: ответы хаба. Ставит ``reason`` или оставляет пусто (кандидат)."""
    if v.task_id is None:
        raise ValueError("кандидат без ID задачи")
    task, err = hub.task(v.task_id)
    project, err2 = hub.project(task) if task else ({}, "")
    if task is None or err2:
        _unread(v, err if task is None else err2)
        return
    refusal = hub.refusal(task, project, hub.clone)
    if refusal:
        # Не тот клон для repo проекта: убирать нечего и это не штатное «оставить».
        v.reason, v.unread = refusal, True
    elif task.get("branch") != v.branch:
        theirs = task.get("branch") or "(пусто)"
        v.reason = f"ветка задачи в хабе {theirs}, а у копии {v.branch}"
    elif task.get("status") != "completed":
        v.reason = f"статус задачи {task.get('status')}, не completed"
    else:
        _delivery_verdict(v, hub, task)


def _delivery_verdict(v: Verdict, hub: _Hub, task: dict[str, Any]) -> None:
    state, err = hub.delivery(int(v.task_id or 0))
    if err:
        _unread(v, err)
    elif state != "delivered":
        v.reason = f"доставка не подтверждена хабом: {state or 'нет ответа'}"
    elif not task.get("submission_sha"):
        v.reason = "у задачи нет submission_sha: сверять HEAD не с чем"
    else:
        v.sha = str(task["submission_sha"])


def _git_verdict(v: Verdict) -> str:
    """HEAD <= submission_sha и чистота; "" - можно, иначе причина."""
    rc, head, _ = _git(v.path, "rev-parse", "HEAD")
    if rc != 0 or not head.strip():
        v.unread = True
        return f"HEAD не прочитан (git rc {rc})"
    rc, _, err = _git(v.path, "merge-base", "--is-ancestor", head.strip(), v.sha)
    if rc == 1:
        return (
            "после сдачи в копии есть локальные коммиты: HEAD не предок submission_sha"
        )
    if rc != 0:
        v.unread = True
        return (
            f"сдача {v.sha[:12]} не сверена с HEAD (git rc {rc}: {err.strip()[:120]})"
        )
    rc, out, err = _git(v.path, "status", "--porcelain")
    if rc != 0:
        v.unread = True
        return f"чистота не определена: git status rc {rc} {err.strip()[:120]}"
    if out.strip():
        return "не чисто: есть незакоммиченные или неотслеживаемые файлы"
    return ""


def _decide(entries: list[Entry], clone: str, hub: _Hub) -> list[Verdict]:
    """Фазы 1-3 по всем копиям; удалений здесь нет."""
    current = _current_toplevel()
    verdicts = sorted(
        (
            _local_verdict(e, clone, current, _real(e.path) == _real(clone))
            for e in entries
        ),
        key=lambda x: x.path,
    )
    for v in verdicts:
        if v.reason:
            continue
        _hub_verdict(v, hub)
        if not v.reason:
            v.reason = _git_verdict(v)
        if not v.reason:
            v.remove = True
            v.reason = (
                "completed, доставка подтверждена хабом, HEAD <= submission_sha, чисто"
            )
    return verdicts


def _label(v: Verdict) -> str:
    return f" (#{v.task_id})" if v.task_id is not None else ""


def _strays(clone: str, entries: list[Entry]) -> list[str]:
    """Папки рядом с клоном, которых нет в git worktree list: только назвать."""
    from hub.integrations.git_ops import task_worktree_path

    known = {_real(e.path) for e in entries}
    canonical_root = os.path.dirname(task_worktree_path(0, clone))
    found: list[str] = []
    for root, pattern in (
        (canonical_root, _TASK_DIR),
        (os.path.dirname(clone), _STRAY_SIBLING),
    ):
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            path = os.path.join(root, name)
            if (
                pattern.search(name)
                and os.path.isdir(path)
                and _real(path) not in known
            ):
                found.append(path)
    return found


def _remove(v: Verdict, clone: str) -> tuple[bool, str]:
    """Повторная проверка и ``git worktree remove`` без --force: (удалено, текст)."""
    rc, ref, _ = _git(v.path, "symbolic-ref", "-q", "HEAD")
    if rc != 0 or ref.strip() != f"refs/heads/{v.branch}":
        return False, "изменилась перед удалением: ветка копии другая"
    why = _git_verdict(v)
    if why:
        return False, f"изменилась перед удалением: {why}"
    rc, _, err = _git(clone, "worktree", "remove", v.path)
    if rc != 0:
        return False, f"git отказал в удалении (rc {rc}): {err.strip()[:200]}"
    return True, ""


def run_prune(
    args: Any, *, api: Api, refusal: Refusal, out: Callable[[str], None] = print
) -> int:
    from hub.integrations.git_ops import clone_root

    clone = asyncio.run(clone_root(args.repo or "."))
    if not clone:
        print(f"{args.repo or '.'} не git-клон: нечего убирать", file=sys.stderr)
        return 1
    rc, text, err = _git(clone, "worktree", "list", "--porcelain")
    if rc != 0:
        print(
            f"git worktree list не прочитан (rc {rc}): {err.strip()}", file=sys.stderr
        )
        return 1
    entries = _parse_porcelain(text)
    verdicts = _decide(entries, clone, _Hub(api, refusal, clone))
    for v in verdicts:
        out(f"{'удалить ' if v.remove else 'оставить'} {v.path}{_label(v)}: {v.reason}")
    for path in _strays(clone, entries):
        out(f"оставить {path}: вне git worktree list клона, не удаляю")
    failed = any(v.unread for v in verdicts)
    if not args.apply:
        out("dry-run: ничего не удалено; --apply убирает копии со строкой «удалить»")
        return 1 if failed else 0
    for v in (x for x in verdicts if x.remove):
        done, why = _remove(v, clone)
        failed = failed or v.unread or (not done and not why.startswith("изменилась"))
        out(f"удалено  {v.path}" if done else f"оставить {v.path}{_label(v)}: {why}")
    return 1 if failed else 0


def add_arguments(parser: Any) -> None:
    parser.add_argument("target", metavar="N|prune", help="task id, or 'prune'")
    parser.add_argument(
        "--repo", default="", help="Clone path (default: current git clone)"
    )
    parser.add_argument(
        "--apply", action="store_true", help="prune: actually remove (default: list)"
    )


def dispatch(
    args: Any, *, create: Callable[[Any], int], api: Api, refusal: Refusal
) -> int:
    """``worktree N`` как раньше; ``worktree prune`` - уборка."""
    if args.target == "prune":
        return run_prune(args, api=api, refusal=refusal)
    if not str(args.target).isdigit():
        print(
            f"worktree: нужен номер задачи или prune: {args.target!r}", file=sys.stderr
        )
        return 2
    if args.apply:
        print("worktree: --apply только для prune", file=sys.stderr)
        return 2
    args.task_id = int(args.target)
    return create(args)
