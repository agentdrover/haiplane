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

__all__ = [
    "parse_porcelain_paths",
    "foreign_paths",
    "ROUTINE_PATHS",
    "SCOPE_GROWTH_MARKER",
]

# #890: the opening words of the update that records areas accepted at
# submission. A marker rather than a new column: the growth is an event in the
# feed, and the review brief finds it the same way has_plan_updates finds a
# plan. Changing this string changes what the reviewer sees — it is a contract.
SCOPE_GROWTH_MARKER = "Объём вырос по ходу работы:"

# Files a task changes as a consequence of doing its work rather than as its
# subject (#550). Declaring them would be noise, and demanding it would train
# authors to pad affected_areas until the field means nothing. Kept short and
# explicit on purpose: an open-ended ignore list would quietly swallow real
# surfaces. Anything added here needs the same argument — changed BY the work,
# never the point OF it.
ROUTINE_PATHS = frozenset({"uv.lock", "poetry.lock", "package-lock.json"})

# The XY status field, then whitespace, then the path. Not a fixed-column
# slice: callers hand us stripped output, and a stripped " M app.py" loses the
# leading space, so line[3:] would eat the first character of the name.
_STATUS = re.compile(r"^[ MADRCU?!]{1,2}\s+")


def parse_porcelain_paths(porcelain: str) -> list[str]:
    """Repo-relative paths from ``git status --porcelain -z`` output.

    The ``-z`` form is not a preference: without it git quotes and escapes any
    path that is not plain ASCII, so ``docs/Тест.md`` arrives as
    ``"docs/\\320\\242..."``. These paths are not only shown to a human — they
    go to :func:`foreign_paths` and are COMPARED against the task's declared
    areas, where an escaped name matches nothing and a file the task declared
    reads as somebody else's (#555). The original docstring here justified
    leaving the escapes because the result "is never fed back to git": true of
    git, and wrong about the other consumer, written in the same commit.

    Records are NUL-separated. A rename or copy is two records — ``R<sp>new``
    followed by the source path with no status field of its own — and both
    sides land in the commit, so both are returned.
    """
    paths: list[str] = []
    records = [r for r in porcelain.split("\0") if r]
    expect_source = False
    for record in records:
        if expect_source:
            # The bare second half of a rename: no status field to match on.
            expect_source = False
            if record.strip():
                paths.append(record.strip())
            continue
        m = _STATUS.match(record)
        if not m:
            continue
        path = record[m.end() :].strip()
        if path:
            paths.append(path)
        expect_source = record.lstrip()[:1] in ("R", "C")
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
