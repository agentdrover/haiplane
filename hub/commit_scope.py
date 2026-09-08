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
    "is_test_path",
    "TEST_DIRS",
    "TEST_NAME_MARKERS",
    "code_without_tests",
    "tests_only",
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


# Directory names that make everything beneath them a test (#1179). Python
# puts tests in tests/, the JS world adds __tests__/ next to the code.
TEST_DIRS = frozenset({"tests", "test", "__tests__"})

# Filename markers of the dot-separated convention: the segment right before
# the extension. Suffixes of the NAME rather than extensions of one language,
# so .test.js and .spec.jsx are already covered and the next project does not
# come back here for a third edit. Languages beyond Python and JS/TS are left
# out on purpose — added when a project measures the need, not in advance.
TEST_NAME_MARKERS = frozenset({"test", "spec"})


def is_test_path(path: str) -> bool:
    """Whether a repo path is a test file (#855, #1179).

    By location and filename, never by content: the rule must be decidable
    from the diff alone, without reading or running anything. It answers
    "was a test touched", not "is the test any good" — the second question
    belongs to a reviewer, and pretending a path check answers it is how a
    cheap layer starts being read as an expensive one.

    It also has to know the conventions of the repo it is pointed at. Until
    #1179 it knew only Python's, so on snip-portal — the first non-Python
    project to reach the submission gate — no path in a frontend diff read as
    a test, :func:`code_without_tests` never took its "a test was touched"
    branch, and the report handed back the WHOLE diff as code without tests,
    listing three .test.tsx files as the proof that no test was there. A
    self-contradicting line does more damage than a missing one: a reader who
    unpicks it once stops believing the true lines standing next to it.
    """
    norm = _normalize(path)
    if not norm:
        return False
    parts = norm.split("/")
    if any(part in TEST_DIRS for part in parts[:-1]):
        return True
    name = parts[-1]
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    # foo.test.ts, foo.spec.tsx: the marker must be the segment IMMEDIATELY
    # before the extension. Matched anywhere in the name it would swallow
    # latest.test.helpers.ts, and a rule that wrongly says "a test is here"
    # is worse than one that wrongly says none is — nobody goes looking for
    # the check that stayed quiet.
    segments = name.split(".")
    return len(segments) >= 3 and segments[-2] in TEST_NAME_MARKERS


def code_without_tests(paths: list[str]) -> list[str]:
    """Code files in a diff that brought no test file with them (#855).

    Empty when the diff touched any test, when it touched no code at all, or
    when everything in it is routine. What comes back is the code the rule is
    speaking about, so the report can name files instead of scolding.

    Measured reason this exists (#854, 30 days): the paid reviewer confirmed
    findings in categories test-coverage, test-adequacy and
    missing-test-hides-defect at 124k tokens apiece. This costs nothing and
    runs on every submission rather than when a reviewer thinks to look.
    """
    candidates = [p for p in paths if _normalize(p) not in ROUTINE_PATHS]
    if any(is_test_path(p) for p in candidates):
        return []
    return [p for p in candidates if not is_test_path(p)]


def tests_only(paths: list[str]) -> bool:
    """Whether a diff touches tests and nothing else (#855).

    On a bug this is a signal worth naming — a test written to match already
    changed behaviour proves nothing about the fix — but never a refusal: a
    genuinely missing test IS the whole fix often enough.
    """
    candidates = [p for p in paths if _normalize(p) not in ROUTINE_PATHS]
    return bool(candidates) and all(is_test_path(p) for p in candidates)
