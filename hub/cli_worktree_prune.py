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

Два шага, оба обратимы до второго. ``prune --apply`` НЕ удаляет: он переносит
копию ``git worktree move`` в ``<корень-копий>/.trash/task-N-<UTC>`` (gitdir,
refs и содержимое целы, копия остаётся в ``git worktree list``, HEAD отцепляется
от ветки на том же коммите, чтобы ``hp-hub worktree N`` мог завести новую копию).
Перед переносом в клоне ставятся защитные refs ``refs/haiplane-trash/<имя>/<k>``
(HEAD, каждая запись reflog HEAD, sha из ORIG_HEAD/FETCH_HEAD/AUTO_MERGE), чтобы gc
не убрал объекты, видимые только копии; после переноса в её gitdir пишется маркер
(время, id задачи, старый путь, ветка, HEAD). Вернуть: ``hp-hub worktree restore
<имя>`` (проверяет, что канонический путь свободен, двигает копию, делает
``git switch <ветка>``, снимает refs и маркер). Окончательно убирает только
``hp-hub worktree purge --apply``, и только копии с маркером, совпадающим с именем
и HEAD, старше 7 суток по времени МАРКЕРА (``--older-than-days``, >= 1). Внутрь
``.trash`` prune не заглядывает (путь не канонический), поэтому маркера нет в
белом списке gitdir.

Почему так (раунд 4). Доказать безопасность необратимого удаления проверками
нельзя: каждый раунд ревью находил ещё одно место, где ценное состояние не
видно проверке. Поэтому проверки остаются, но шаг обратим.

Проверки - белый список: копия уходит в карантин, только если ВСЁ в ней и в её
gitdir названо в явном списке; всё неизвестное - «оставить» с причиной:
  * рабочее дерево: ``status --porcelain=v1 -z --untracked-files=all
    --ignored=matching``; разрешены только ignored-пути из REGENERABLE_* (имя
    каталога не доказательство, поэтому карантин, а не удаление); любой другой
    ignored, untracked или изменённый путь (.env, .claude/...) оставляет копию;
  * index: ``ls-files -v`` - любой тег кроме H (assume-unchanged, skip-worktree)
    прячет правки от status, копия остаётся; плюс независимый ``git diff-index``;
  * gitdir копии: только GITDIR_ALLOWED; refs/ (refs/worktree, refs/bisect),
    logs/refs, MERGE_HEAD, REBASE_*, rebase-*, sequencer, BISECT_*,
    CHERRY_PICK_HEAD, locked и всё прочее оставляют копию; каждая sha в
    ORIG_HEAD, FETCH_HEAD и AUTO_MERGE - предок сдачи или в refs/remotes;
  * HEAD и каждая запись его reflog - предок submission_sha или в refs/remotes;
  * внутри копии нет вложенного ``.git`` (обход не пропускает ни .venv, ни
    node_modules) и другого worktree клона; канонический путь и его лист - не
    симлинки; gitdir не менялся ``--min-idle-minutes`` (30 по умолчанию).
Любая ошибка чтения (git, обход каталога, снятие отпечатка) - «не прочитано»:
копия остаётся, код возврата ненулевой.

Гонка. В начале финальной проверки снимается отпечаток (HEAD, число и новейшая
запись reflog, ПОЛНЫЙ список файлов gitdir с размером и mtime_ns), проверяются
ВСЕ условия, включая обход каталога, и отпечаток снимается ещё раз
непосредственно перед ``git worktree move``; расхождение оставляет копию.

ПРИНЯТЫЙ ОСТАТОЧНЫЙ РИСК: окно между вторым отпечатком и самим переносом. Оно
больше не теряет данные: перенос не удаляет ничего, а сделанное в окне лежит в
карантине целиком до purge.

Неудачный перенос не «оставить»: проверяются старое место и карантин, итог
называется «ошибка перемещения», «результат неизвестен» и т. п., код возврата
ненулевой. Доставку признаёт только явный ``delivered``; пустой ответ,
неизвестное значение и ``unknown`` - «не прочитано», код возврата ненулевой.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import subprocess  # nosec B404 - только git, список аргументов, без shell
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

from hub import cli_worktree_trash as trash

GIT_TIMEOUT_S = 60
DEFAULT_MIN_IDLE_MINUTES = 30.0
# Белый список gitdir копии (.git/worktrees/<имя>/): всё остальное - оставить.
GITDIR_ALLOWED = frozenset(
    {"HEAD", "ORIG_HEAD", "FETCH_HEAD", "index", "commondir", "gitdir"}
    | {
        "logs/HEAD",
        "AUTO_MERGE",
        "COMMIT_EDITMSG",
    }  # EDITMSG: git пишет при каждом commit
)
# Игнорируемое, что пересоздаётся само: единственное, чего может не жалеть удаление.
REGENERABLE_DIRS = frozenset(
    {".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    | {"node_modules", "htmlcov", "dist", "build"}
)
REGENERABLE_GLOBS = (".coverage", "*.pyc", "*.egg-info")
SHOWN_PATHS = 3
_TASK_DIR = re.compile(r"^task-(\d+)$")
_TASK_BRANCH = re.compile(r"^refs/heads/task-(\d+)/.+")
_STRAY_SIBLING = re.compile(r"-task-\d+$")
TRASH_DIR = ".trash"

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
    others: tuple[str, ...] = ()  # остальные worktree клона (для вложенности)
    min_idle: float = DEFAULT_MIN_IDLE_MINUTES


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
    """ID задачи из пути и причина, если путь не канонический (#459).

    realpath - только для корня ``.<клон>-worktrees`` (на macOS /tmp это
    /private/tmp). Лист ``task-N`` берётся как записан и не должен быть
    симлинком: иначе внешний worktree выдаёт себя за канонический.
    """
    from hub.integrations.git_ops import _worktree_path as task_worktree_path

    root = _real(os.path.dirname(task_worktree_path(0, clone)))
    leaf = os.path.abspath(path)
    match = _TASK_DIR.match(os.path.basename(leaf))
    if not match or _real(os.path.dirname(leaf)) != root:
        return None, f"вне канонического пути {root}/task-N"
    if os.path.islink(leaf):
        return None, f"канонический путь {leaf} - симлинк: внешняя копия не трогается"
    return int(match.group(1)), ""


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

    def delivery(self, task_id: int) -> tuple[str, str, str]:
        """(state, ошибка чтения, пояснение хаба)."""
        data, err = _read(self.api, f"/api/tasks/{task_id}/delivery")
        if err or not isinstance(data, dict):
            return "", err or f"GET /api/tasks/{task_id}/delivery: ответ не объект", ""
        return str(data.get("state") or ""), "", str(data.get("reason") or "")


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
    """Только явный ``delivered`` пускает дальше; пустое и неизвестное - не прочитано."""
    state, err, note = hub.delivery(int(v.task_id or 0))
    if err:
        _unread(v, err)
    elif state in ("pr_open", "pr_closed"):
        v.reason = f"доставка не подтверждена хабом: {state}"
    elif state != "delivered":
        v.reason = f"доставка не прочитана: state={state or '(нет)'}" + (
            f", {note[:200]}" if note else ""
        )
        v.unread = True
    elif not task.get("submission_sha"):
        v.reason = "у задачи нет submission_sha: сверять HEAD не с чем"
    else:
        v.sha = str(task["submission_sha"])


def _reachable_reason(v: Verdict, sha: str, where: str) -> str:
    """sha - предок сдачи или есть в refs/remotes; иначе причина (он только здесь)."""
    rc, _, _ = _git(v.path, "merge-base", "--is-ancestor", sha, v.sha)
    if rc == 0:
        return ""
    if rc != 1:
        v.unread = True
        return f"{where} {sha[:12]} не сверена со сдачей (git rc {rc})"
    rc, listed, _ = _git(
        v.path, "for-each-ref", "--count=1", "--contains", sha, "refs/remotes"
    )
    if rc != 0:
        v.unread = True
        return f"{where} {sha[:12]} не сверена с refs/remotes (git rc {rc})"
    if not listed.strip():
        return f"коммит {sha[:12]} из {where} есть только в этой копии"
    return ""


def _reflog_reason(v: Verdict) -> str:
    """Каждая sha, которую держит копия (reflog с обеих сторон, ORIG/FETCH/AUTO_MERGE)."""
    rc, gitdir, _ = _git(v.path, "rev-parse", "--absolute-git-dir")
    if rc != 0 or not gitdir.strip():
        v.unread = True
        return f"gitdir копии не прочитан (git rc {rc})"
    held, err = trash.copy_shas(gitdir.strip())
    if err:
        v.unread = True
        return err
    for label, sha in held:
        why = _reachable_reason(v, sha, label)
        if why:
            return why
    return ""


def _nested_reason(v: Verdict) -> str:
    """Вложенный .git (где угодно, и в .venv тоже) или другой worktree клона внутри."""
    root = _real(v.path)
    for other in v.others:
        inside = _real(other)
        if inside != root and inside.startswith(root + os.sep):
            return f"внутри копии лежит другой worktree клона: {other}"
    errors: list[OSError] = []
    for here, dirs, files in os.walk(root, onerror=errors.append, followlinks=False):
        hit = [n for n in (*dirs, *files) if n == ".git" and here != root]
        if hit:
            return f"внутри копии вложенный git-репозиторий или worktree: {here}"
    if errors:
        v.unread = True
        return f"обход копии не прочитан до конца: {errors[0]}"
    return ""


def _idle_reason(v: Verdict) -> str:
    """HEAD, index, logs/HEAD gitdir копии не менялись последние min_idle минут."""
    rc, gitdir, _ = _git(v.path, "rev-parse", "--absolute-git-dir")
    if rc != 0 or not gitdir.strip():
        v.unread = True
        return f"gitdir копии не прочитан (git rc {rc})"
    newest = 0.0
    for name in ("HEAD", "index", os.path.join("logs", "HEAD")):
        try:
            newest = max(newest, os.stat(os.path.join(gitdir.strip(), name)).st_mtime)
        except FileNotFoundError:
            continue
        except OSError as exc:
            v.unread = True
            return f"{name} в gitdir копии не прочитан ({type(exc).__name__})"
    age_min = (time.time() - newest) / 60
    if age_min < v.min_idle:
        return f"копия менялась {age_min:.0f} мин назад (< {v.min_idle:g}): возможно, в работе"
    return ""


def _regenerable(path: str) -> bool:
    """Ignored-путь, в котором есть пересоздаваемое звено (.venv, *.pyc, ...)."""
    return any(
        part in REGENERABLE_DIRS
        or any(fnmatch.fnmatch(part, g) for g in REGENERABLE_GLOBS)
        for part in path.split("/")
        if part
    )


def _head_reason(v: Verdict) -> str:
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
    return ""


def _gitdir_listing(gitdir: str) -> tuple[list[tuple[str, int, int]], list[OSError]]:
    """Все файлы gitdir: (путь, размер, mtime_ns) и ошибки обхода."""
    errors: list[OSError] = []
    rows: list[tuple[str, int, int]] = []
    for here, _dirs, files in os.walk(gitdir, onerror=errors.append, followlinks=False):
        for name in files:
            full = os.path.join(here, name)
            try:
                st = os.lstat(full)
            except OSError as exc:
                errors.append(exc)
                continue
            rel = os.path.relpath(full, gitdir).replace(os.sep, "/")
            rows.append((rel, st.st_size, st.st_mtime_ns))
    return sorted(rows), errors


def _gitdir_reason(v: Verdict) -> str:
    """В gitdir копии только файлы из белого списка: refs/, MERGE_HEAD, rebase-* - нет."""
    rc, gitdir, _ = _git(v.path, "rev-parse", "--absolute-git-dir")
    if rc != 0 or not gitdir.strip():
        v.unread = True
        return f"gitdir копии не прочитан (git rc {rc})"
    rows, errors = _gitdir_listing(gitdir.strip())
    if errors:
        v.unread = True
        return f"gitdir копии не прочитан до конца: {errors[0]}"
    extra = [n for n, _size, _mtime in rows if n not in GITDIR_ALLOWED]
    if extra:
        return "в gitdir копии есть то, чего нет в белом списке: " + ", ".join(
            extra[:SHOWN_PATHS]
        )
    return ""


def _status_records(out: str) -> list[tuple[str, str]]:
    """Записи ``status --porcelain=v1 -z``: (XY, путь); пара переименования - одна."""
    tokens = out.split("\0")
    records: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if len(tok) < 4:
            continue
        records.append((tok[:2], tok[3:]))
        if tok[0] in "RC":
            i += 1  # исходный путь переименования или копии
    return records


def _tree_reason(v: Verdict) -> str:
    """Рабочее дерево: ни правок, ни untracked, ignored - только пересоздаваемое."""
    rc, out, err = _git(
        v.path,
        "--no-optional-locks",  # status не должен обновлять index: он считается простоем
        "-c",
        "status.showUntrackedFiles=all",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=matching",
    )
    if rc != 0:
        v.unread = True
        return f"чистота не определена: git status rc {rc} {err.strip()[:120]}"
    blockers = [
        f"{code.strip()} {path}"
        for code, path in _status_records(out)
        if not (code == "!!" and _regenerable(path))
    ]
    if blockers:
        return "не чисто, нет в белом списке пересоздаваемого: " + ", ".join(
            blockers[:SHOWN_PATHS]
        )
    return ""


def _flags_reason(v: Verdict) -> str:
    """Теги index кроме H (assume-unchanged, skip-worktree) прячут правки от status."""
    rc, out, err = _git(v.path, "--no-optional-locks", "ls-files", "-v", "-z")
    if rc != 0:
        v.unread = True
        return f"index не прочитан: git ls-files rc {rc} {err.strip()[:120]}"
    hidden = [t[2:] for t in out.split("\0") if len(t) > 2 and t[0] != "H"]
    if hidden:
        return (
            "в index флаги assume-unchanged/skip-worktree прячут правки: "
            + ", ".join(hidden[:SHOWN_PATHS])
        )
    # diff-index, а не ``git diff``: porcelain-diff обновляет index и сам освежает
    # простой. Устаревший stat даёт ложное «правки» - это сторона «оставить».
    rc, _, err = _git(v.path, "diff-index", "--no-ext-diff", "--quiet", "HEAD", "--")
    if rc == 1:
        return "не чисто: git diff-index HEAD показывает правки"
    if rc != 0:
        v.unread = True
        return f"чистота не определена: git diff rc {rc} {err.strip()[:120]}"
    return ""


def _fingerprint(v: Verdict) -> tuple[Any, ...] | None:
    """Отпечаток до и после проверки: HEAD, reflog и ПОЛНЫЙ список файлов gitdir.

    None - не снять (git или обход вернули ошибку): вызывающий ставит unread.
    """
    rc, gitdir, _ = _git(v.path, "rev-parse", "--absolute-git-dir")
    rc2, head, _ = _git(v.path, "rev-parse", "HEAD")
    rc3, log, _ = _git(v.path, "reflog", "--format=%H", "HEAD")
    if rc or rc2 or rc3:
        return None
    rows, errors = _gitdir_listing(gitdir.strip())
    if errors:
        return None
    entries = log.split()
    return (head.strip(), len(entries), entries[:1], tuple(rows))


def _git_verdict(v: Verdict) -> str:
    """Все условия белого списка по порядку; "" - копию можно удалять."""
    for check in (
        _idle_reason,  # первым: дальнейшие git-вызовы не должны трогать gitdir
        _head_reason,
        _reflog_reason,
        _gitdir_reason,
        _nested_reason,
        _flags_reason,
        _tree_reason,
    ):
        why = check(v)
        if why:
            return why
    return ""


def _decide(
    entries: list[Entry], clone: str, hub: _Hub, min_idle: float
) -> list[Verdict]:
    """Фазы 1-3 по всем копиям; удалений здесь нет."""
    current = _current_toplevel()
    verdicts = sorted(
        (
            _local_verdict(e, clone, current, _real(e.path) == _real(clone))
            for e in entries
        ),
        key=lambda x: x.path,
    )
    everywhere = tuple(e.path for e in entries)
    for v in verdicts:
        if v.reason:
            continue
        v.others, v.min_idle = everywhere, min_idle
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
    from hub.integrations.git_ops import _worktree_path as task_worktree_path

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


def trash_root(clone: str) -> str:
    """``<корень-копий>/.trash``: карантин рядом с каноническими путями."""
    from hub.integrations.git_ops import _worktree_path as task_worktree_path

    return os.path.join(_real(os.path.dirname(task_worktree_path(0, clone))), TRASH_DIR)


def _trash_dest(v: Verdict, clone: str) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    base = os.path.join(trash_root(clone), f"task-{v.task_id}-{stamp}")
    dest, n = base, 0
    while os.path.lexists(dest):
        n += 1
        dest = f"{base}-{n}"
    return dest


def _failed_move(v: Verdict, dest: str, rc: int, err: str) -> str:
    """Где копия после неудачного move: на старом месте, в карантине, нигде."""
    here, there = os.path.lexists(v.path), os.path.lexists(dest)
    tail = f"git rc {rc}: {err.strip()[:200]}"
    if here and not there:
        return f"ошибка перемещения, копия на месте: {tail}"
    if there and not here:
        return f"результат неизвестен: копия уже в {dest}, git вернул ошибку (git worktree repair): {tail}"
    if here and there:
        return f"ошибка перемещения: путь и карантин оба существуют, проверьте вручную: {tail}"
    return f"результат неизвестен: копии нет ни на месте, ни в {dest}: {tail}"


def _unread_outcome(v: Verdict, what: str) -> tuple[str, str]:
    v.unread = True
    return "kept", f"изменилась перед перемещением: {what}"


def _quarantine(v: Verdict, clone: str) -> tuple[str, str]:
    """Финальная проверка и ``git worktree move`` в .trash (обратимо, 7 суток).

    Возвращает (исход, текст): moved | kept | failed. Копия остаётся
    зарегистрированной в клоне; HEAD в карантине отцепляется от ветки задачи
    (тот же коммит), чтобы ``hp-hub worktree N`` мог завести новую копию.
    """
    first = _fingerprint(v)
    if first is None:
        return _unread_outcome(v, "отпечаток не снят (git или обход вернули ошибку)")
    rc, ref, _ = _git(v.path, "symbolic-ref", "-q", "HEAD")
    if rc != 0 or ref.strip() != f"refs/heads/{v.branch}":
        return "kept", "изменилась перед перемещением: ветка копии другая"
    why = _git_verdict(v)
    if why:
        return "kept", f"изменилась перед перемещением: {why}"
    second = _fingerprint(v)
    if second is None:
        return _unread_outcome(v, "второй отпечаток не снят")
    if second != first:
        return (
            "kept",
            "изменилась перед перемещением: отпечаток HEAD/reflog/gitdir разошёлся",
        )
    dest = _trash_dest(v, clone)
    name = os.path.basename(dest)
    refs, err = trash.protect(_git, clone, v.path, name)
    if err:
        v.unread = True
        return "kept", f"защитные refs не созданы, копия осталась: {err}"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    rc, _, err = _git(clone, "worktree", "move", v.path, dest)
    if rc != 0:
        if not os.path.lexists(dest):
            trash.unprotect_refs(_git, clone, refs)  # копия не ушла: защита не нужна
        return "failed", _failed_move(v, dest, rc, err)
    rc, _, err = _git(dest, "switch", "--detach")
    note = "" if rc == 0 else f" (ветка не освобождена: {err.strip()[:120]})"
    return _write_marker(v, dest, name, note)


def _write_marker(v: Verdict, dest: str, name: str, note: str) -> tuple[str, str]:
    """Маркер карантина в gitdir уже перенесённой копии: без него purge её не тронет."""
    _, gitdir, _ = _git(dest, "rev-parse", "--absolute-git-dir")
    _, head, _ = _git(dest, "rev-parse", "HEAD")
    marker = trash.make_marker(
        int(v.task_id or 0), v.path, v.branch, head.strip(), name
    )
    err = (
        trash.write_marker(gitdir.strip(), marker)
        if gitdir.strip()
        else "gitdir не прочитан"
    )
    if err:
        return "failed", f"в карантине {dest}, но {err}: purge её не тронет{note}"
    return "moved", f"{dest}{note}"


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
    idle = float(getattr(args, "min_idle_minutes", DEFAULT_MIN_IDLE_MINUTES))
    verdicts = _decide(entries, clone, _Hub(api, refusal, clone), idle)
    for v in verdicts:
        word = "в карантин" if v.remove else "оставить  "
        out(f"{word} {v.path}{_label(v)}: {v.reason}")
    for path in _strays(clone, entries):
        out(f"оставить {path}: вне git worktree list клона, не удаляю")
    failed = any(v.unread for v in verdicts)
    if not args.apply:
        out(
            "dry-run: ничего не удалено и не перемещено; --apply отправляет копии "
            f"со строкой «в карантин» в {TRASH_DIR}, hp-hub worktree purge убирает "
            "их через 7 суток"
        )
        return 1 if failed else 0
    for v in (x for x in verdicts if x.remove):
        outcome, why = _quarantine(v, clone)
        failed = failed or v.unread or outcome == "failed"
        if outcome == "moved":
            out(f"в карантине: {why} (#{v.task_id}, было {v.path})")
        else:
            word = {"kept": "оставить  ", "failed": "ошибка    "}[outcome]
            out(f"{word} {v.path}{_label(v)}: {why}")
    return 1 if failed else 0


def add_arguments(parser: Any) -> None:
    parser.add_argument(
        "target",
        metavar="N|prune|purge|restore",
        help="task id, 'prune', 'purge' or 'restore <name>'",
    )
    parser.add_argument(
        "name", nargs="?", default="", help="restore: имя копии в .trash"
    )
    parser.add_argument(
        "--repo", default="", help="Clone path (default: current git clone)"
    )
    parser.add_argument(
        "--min-idle-minutes",
        type=float,
        default=DEFAULT_MIN_IDLE_MINUTES,
        help="prune: keep a copy whose gitdir changed more recently (default 30)",
    )
    parser.add_argument(
        "--older-than-days",
        type=trash.days_arg,
        default=7.0,
        help="purge: remove quarantine copies older than this (default 7)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="prune: move to quarantine; purge: remove (default: only list)",
    )


def dispatch(
    args: Any, *, create: Callable[[Any], int], api: Api, refusal: Refusal
) -> int:
    """``worktree N`` как раньше; ``prune`` - в карантин, ``purge`` - из карантина."""
    if args.target == "prune":
        return run_prune(args, api=api, refusal=refusal)
    if args.target in ("purge", "restore"):
        from hub import cli_worktree_purge as purge

        return (
            purge.run_purge(args) if args.target == "purge" else purge.run_restore(args)
        )
    if not str(args.target).isdigit():
        print(
            f"worktree: нужен номер задачи, prune, purge или restore: {args.target!r}",
            file=sys.stderr,
        )
        return 2
    if args.apply:
        print("worktree: --apply только для prune и purge", file=sys.stderr)
        return 2
    args.task_id = int(args.target)
    return create(args)
