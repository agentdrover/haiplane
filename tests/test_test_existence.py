"""Unit tests for static AC locator existence resolution (#506)."""

from __future__ import annotations

from hub.services.test_existence import (
    BY_COLLECTION,
    BY_SOURCE,
    MISSING,
    RESOLVABLE,
    UNKNOWN,
    UNPARSEABLE,
    collect_test_nodeids,
    resolve_ac_locators,
    resolve_locator_in_source,
)


class _AC:
    def __init__(self, ac_id, verifiable_by, test_ref):
        self.id = ac_id
        self.verifiable_by = verifiable_by
        self.test_ref = test_ref


def test_resolve_marks_present_missing_and_skips_non_test():
    collected = {"tests/test_a.py::test_ok"}
    acs = [
        _AC("AC-1", "test", "tests/test_a.py::test_ok"),  # resolvable
        _AC("AC-2", "test", "tests/test_a.py::test_gone"),  # valid locator, absent
        _AC("AC-3", "test", "free-text ref"),  # no valid locator
        _AC("AC-4", "manual", None),  # non-test → skipped
    ]
    by = {r["ac_id"]: r["status"] for r in resolve_ac_locators(acs, collected)}
    assert by == {"AC-1": RESOLVABLE, "AC-2": MISSING, "AC-3": MISSING}
    assert "AC-4" not in by


def test_resolve_unknown_when_collection_unavailable():
    # collected=None (collection could not run) → unknown, never false missing.
    acs = [_AC("AC-1", "test", "tests/test_a.py::test_ok")]
    res = resolve_ac_locators(acs, None)
    assert res[0]["status"] == UNKNOWN


async def test_collect_returns_none_without_repo_path():
    assert await collect_test_nodeids(None) is None
    assert await collect_test_nodeids("") is None


def test_resolve_matches_parametrized_test_by_bare_locator():
    # #506 fix: pytest emits only the parametrized ids; a bare function locator
    # (the documented common form) still runs every case and must resolve.
    collected = {"tests/test_a.py::test_p[case1]", "tests/test_a.py::test_p[case2]"}
    acs = [_AC("AC-1", "test", "tests/test_a.py::test_p")]
    assert resolve_ac_locators(acs, collected)[0]["status"] == RESOLVABLE


def test_resolve_still_missing_for_unknown_base():
    # The base-nodeid relaxation must not turn a genuinely absent test green.
    collected = {"tests/test_a.py::test_p[case1]"}
    acs = [_AC("AC-1", "test", "tests/test_a.py::test_other")]
    assert resolve_ac_locators(acs, collected)[0]["status"] == MISSING


# --- #764: reading the file when collection cannot run ----------------------
#
# At review time no working tree holds the submitted branch — submit removes
# the task's worktree — so collection is not merely unavailable, it is the
# wrong instrument. The file's own text, taken from the submitted commit,
# answers the only question a locator asks: is this test written here.


_SOURCE = """
import pytest


def test_module_level():
    assert True


@pytest.mark.parametrize("case", [1, 2])
def test_parametrised(case):
    assert case


class TestGroup:
    def test_method(self):
        assert True
"""


def test_static_resolution_reports_missing_with_what_it_looked_for():
    """#764 AC-2: an absent test is named as absent, not shrugged at."""
    acs = [_AC("AC-1", "test", "tests/test_a.py::test_never_written")]
    res = resolve_ac_locators(acs, None, {"tests/test_a.py": _SOURCE})[0]
    assert res["status"] == MISSING
    assert "test_never_written" in res["reason"]
    assert "tests/test_a.py" in res["reason"]


def test_static_resolution_handles_method_parametrised_and_unparseable():
    """#764 AC-3: the shapes people actually write, and an honest unparseable."""
    acs = [
        _AC("AC-1", "test", "tests/test_a.py::TestGroup::test_method"),
        _AC("AC-2", "test", "tests/test_a.py::test_parametrised"),
        _AC("AC-3", "test", "tests/test_a.py::test_parametrised[1]"),
        _AC("AC-4", "test", "tests/broken.py::test_anything"),
    ]
    sources = {"tests/test_a.py": _SOURCE, "tests/broken.py": "def (:"}
    by = {r["ac_id"]: r for r in resolve_ac_locators(acs, None, sources)}
    assert by["AC-1"]["status"] == RESOLVABLE
    assert by["AC-2"]["status"] == RESOLVABLE
    assert by["AC-3"]["status"] == RESOLVABLE
    assert by["AC-4"]["status"] == UNPARSEABLE
    assert "broken.py" in by["AC-4"]["reason"]


def test_resolution_names_how_it_was_resolved():
    """#764 AC-5: collection and reading the file are not equal evidence.

    One says pytest would collect the test; the other says a function by that
    name is written in the file. A brief that reported both as a bare
    "resolvable" would let the weaker of the two pass for the stronger.
    """
    acs = [_AC("AC-1", "test", "tests/test_a.py::test_module_level")]

    by_collection = resolve_ac_locators(acs, {"tests/test_a.py::test_module_level"})[0]
    by_source = resolve_ac_locators(acs, None, {"tests/test_a.py": _SOURCE})[0]

    assert by_collection["status"] == by_source["status"] == RESOLVABLE
    assert by_collection["reason"] == BY_COLLECTION
    assert BY_SOURCE in by_source["reason"]
    assert "tests/test_a.py:5" in by_source["reason"]  # file and line


def test_unreadable_file_stays_unknown():
    """#764 AC-4 at unit level: "could not read" never becomes "not there"."""
    acs = [_AC("AC-1", "test", "tests/test_a.py::test_module_level")]
    res = resolve_ac_locators(acs, None, {"tests/test_a.py": None})[0]
    assert res["status"] == UNKNOWN
    assert res["status"] != MISSING


# ---- A locator of another runner is never called missing (#1203) ----

_VITEST_FILE = "frontend/src/lib/recent-inspections.test.ts"
_VITEST_NAME = "still toggles when the storage object itself is unreachable"
_VITEST_LOCATOR = f"{_VITEST_FILE}::{_VITEST_NAME}"
_VITEST_SOURCE = """import { describe, it } from "vitest";

describe("признак раскрытия подсписка", () => {
  it("still toggles when the storage object itself is unreachable", () => {});
});
"""


def test_foreign_runner_locator_is_never_missing():
    # AC-1. The measured defect: `collected` comes from pytest and speaks only
    # for pytest, but every locator was judged against it. On #1202 that
    # answered `missing`, "locator does not match any collected test", about a
    # test written three lines into the file — the false missing this module's
    # own docstring promises never to produce.
    pytest_collected = {"tests/test_poller.py::test_a"}
    ac = [_AC("AC-1", "test", _VITEST_LOCATOR)]

    # Path 1: pytest collection ran and (of course) does not list this test.
    with_source = resolve_ac_locators(
        ac, pytest_collected, {_VITEST_FILE: _VITEST_SOURCE}
    )[0]
    assert with_source["status"] == RESOLVABLE
    assert BY_SOURCE in with_source["reason"]

    # Path 2: collection ran, but the file itself could not be read. Unknown
    # with a stated reason — "could not look" is not "the answer is no" (#725).
    unread = resolve_ac_locators(ac, pytest_collected, {_VITEST_FILE: None})[0]
    assert unread["status"] == UNKNOWN
    assert _VITEST_FILE in unread["reason"]

    # Path 3: no collection at all — the pre-existing fallback route.
    no_collection = resolve_ac_locators(ac, None, {_VITEST_FILE: _VITEST_SOURCE})[0]
    assert no_collection["status"] == RESOLVABLE

    # And the guard is not blanket silence: when the file is readable and the
    # test really is absent, `missing` is still the honest answer.
    absent = resolve_ac_locators(
        [_AC("AC-1", "test", f"{_VITEST_FILE}::a test nobody wrote")],
        pytest_collected,
        {_VITEST_FILE: _VITEST_SOURCE},
    )[0]
    assert absent["status"] == MISSING


def test_vitest_source_reading_finds_the_named_test():
    # The vitest resolver answers existence by reading, exactly as BY_SOURCE
    # claims — no stronger. It must survive the quote styles and the decorated
    # forms that appear in real suites.
    src = """
it.each([1, 2])("parametrised %i", () => {});
test('single quoted name', () => {});
it(`backticked name`, () => {});
it("name with \\"quotes\\" inside", () => {});
"""
    for name in (
        "parametrised %i",
        "single quoted name",
        "backticked name",
        'name with "quotes" inside',
    ):
        status, reason = resolve_locator_in_source(src, f"a/b.test.ts::{name}")
        assert status == RESOLVABLE, (name, reason)


def test_vitest_names_keep_characters_pytest_would_trim():
    # A pytest node is trimmed at "[" and read after the last "::"; a vitest
    # name owns both characters. Trimming them would send the resolver hunting
    # for a different test and report a real one as absent.
    src = 'it("renders [draft] :: with a colon", () => {});\n'
    status, _ = resolve_locator_in_source(
        src, "a/b.test.ts::renders [draft] :: with a colon"
    )
    assert status == RESOLVABLE


def test_unknown_runner_says_so_instead_of_unparseable():
    # Before #1203 every file went through ast, so a TypeScript test came back
    # `unparseable` — true in the letter, useless in fact, because the file
    # parses perfectly for the tool that owns it.
    status, reason = resolve_locator_in_source(_VITEST_SOURCE, _VITEST_LOCATOR)
    assert status != UNPARSEABLE
