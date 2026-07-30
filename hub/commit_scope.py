"""Commit-scope gate: catch foreign edits before they land in a task's PR (#361).

create_branch refuses to start in a dirty workspace, so the tree is clean when
a task begins. That was the whole justification for auto_commit's blanket
``git add -A`` — but it is a point-in-time check. Headless tasks (job_id set)
run in the shared main clone for their entire run and never get a worktree, so
anything written to that clone while the agent works is dirty at commit time
and indistinguishable from the task's own output by git alone.

This module supplies the only attribution the hub actually has: the task's own
declared ``affected_areas``. A dirty path outside every declared area is not
provably foreign — the agent may simply have touched more than the task
predicted — which is exactly why the gate escalates to a human instead of
deciding, and why it defaults to warn.
"""

from __future__ import annotations

import re

__all__ = ["parse_porcelain_paths", "foreign_paths"]

# XY status field: one or two codes, then whitespace, then the path.
_STATUS = re.compile(r"^[ MADRCU?!]{1,2}\s+")


def parse_porcelain_paths(porcelain: str) -> list[str]:
    """Repo-relative paths from ``git status --porcelain`` output.

    Renames arrive as ``R  old -> new``; both sides land in the commit, so both
    are returned. Paths containing whitespace or non-ASCII are quoted by git —
    the quotes are stripped, the escapes are left alone, because the result is
    shown to a human, never fed back to git.
    """
    paths: list[str] = []
    for line in porcelain.splitlines():
        # Not a fixed-column slice: callers hand us stripped output, and a
        # stripped " M app.py" loses its leading space, so line[3:] would eat
        # the first character of the name. Match the status letters instead.
        m = _STATUS.match(line)
        if not m:
            continue
        entry = line[m.end() :].strip()
        for part in entry.split(" -> ") if " -> " in entry else [entry]:
            part = part.strip()
            if part.startswith('"') and part.endswith('"') and len(part) > 1:
                part = part[1:-1]
            if part:
                paths.append(part)
    return paths


def _normalize(area: str) -> str:
    return area.strip().strip("/").replace("\\", "/")


def foreign_paths(dirty: list[str], affected_areas: list[str]) -> list[str]:
    """Dirty paths that fall outside every declared area.

    An area may name a file or a directory; a directory covers everything
    beneath it. With no areas declared there is nothing to compare against, so
    the answer is "no foreign paths" — the caller must treat an empty
    ``affected_areas`` as "cannot check", not as "checked and clean". Absence
    of a signal is never absence of a defect.
    """
    if not affected_areas:
        return []
    areas = [a for a in (_normalize(a) for a in affected_areas) if a]
    if not areas:
        return []
    out: list[str] = []
    for raw in dirty:
        path = _normalize(raw)
        if not any(path == a or path.startswith(a + "/") for a in areas):
            out.append(raw)
    return out
