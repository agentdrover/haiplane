"""Unit tests for static AC locator existence resolution (#506)."""

from __future__ import annotations

from hub.services.test_existence import (
    BY_SOURCE,
    MISSING,
    PARAM_NOT_CHECKED,
    RESOLVABLE,
    UNKNOWN,
    UNPARSEABLE,
    resolve_ac_locators,
    resolve_locator_in_source,
)


class _AC:
    def __init__(self, ac_id, verifiable_by, test_ref):
        self.id = ac_id
        self.verifiable_by = verifiable_by
        self.test_ref = test_ref


_PRESENT = "def test_ok():\n    assert True\n"


def test_resolve_marks_present_missing_and_skips_non_test():
    acs = [
        _AC("AC-1", "test", "tests/test_a.py::test_ok"),  # resolvable
        _AC("AC-2", "test", "tests/test_a.py::test_gone"),  # valid locator, absent
        _AC("AC-3", "test", "free-text ref"),  # no valid locator
        _AC("AC-4", "manual", None),  # non-test → skipped
    ]
    by = {
        r["ac_id"]: r["status"]
        for r in resolve_ac_locators(acs, {"tests/test_a.py": _PRESENT})
    }
    assert by == {"AC-1": RESOLVABLE, "AC-2": MISSING, "AC-3": MISSING}
    assert "AC-4" not in by


def test_resolve_unknown_when_nothing_was_read():
    # No text at all (no ref, no workspace) → unknown, never a false missing.
    acs = [_AC("AC-1", "test", "tests/test_a.py::test_ok")]
    assert resolve_ac_locators(acs, None)[0]["status"] == UNKNOWN


def test_resolve_names_the_ref_it_read():
    acs = [_AC("AC-1", "test", "tests/test_a.py::test_ok")]
    res = resolve_ac_locators(
        acs, {"tests/test_a.py": _PRESENT}, ref_label="submission_sha 1234567890"
    )[0]
    assert res["reason"].endswith("[ref: submission_sha 1234567890]")


def test_resolve_matches_parametrized_test_by_bare_locator():
    # A bare function locator (the documented common form) runs every case.
    src = "import pytest\n\n\n@pytest.mark.parametrize('c', [1])\ndef test_p(c):\n    pass\n"
    acs = [_AC("AC-1", "test", "tests/test_a.py::test_p")]
    assert resolve_ac_locators(acs, {"tests/test_a.py": src})[0]["status"] == RESOLVABLE


# --- #764: reading the file instead of collecting ----------------------
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
    res = resolve_ac_locators(acs, {"tests/test_a.py": _SOURCE})[0]
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
    by = {r["ac_id"]: r for r in resolve_ac_locators(acs, sources)}
    assert by["AC-1"]["status"] == RESOLVABLE
    assert by["AC-2"]["status"] == RESOLVABLE
    assert by["AC-3"]["status"] == RESOLVABLE
    assert by["AC-4"]["status"] == UNPARSEABLE
    assert "broken.py" in by["AC-4"]["reason"]


def test_resolution_names_how_it_was_resolved():
    """#764 AC-5: a resolution says it was READ, not run, and where."""
    acs = [_AC("AC-1", "test", "tests/test_a.py::test_module_level")]

    by_source = resolve_ac_locators(acs, {"tests/test_a.py": _SOURCE})[0]

    assert by_source["status"] == RESOLVABLE
    assert BY_SOURCE in by_source["reason"]
    assert "tests/test_a.py:5" in by_source["reason"]  # file and line


def test_unreadable_file_stays_unknown():
    """#764 AC-4 at unit level: "could not read" never becomes "not there"."""
    acs = [_AC("AC-1", "test", "tests/test_a.py::test_module_level")]
    res = resolve_ac_locators(acs, {"tests/test_a.py": None})[0]
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
    # AC-1. A locator of another runner is judged by its own reader, never by
    # what pytest would list: on #1202 that answered `missing` about a test
    # written three lines into the file.
    ac = [_AC("AC-1", "test", _VITEST_LOCATOR)]

    found = resolve_ac_locators(ac, {_VITEST_FILE: _VITEST_SOURCE})[0]
    assert found["status"] == RESOLVABLE
    assert BY_SOURCE in found["reason"]

    # The file could not be read: unknown with a stated reason (#725).
    unread = resolve_ac_locators(ac, {_VITEST_FILE: None})[0]
    assert unread["status"] == UNKNOWN
    assert _VITEST_FILE in unread["reason"]

    # And the guard is not blanket silence: when the file is readable and the
    # test really is absent, `missing` is still the honest answer.
    absent = resolve_ac_locators(
        [_AC("AC-1", "test", f"{_VITEST_FILE}::a test nobody wrote")],
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


def test_unreadable_declaration_form_is_unknown_not_missing():
    # #1203, вторая находка ревью. ast делает "не нашёл" достоверным для
    # Python: настоящий парсер видел весь файл. Здесь читает регулярное
    # выражение, и форма, которую оно не осилило, выглядит ровно как тест,
    # которого не писали. Все три случая ниже воспроизведены ревьюером на
    # первой сдаче — до правки каждый отвечал `missing` про существующий тест.
    forms = [
        ('it.each`\n  $a | $b\n`("adds $a and $b", () => {});', "adds $a and $b"),
        ('const n = "still toggles";\nit(n, () => {});', "still toggles"),
    ]
    for src, name in forms:
        status, reason = resolve_locator_in_source(src, f"a/b.test.ts::{name}")
        assert status == UNKNOWN, (name, status, reason)
        # И причина обязана назвать, ЧТО именно помешало: "не смог прочитать
        # эту форму" — ответ, а "теста нет" на том же месте было обвинением.
        assert "cannot follow" in reason, (name, reason)


def test_a_nested_each_table_is_followed_by_the_scanner():
    # The old regular expression gave up on a call inside the table and said
    # unknown; the scanner balances the parentheses and finds the name.
    src = 'it.each(items.map(x => foo(x)))("maps then names", () => {});'
    status, _ = resolve_locator_in_source(src, "a/b.test.ts::maps then names")
    assert status == RESOLVABLE


def test_missing_survives_where_it_is_honest():
    # Осторожность не должна выродиться в вечное молчание: когда все
    # объявления в файле читаемы, а нужного имени среди них нет, `missing` —
    # правда, и она обязана остаться. Ложное "тест есть" хуже ложного
    # "теста нет", потому что первое никто не заметит.
    plain = 'it("a", () => {});\ntest("b", () => {});\n'
    assert resolve_locator_in_source(plain, "a/b.test.ts::c")[0] == MISSING
    assert resolve_locator_in_source(plain, "a/b.test.ts::b")[0] == RESOLVABLE


# ---- #1650: the full definition path ------------------------------------------

_PATH_SOURCE = """
from base_module import ImportedBase


def test_only_here():
    pass


class TestOwn:
    def test_method(self):
        pass


class LocalBase:
    def test_from_base(self):
        pass


class TestChild(LocalBase):
    pass


class TestForeign(ImportedBase):
    pass


class TestParam:
    @staticmethod
    def test_p(x):
        pass
"""


def _one(locator: str, source: str = _PATH_SOURCE) -> dict:
    return resolve_ac_locators(
        [_AC("AC-1", "test", locator)], {"tests/test_p.py": source}
    )[0]


def test_static_resolver_matches_the_full_definition_path():
    """#1650 AC-2: Class::method is a path, not the last name found anywhere."""
    found = _one("tests/test_p.py::TestOwn::test_method")
    assert found["status"] == RESOLVABLE
    assert "tests/test_p.py:10" in found["reason"]

    # The function exists, but not in this class: it must not satisfy the path.
    wrong_class = _one("tests/test_p.py::MissingClass::test_only_here")
    assert wrong_class["status"] == MISSING
    assert "MissingClass" in wrong_class["reason"]
    gone = _one("tests/test_p.py::MissingClass::test_x")
    assert gone["status"] == MISSING
    # ...nor does a method of another class stand in for this one.
    assert _one("tests/test_p.py::TestOwn::test_from_base")["status"] == MISSING
    # A module-level function is not reachable through a class either.
    assert _one("tests/test_p.py::TestOwn::test_only_here")["status"] == MISSING

    # Unambiguous local inheritance, in the same file.
    local = _one("tests/test_p.py::TestChild::test_from_base")
    assert local["status"] == RESOLVABLE
    assert _one("tests/test_p.py::TestChild::test_nothing")["status"] == MISSING

    # Inheritance from elsewhere cannot be followed without importing: unknown,
    # and the reason names the base — never "missing".
    imported = _one("tests/test_p.py::TestForeign::test_x")
    assert imported["status"] == UNKNOWN
    assert "ImportedBase" in imported["reason"]

    # [param-id]: the base definition is found and the parameter is not judged.
    param = _one("tests/test_p.py::TestParam::test_p[a-b]")
    assert param["status"] == RESOLVABLE
    assert PARAM_NOT_CHECKED in param["reason"]
    assert _one("tests/test_p.py::TestParam::test_q[a]")["status"] == MISSING


def test_what_a_static_reader_cannot_follow_is_unknown_not_missing():
    star = "from helpers import *\n\n\ndef test_a():\n    pass\n"
    assert _one("tests/test_p.py::test_b", star)["status"] == UNKNOWN

    assigned = "test_b = make_test()\n"
    assert _one("tests/test_p.py::test_b", assigned)["status"] == UNKNOWN

    hidden = "class TestX:\n    __test__ = False\n"
    assert _one("tests/test_p.py::TestX::test_a", hidden)["status"] == UNKNOWN

    decorated = "@wrap\nclass TestX:\n    pass\n"
    assert _one("tests/test_p.py::TestX::test_a", decorated)["status"] == UNKNOWN

    marked = "import pytest\n\n\n@pytest.mark.slow\nclass TestX:\n    pass\n"
    assert _one("tests/test_p.py::TestX::test_a", marked)["status"] == MISSING

    guarded = "if True:\n    def test_a():\n        pass\n"
    assert _one("tests/test_p.py::test_a", guarded)["status"] == UNKNOWN


def test_a_config_issue_makes_a_found_pytest_test_unknown():
    res = resolve_ac_locators(
        [_AC("AC-1", "test", "tests/test_p.py::test_only_here")],
        {"tests/test_p.py": _PATH_SOURCE},
        pytest_config_issue="pyproject.toml sets python_functions",
    )[0]
    assert res["status"] == UNKNOWN
    assert "python_functions" in res["reason"]
