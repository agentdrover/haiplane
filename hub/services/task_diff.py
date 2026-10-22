"""The changes under review, read inside the hub (#824).

Until now the gate named the SIZE of a submission's diff — files and lines,
#808 — and offered a command to run somewhere else:

    git diff develop...task-643/memo-spike-impl

That command is the whole cost of human review. To see what changed, the owner
had to leave the hub, fetch the branch and check it out, and that is the step
that gets skipped — which is why the verdict ends up resting on the prose of
the party being judged.

Two rules this module exists to keep:

1. The diff is taken against the PINNED ``submission_sha``, never the branch
   name. The verdict is cast on one submission; a branch that moved after it
   would show the reader code they are not approving. ``sha_check`` already
   tells the reviewer when the branch has moved (#572) — showing the moved
   branch's diff beside that warning would contradict it.

2. Every refusal names its own cause, and truncation is stated out loud.
   "Could not read the repository", "that commit is not here" and "there is no
   workspace" are three different answers; collapsing them into one empty box
   is the half-truth #725 removed from the brief's other blocks. A silently cut
   diff is worse still: it reads as the whole change while being a fragment.
"""

from __future__ import annotations

from hub.services.diff_scan import FILE_STARTS, scan_diff

import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from hub import config
from hub import repository as repo
from hub.integrations.registry import plugins
from hub.services import review_evidence
from hub.services.brief_failure import scrub_strings

log = logging.getLogger("hub")

# What the reader is looking at, or why they are not.
READ = "read"  # the diff is here
NO_SUBMISSION = "no_submission"  # nothing was pinned, so there is nothing to show
UNREACHABLE_SHA = "unreachable_sha"  # the workspace does not carry that commit
NO_WORKSPACE = "no_workspace"  # nowhere to look
UNREADABLE = "unreadable"  # git was there and still could not answer
MERGED_INTO_BASE = "merged_into_base"  # the commit is already in the base (#1239)
WORKSPACE_MISSING = "workspace_missing"  # the project directory is not on disk (#2002)

# Stated to the reader as is. The path is NOT in it: a card is read by people
# who were never told where the hub keeps its clones. It goes to the log.
WORKSPACE_MISSING_REASON = "дифф недоступен: рабочий каталог проекта не найден"

_T = TypeVar("_T")


def _blank(state: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "state": state,
        "reason": reason,
        "files": [],
        "truncated": False,
        "shown_lines": 0,
        "total_lines": 0,
        "fallback_command": "",
        "sha": "",
        "base": "",
        **extra,
    }


class _Unavailable(Exception):
    """A git question could not even be asked; carries the finished answer."""

    def __init__(self, blank: dict[str, Any]) -> None:
        super().__init__(blank["state"])
        self.blank = blank


def _failer(sha: str, base: str, fallback: str) -> Callable[[str, str], dict[str, Any]]:
    def fail(state: str, reason: str) -> dict[str, Any]:
        return _blank(state, reason, sha=sha, base=base, fallback_command=fallback)

    return fail


def _is_missing(path: str) -> bool:
    """Absent (or a parent is a file) — NOT "cannot look", which is another answer.

    ``os.path.isdir`` folds every ``OSError`` into False, so a parent the hub may
    not traverse read as a missing directory. Only these two errors mean absent.
    """
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


async def _ask(
    workspace: str, call: Awaitable[_T], fail: Callable[[str, str], dict[str, Any]]
) -> _T:
    """Await one git question; a directory that is not there is an answer (#2002).

    ``proc.run`` raises instead of returning a non-zero rc when the cwd is
    missing, and that ``FileNotFoundError`` used to travel up to the page — a
    500 on the one card where the owner has to act. Python names the
    EXECUTABLE in that error even when the directory is what is missing, so
    the directory is asked about directly. The text of the exception carries
    the path: it goes to the log, never into the answer.
    """
    try:
        return await call
    except OSError as exc:
        log.warning("diff: git in %s could not run: %s", workspace, exc)
        if _is_missing(workspace):
            raise _Unavailable(
                fail(WORKSPACE_MISSING, WORKSPACE_MISSING_REASON)
            ) from exc
        raise _Unavailable(
            fail(
                UNREADABLE,
                f"git не запустился ({type(exc).__name__}) — подробности в логе хаба",
            )
        ) from exc


def _publish(result: dict[str, Any]) -> dict[str, Any]:
    """The one boundary of the diff view: reason strings only, never the code (#2002)."""
    return scrub_strings(result)


def _hunk_start(header: str) -> int | None:
    """New-side start line of a ``@@ -a,b +c,d @@`` header, or None."""
    plus = header.find("+")
    if plus < 0:
        return None
    token = header[plus + 1 :].split()[0] if header[plus + 1 :].split() else ""
    number = token.split(",", 1)[0]
    return int(number) if number.isdigit() else None


def _line_kind(entry: Any) -> str:
    """``"+"`` added, ``"-"`` removed, ``" "`` context (in the result), ``""`` none.

    A hunk body is read by its columns (:mod:`diff_scan`): in a merge's combined
    diff a line is in the result when no column holds ``-``, and added when one
    holds ``+``. Outside a hunk (headers, text the counters could not place)
    the first character decides, and ``+++ `` / ``--- `` are headers.
    """
    text = entry.text
    if entry.body:
        if text.startswith("\\"):
            return ""
        prefix = text[: entry.body].ljust(entry.body)
        return "-" if "-" in prefix else "+" if "+" in prefix else " "
    if text.startswith("+") and not text.startswith("+++"):
        return "+"
    if text.startswith("-") and not text.startswith("---"):
        return "-"
    return " " if text.startswith(" ") or text == "" else ""


def split_files(diff: str) -> list[dict[str, Any]]:
    """Split a unified diff into per-file entries, in the order git printed them.

    Each line carries the new-side line number it lands on (#826) so a reader
    can point at it: a finding without an address sends the implementer looking
    for the place by description. Removed lines have no new-side number and
    carry None rather than a neighbour's — a wrong address is worse than none.

    Deliberately tolerant: an unparsable header keeps its lines rather than
    dropping them, because a lost hunk is a change nobody was shown.
    """
    files: list[dict[str, Any]] = []
    owners: list[Any] = []  # the span of each entry: its path is known only later
    current: dict[str, Any] | None = None
    last: Any = None
    new_line: int | None = None
    for entry in scan_diff(diff):
        line, body = entry.text, bool(entry.body)
        if entry.span is not last:
            last = entry.span
            current = {
                "path": "(diff preamble)" if not files and not entry.span.path else "",
                "added": 0,
                "removed": 0,
                "lines": [],
            }
            files.append(current)
            owners.append(entry.span)
            new_line = None
        if line.startswith(FILE_STARTS):
            current["fallback"] = line  # type: ignore[index]
            continue
        if current is None:  # unreachable: the first line opens a file
            continue

        number: int | None = None
        kind = _line_kind(entry)
        if line.startswith("@@") and not body:
            new_line = _hunk_start(line)
        elif new_line is not None and kind in ("+", " "):
            number, new_line = new_line, new_line + 1

        current["lines"].append({"text": line, "new_line": number})
        if kind == "+":
            current["added"] += 1
        elif kind == "-":
            current["removed"] += 1
    for item, span in zip(files, owners, strict=True):
        fallback = item.pop("fallback", "")
        item["path"] = span.path or item["path"] or fallback
    return files


def truncate(
    files: list[dict[str, Any]], max_lines: int, max_bytes: int
) -> tuple[list[dict[str, Any]], bool, int, int]:
    """Cut the rendering to the cap and report the cut.

    Cuts INSIDE a file when one file alone exceeds the budget: keeping such a
    file whole would let a single 30k-line diff ignore the cap entirely, which
    is how a page stops rendering at the moment it matters most. The entry then
    carries ``cut=True`` so the reader is told which file they are seeing only
    part of — a cut nobody is told about is the failure this cap must not
    become. Returns ``(kept, truncated, shown_lines, total_lines)``.
    """
    total = sum(len(f["lines"]) for f in files)
    kept: list[dict[str, Any]] = []
    shown = 0
    size = 0
    for index, entry in enumerate(files):
        room_lines = max_lines - shown
        room_bytes = max_bytes - size
        if room_lines <= 0 or room_bytes <= 0:
            return kept, True, shown, total
        lines: list[str] = []
        weight = 0
        for line in entry["lines"]:
            if len(lines) >= room_lines or weight + len(line["text"]) + 1 > room_bytes:
                kept.append({**entry, "lines": lines, "cut": True})
                return kept, True, shown + len(lines), total
            lines.append(line)
            weight += len(line["text"]) + 1
        kept.append({**entry, "cut": False})
        shown += len(lines)
        size += weight
        if index == len(files) - 1:
            return kept, False, shown, total
    return kept, False, shown, total


async def _resolve_target(
    db: Any, task_id: int
) -> tuple[dict[str, Any] | None, str, str, str, str]:
    """Everything both readers need before git is asked anything.

    Returns ``(blank, sha, base, workspace, fallback)``. When ``blank`` is not
    None it is the finished answer — a stated reason, never an empty box.
    """
    from hub import services

    row = await repo.get_task(db, task_id)
    if not row:
        return _blank(NO_SUBMISSION, "task not found"), "", "", "", ""
    task = dict(row)
    sha = (task.get("submission_sha") or "").strip()
    branch = (task.get("branch") or "").strip()
    if not sha:
        return (
            _blank(
                NO_SUBMISSION,
                "коммит сдачи не закреплён — показывать нечего: "
                "у ветки нет зафиксированной точки, на которую вынесен вердикт",
            ),
            "",
            "",
            "",
            "",
        )

    diff_base = await review_evidence.resolve_diff_base(
        db, task_id, branch, offline=True
    )
    base = str(diff_base.get("base") or "")
    fallback = review_evidence.diff_command_for(diff_base, sha)

    def fail(state: str, reason: str) -> dict[str, Any]:
        return _blank(state, reason, sha=sha, base=base, fallback_command=fallback)

    try:
        ctx = await services.project_git_context(db, task_id)
        workspace = ctx.get("repo")
    except Exception as exc:  # noqa: BLE001 - a card must render regardless
        log.warning("diff for #%s: no project context: %s", task_id, exc)
        return (
            fail(
                NO_WORKSPACE,
                "рабочую копию проекта определить не удалось "
                f"({type(exc).__name__}) — подробности в логе хаба",
            ),
            sha,
            base,
            "",
            fallback,
        )
    if not workspace:
        return (
            fail(
                NO_WORKSPACE,
                "у проекта нет рабочей копии на этом хосте — дифф читается там, "
                "где лежит репозиторий",
            ),
            sha,
            base,
            "",
            fallback,
        )

    try:
        exists = await _ask(
            workspace, plugins.git_ops.commit_exists(workspace, sha), fail
        )
    except _Unavailable as unavailable:
        return unavailable.blank, sha, base, workspace, fallback
    if exists is None:
        return (
            fail(
                UNREADABLE,
                "рабочая копия не читается как git-репозиторий — "
                "это не значит, что коммита нет",
            ),
            sha,
            base,
            workspace,
            fallback,
        )
    if not exists:
        return (
            fail(
                UNREACHABLE_SHA,
                f"коммита {sha[:12]} нет в рабочей копии — возможно, ветка не "
                "подтянута. Это не «изменений нет»: сравнить было не с чем",
            ),
            sha,
            base,
            workspace,
            fallback,
        )
    if not base:
        return (
            fail(
                UNREADABLE,
                "база для сравнения не определена — "
                + str(diff_base.get("reason") or ""),
            ),
            sha,
            base,
            workspace,
            fallback,
        )
    return None, sha, base, workspace, fallback


async def _empty_reason(
    workspace: str, base: str, sha: str, fallback: str
) -> dict[str, Any] | None:
    """Why is this diff empty? ``None`` when it honestly is "nothing changed".

    A three-dot diff is computed from the merge base, so a commit that already
    lies in the history of the base produces an EMPTY diff no matter what the
    submission touched (#1239). Shown as-is that is the one lie the gate tells
    the reader to their face — the card is what a human reads, the code is not
    — and it reads as "изменений нет" on work that changed plenty.

    So the emptiness is interrogated rather than printed. Three answers, the
    same three git gives: already in the base (say so), not in the base (an
    honest empty diff, ``None`` here and the caller renders it), or the
    question could not be asked (``UNREADABLE``, never a silent empty screen).
    """
    merged = await _ask(
        workspace,
        plugins.git_ops.commit_in_base_history(workspace, base, sha),
        _failer(sha, base, fallback),
    )
    if merged is None:
        return _blank(
            UNREADABLE,
            f"дифф {base}...{sha[:12]} пуст, но предок ли коммит базы — git не "
            "ответил: пустоту показать нельзя, она недоказуема как «изменений нет»",
            sha=sha,
            base=base,
            fallback_command=fallback,
        )
    if merged:
        return _blank(
            MERGED_INTO_BASE,
            f"коммит {sha[:12]} уже лежит в истории базы {base}: трёхточечный "
            f"дифф {base}...{sha[:12]} схлопнулся в пустой и показывает не "
            "«изменений нет», а то, что сдача уже доставлена. Смотреть её "
            f"изменения надо по самому коммиту: git show {sha[:12]}",
            sha=sha,
            base=base,
            fallback_command=fallback,
        )
    return None


async def submission_files(db: Any, task_id: int) -> dict[str, Any]:
    return _publish(await _submission_files(db, task_id))


async def _submission_files(db: Any, task_id: int) -> dict[str, Any]:
    """Paths and line counts of the pinned submission (#825).

    The cheap half of ``submission_diff``: the change map needs to know which
    files a submission touched, and paying for every hunk on each gate render
    to learn that would undo the on-demand loading #824 introduced.
    """
    blank, sha, base, workspace, fallback = await _resolve_target(db, task_id)
    if blank is not None:
        return blank
    try:
        return await _files_of(workspace, base, sha, fallback)
    except _Unavailable as unavailable:
        return unavailable.blank


async def _files_of(
    workspace: str, base: str, sha: str, fallback: str
) -> dict[str, Any]:
    rows = await _ask(
        workspace,
        plugins.git_ops.commit_diff_stat(workspace, base, sha),
        _failer(sha, base, fallback),
    )
    if rows is None:
        return _blank(
            UNREADABLE,
            f"git не смог посчитать состав {base}...{sha[:12]}",
            sha=sha,
            base=base,
            fallback_command=fallback,
        )
    if not rows:
        empty = await _empty_reason(workspace, base, sha, fallback)
        if empty is not None:
            return empty
    files: list[dict[str, Any]] = [
        {"path": path, "added": added, "removed": removed}
        for added, removed, path in rows
    ]
    return {
        "state": READ,
        "reason": "",
        "files": files,
        "truncated": False,
        "shown_lines": 0,
        "total_lines": sum(f["added"] + f["removed"] for f in files),
        "fallback_command": fallback,
        "sha": sha,
        "base": base,
    }


async def submission_diff(db: Any, task_id: int) -> dict[str, Any]:
    return _publish(await _submission_diff(db, task_id))


async def _submission_diff(db: Any, task_id: int) -> dict[str, Any]:
    """The diff of a task's pinned submission, or a stated reason for its absence."""
    blank, sha, base, workspace, fallback = await _resolve_target(db, task_id)
    if blank is not None:
        return blank
    try:
        return await _diff_of(workspace, base, sha, fallback)
    except _Unavailable as unavailable:
        return unavailable.blank


async def _diff_of(
    workspace: str, base: str, sha: str, fallback: str
) -> dict[str, Any]:
    raw = await _ask(
        workspace,
        plugins.git_ops.commit_diff(workspace, base, sha),
        _failer(sha, base, fallback),
    )
    if raw is None:
        return _blank(
            UNREADABLE,
            f"git не смог посчитать {base}...{sha[:12]} — причина осталась в "
            "логе хаба, показанного диффа за этим нет",
            sha=sha,
            base=base,
            fallback_command=fallback,
        )

    files = split_files(raw)
    if not files:
        empty = await _empty_reason(workspace, base, sha, fallback)
        if empty is not None:
            return empty
    kept, truncated, shown, total = truncate(
        files, config.DIFF_MAX_LINES, config.DIFF_MAX_BYTES
    )
    return {
        "state": READ,
        "reason": "",
        "files": kept,
        "truncated": truncated,
        "shown_lines": shown,
        "total_lines": total,
        "shown_files": len(kept),
        "total_files": len(files),
        "fallback_command": fallback,
        "sha": sha,
        "base": base,
    }
