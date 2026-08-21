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

import logging
from typing import Any

from hub import config
from hub import repository as repo
from hub.integrations.registry import plugins
from hub.services import review_evidence

log = logging.getLogger("hub")

# What the reader is looking at, or why they are not.
READ = "read"  # the diff is here
NO_SUBMISSION = "no_submission"  # nothing was pinned, so there is nothing to show
UNREACHABLE_SHA = "unreachable_sha"  # the workspace does not carry that commit
NO_WORKSPACE = "no_workspace"  # nowhere to look
UNREADABLE = "unreadable"  # git was there and still could not answer


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


def _hunk_start(header: str) -> int | None:
    """New-side start line of a ``@@ -a,b +c,d @@`` header, or None."""
    plus = header.find("+")
    if plus < 0:
        return None
    token = header[plus + 1 :].split()[0] if header[plus + 1 :].split() else ""
    number = token.split(",", 1)[0]
    return int(number) if number.isdigit() else None


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
    current: dict[str, Any] | None = None
    new_line: int | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = line.split(" b/", 1)[-1].strip() if " b/" in line else line
            current = {"path": path, "added": 0, "removed": 0, "lines": []}
            files.append(current)
            new_line = None
            continue
        if current is None:
            # Output before any file header (git rarely emits this) — keep it
            # under a named entry instead of discarding it.
            current = {"path": "(diff preamble)", "added": 0, "removed": 0, "lines": []}
            files.append(current)
            new_line = None

        number: int | None = None
        if line.startswith("@@"):
            new_line = _hunk_start(line)
        elif new_line is not None and not line.startswith("---"):
            if line.startswith("+"):
                number, new_line = new_line, new_line + 1
            elif line.startswith("-"):
                number = None
            elif line.startswith(" ") or line == "":
                number, new_line = new_line, new_line + 1

        current["lines"].append({"text": line, "new_line": number})
        if line.startswith("+") and not line.startswith("+++"):
            current["added"] += 1
        elif line.startswith("-") and not line.startswith("---"):
            current["removed"] += 1
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

    diff_base = await review_evidence.resolve_diff_base(db, task_id, branch)
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
            fail(NO_WORKSPACE, f"рабочую копию проекта определить не удалось: {exc}"),
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

    exists = await plugins.git_ops.commit_exists(workspace, sha)
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


async def submission_files(db: Any, task_id: int) -> dict[str, Any]:
    """Paths and line counts of the pinned submission (#825).

    The cheap half of ``submission_diff``: the change map needs to know which
    files a submission touched, and paying for every hunk on each gate render
    to learn that would undo the on-demand loading #824 introduced.
    """
    blank, sha, base, workspace, fallback = await _resolve_target(db, task_id)
    if blank is not None:
        return blank
    rows = await plugins.git_ops.commit_diff_stat(workspace, base, sha)
    if rows is None:
        return _blank(
            UNREADABLE,
            f"git не смог посчитать состав {base}...{sha[:12]}",
            sha=sha,
            base=base,
            fallback_command=fallback,
        )
    files = [
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
    """The diff of a task's pinned submission, or a stated reason for its absence."""
    blank, sha, base, workspace, fallback = await _resolve_target(db, task_id)
    if blank is not None:
        return blank

    raw = await plugins.git_ops.commit_diff(workspace, base, sha)
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
