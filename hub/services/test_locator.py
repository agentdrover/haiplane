"""Machine-resolvable test locators for acceptance criteria (#505, #1203).

An AC with ``verifiable_by=test`` should point at a concrete test via a
locator stored in ``test_ref`` — ``path/to/file::node`` — instead of a
free-text hint. This module parses and validates that locator so downstream
layers can check the test exists (#506) and run it (#507). It never mutates
data: the ``AcceptanceCriterion`` model stays permissive so reading legacy
rows can never fail; enforcement lives only in the refine path and is gated
by ``config.SDD_AC_LOCATOR``.

WHICH RUNNER a locator belongs to is part of reading it (#1203). The module
used to accept pytest nodeids and nothing else, so on the hub's first
non-Python project no criterion could be declared machine-verifiable at all
— measured as a 422 on #1202. Widening the accepted shape is only half the
answer, and the dangerous half on its own: a shape the hub accepts but
cannot resolve turns an honest "I do not know this runner" into a false
"that test does not exist" (#725, #498). So the accepted shapes live in one
registry here, and :mod:`hub.services.test_existence` is required to carry a
resolver for every runner in it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

PYTEST = "pytest"
VITEST = "vitest"


@dataclass(frozen=True)
class Runner:
    """One test runner's locator shape.

    ``suffixes`` decides which runner a locator belongs to — the file it names
    settles that on its own, without the hub having to be told. ``shape`` then
    says what a well-formed locator for that runner looks like; the two
    runners differ, and forcing one syntax on both is what would silently
    mangle the other (a pytest node is a Python identifier chain, a vitest
    test name is free text).
    """

    name: str
    suffixes: tuple[str, ...]
    shape: re.Pattern[str]
    example: str


# A pytest nodeid: a .py path, "::", then one or more dotted node segments,
# optionally a "[param-id]" suffix. Kept deliberately permissive on the param
# body (pytest allows spaces, dashes, unicode) but strict on the shape.
_PYTEST_SHAPE = re.compile(r"^[\w./\-]+\.py::[A-Za-z_]\w*(::[A-Za-z_]\w*)*(\[.+\])?$")

# A vitest locator: a JS/TS path, "::", then the test's own name. The name is
# whatever the author passed to it()/test() — spaces, punctuation, unicode and
# non-Latin scripts are all ordinary there, so the only rule is that it is not
# empty. Carrying pytest's identifier rule across would reject almost every
# real vitest name while looking like a shape check.
_VITEST_SHAPE = re.compile(r"^[\w./\-]+\.(ts|tsx|js|jsx|mjs|cjs)::.*\S.*$")

RUNNERS: tuple[Runner, ...] = (
    Runner(
        name=PYTEST,
        suffixes=(".py",),
        shape=_PYTEST_SHAPE,
        example="tests/test_poller.py::test_ci_absent_dispatches_review",
    ),
    Runner(
        name=VITEST,
        suffixes=(".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
        shape=_VITEST_SHAPE,
        example="src/lib/recent-inspections.test.ts::still toggles without storage",
    ),
)


def _path_of(value: str) -> str:
    return value.split("::", 1)[0]


def runner_of(value: str | None) -> str:
    """Which runner ``value`` names a test for; ``""`` when nothing matches.

    Decided by the file the locator names, not by the project it belongs to:
    pytest cannot collect a ``.ts`` file whatever the project says, and a
    locator that names one is a vitest locator even in a mixed repository.
    """
    if not value:
        return ""
    path = _path_of(value.strip()).lower()
    for runner in RUNNERS:
        if path.endswith(runner.suffixes):
            return runner.name
    return ""


def is_valid_test_locator(value: str | None) -> bool:
    """Whether ``value`` is a well-formed locator for a runner we know."""
    if not value:
        return False
    text = value.strip()
    name = runner_of(text)
    for runner in RUNNERS:
        if runner.name == name:
            return bool(runner.shape.match(text))
    return False


def parse_test_locator(value: str | None) -> tuple[str, str] | None:
    """Best-effort split into ``(file_path, nodeid)``; ``None`` if not a locator.

    Best-effort by design (#505 AC-3): a legacy free-text ``test_ref`` simply
    returns ``None`` instead of raising, so existing rows keep parsing.
    """
    if value is None or not is_valid_test_locator(value):
        return None
    nodeid = value.strip()
    return _path_of(nodeid), nodeid


def _verifiable_by(ac: Any) -> str:
    vb = getattr(ac, "verifiable_by", None)
    # Обещали str, а при отсутствующем поле отдавали None: сравнение с "test"
    # молча давало False, а .lower() у вызывающего дал бы AttributeError.
    return str(getattr(vb, "value", vb) or "")


def _ac_id(ac: Any) -> str:
    return getattr(ac, "id", "?")


def _examples() -> str:
    return "; ".join(f"{r.name}: '{r.example}'" for r in RUNNERS)


def validate_test_locators(acs: Any, *, enforce: bool) -> None:
    """Reject verifiable_by=test AC without a valid locator when ``enforce`` (#505).

    Applied to every AC WRITE path, never to DB reads. It used to guard only
    the bulk-refine payload, so the single add/upsert/replace calls let an
    unresolvable locator through with the policy set to require — 425 of the
    695 stored locators were unresolvable by the time that was noticed (#596).

    Never on reads: those 425 rows must keep loading. When ``enforce`` is
    False this is a no-op, so off/warn behaviour is unchanged. Non-test AC
    (manual/log_check/ui_check) never require a locator (AC-2).
    """
    if not enforce or not acs:
        return
    bad = [
        _ac_id(ac)
        for ac in acs
        if _verifiable_by(ac) == "test"
        and not is_valid_test_locator(getattr(ac, "test_ref", None))
    ]
    if bad:
        # A comma-separated list is the most common near-miss, and the plain
        # "no valid locator" wording reads as if the value were missing rather
        # than plural. Naming it stops the author guessing (#596).
        listish = [
            _ac_id(ac)
            for ac in acs
            if _ac_id(ac) in bad and "," in (getattr(ac, "test_ref", None) or "")
        ]
        extra = ""
        if listish:
            extra = (
                f" Criteria {', '.join(listish)} name several tests separated by "
                "commas: test_ref holds exactly ONE nodeid. Point it at the test "
                "that proves the criterion and mention the others in the 'then' "
                "text."
            )
        raise HTTPException(
            422,
            f"acceptance criteria {', '.join(bad)} have verifiable_by=test but "
            "no valid test locator in test_ref. Provide one of the known forms "
            f"— {_examples()}.{extra} The whole request was rejected: no "
            "fields were written, not even the ones that validated, and in a "
            "bulk refine no task in the batch was touched — resend the whole "
            "request once the locators are in place.",
        )
