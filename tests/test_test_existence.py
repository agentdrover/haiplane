"""Unit tests for static AC locator existence resolution (#506)."""

from __future__ import annotations

from hub.services.test_existence import (
    MISSING,
    RESOLVABLE,
    UNKNOWN,
    collect_test_nodeids,
    resolve_ac_locators,
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
