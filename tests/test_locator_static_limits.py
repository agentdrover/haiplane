"""What a static reader owes the hub when the file is hostile or ambiguous (#1650).

The file under the reader is whatever an agent pushed. It must not be able to
crash the hub (recursion, memory, time), and where the tree cannot prove an
answer the answer is ``unknown``, never an accusation and never a "found" that
only looks like one.
"""

from __future__ import annotations

from types import SimpleNamespace

from hub.services.test_existence import (
    MISSING,
    RESOLVABLE,
    UNKNOWN,
    UNPARSEABLE,
    clear_locator_cache,
    resolve_locators_at_ref,
)
from tests.branch_code_support import FakeGit

_PATH = "tests/test_p.py"


def _ac(node: str, file: str = _PATH) -> SimpleNamespace:
    return SimpleNamespace(id="AC-1", verifiable_by="test", test_ref=f"{file}::{node}")


async def _status(files: dict, node: str, git: FakeGit | None = None) -> dict:
    clear_locator_cache()  # one fake sha names different files from call to call
    git = git or FakeGit(files)
    res = await resolve_locators_at_ref(
        git, "/repo", [_ac(node)], branch="task-42/work", base="main"
    )
    return res[0]


# ---- 1: a hostile file cannot take the hub down --------------------------------


async def test_deep_inheritance_is_unknown_not_a_crash():
    chain = "class C0:\n    pass\n" + "".join(
        f"class C{i}(C{i - 1}):\n    pass\n" for i in range(1, 600)
    )
    res = await _status({_PATH: chain}, "C599::test_x")
    assert res["status"] == UNKNOWN, res
    assert res["reason"]


async def test_a_very_deep_expression_is_unknown_not_a_crash():
    tail = "def test_a():\n    pass\n"
    nested = "x = " + "(" * 400 + "1" + ")" * 400 + "\n" + tail
    chain = "y = 1" + "+1" * 6000 + "\n" + tail
    for source in (nested, chain):
        res = await _status({_PATH: source}, "test_a")
        assert res["status"] in (UNKNOWN, UNPARSEABLE, RESOLVABLE), res
        assert res["status"] != MISSING
    assert (await _status({_PATH: chain}, "test_a"))["status"] != "crashed"


async def test_a_file_over_the_limit_is_unknown_and_never_parsed():
    big = "def test_a():\n    pass\n" + "# pad\n" * 60000  # ~360 KB
    res = await _status({_PATH: big}, "test_a")
    assert res["status"] == UNKNOWN, res
    assert "limit" in res["reason"], res


async def test_the_total_read_per_request_is_bounded():
    chunk = "def test_a():\n    pass\n" + "# pad\n" * 30000  # ~180 KB each
    files = {f"tests/test_{i}.py": chunk for i in range(8)}
    git = FakeGit(files)
    acs = [
        SimpleNamespace(
            id=f"AC-{i}", verifiable_by="test", test_ref=f"tests/test_{i}.py::test_a"
        )
        for i in range(8)
    ]
    res = await resolve_locators_at_ref(
        git, "/repo", acs, branch="task-42/work", base="main"
    )
    assert any(r["status"] == UNKNOWN and "limit" in r["reason"] for r in res), res
    assert all(r["status"] != MISSING for r in res)


# ---- 4: one commit, whatever the ref does meanwhile ------------------------------


async def test_every_read_goes_to_one_commit_sha():
    other = "b" * 40
    git = FakeGit(
        {_PATH: "def test_a():\n    pass\n"},
        refs={"task-42/work": "a" * 40},  # local-only branch name
        trees={other: {_PATH: "def test_other():\n    pass\n"}},
    )
    git.refs.pop("origin/task-42/work")
    git.moves = {2: ("task-42/work", other)}  # the branch advances mid-flight
    res = await resolve_locators_at_ref(
        git, "/repo", [_ac("test_a")], branch="task-42/work", base="main"
    )
    assert res[0]["status"] == RESOLVABLE, res
    assert git.refs_read and all(len(r) == 40 for r in git.refs_read), git.refs_read
    assert set(git.refs_read) == {"a" * 40}


async def test_an_unresolvable_ref_is_unknown():
    git = FakeGit({_PATH: "def test_a():\n    pass\n"})
    git.refs.clear()
    res = await resolve_locators_at_ref(
        git, "/repo", [_ac("test_a")], branch="task-42/work", base="main"
    )
    assert res[0]["status"] == UNKNOWN, res


# ---- 5: a "found" must not rest on a form the tree cannot vouch for ---------------


async def test_dynamic_forms_are_not_resolvable():
    cases = {
        "replacing decorator": (
            "from x import replace\n\n\n@replace\ndef test_x():\n    pass\n",
            "test_x",
        ),
        "decorated class, own method": (
            "from x import wrap\n\n\n@wrap\nclass TestX:\n    def test_m(self):\n"
            "        pass\n",
            "TestX::test_m",
        ),
        "imported base, own method": (
            "import unittest\n\n\nclass TestX(unittest.TestCase):\n"
            "    def test_m(self):\n        pass\n",
            "TestX::test_m",
        ),
        "metaclass": (
            "class TestX(metaclass=Meta):\n    def test_m(self):\n        pass\n",
            "TestX::test_m",
        ),
        "fixture is not a test": (
            "import pytest\n\n\n@pytest.fixture\ndef test_x():\n    pass\n",
            "test_x",
        ),
    }
    for name, (source, node) in cases.items():
        res = await _status({_PATH: source}, node)
        assert res["status"] == UNKNOWN, (name, res)


async def test_marks_and_plain_forms_stay_resolvable():
    source = (
        "import pytest\n\n\n@pytest.mark.slow\nclass TestX:\n"
        "    @pytest.mark.parametrize('a', [1])\n    def test_m(self, a):\n"
        "        pass\n"
    )
    assert (await _status({_PATH: source}, "TestX::test_m"))["status"] == RESOLVABLE


# ---- 6: an ambiguous definition is not a missing one ------------------------------

_AMBIGUOUS = (
    "import sys\n\nif sys.platform == 'linux':\n"
    "    class Base:\n        def test_x(self):\n            pass\n"
    "else:\n    class Base:\n        pass\n\n\n"
    "class TestX(Base):\n    pass\n"
)


async def test_a_conditional_base_is_unknown_not_missing():
    res = await _status({_PATH: _AMBIGUOUS}, "TestX::test_x")
    assert res["status"] == UNKNOWN, res


async def test_a_conditional_or_repeated_definition_is_unknown():
    cond = "if FLAG:\n    def test_a():\n        pass\n"
    twice = "def test_a():\n    pass\n\n\ndef test_a():\n    pass\n"
    for source in (cond, twice):
        assert (await _status({_PATH: source}, "test_a"))["status"] == UNKNOWN


# ---- 7: pytest's python_* options, read as data -----------------------------------


async def _with_config(name: str, text: str | bytes) -> dict:
    return await _status({_PATH: "def test_a():\n    pass\n", name: text}, "test_a")


async def test_python_options_in_every_syntax_make_the_answer_unknown():
    toml_quoted = '[tool.pytest.ini_options]\n"python_functions" = ["check_*"]\n'
    toml_plain = "[tool.pytest.ini_options]\npython_classes = 'Check'\n"
    ini_colon = "[pytest]\npython_functions: check_*\n"
    cfg = "[tool:pytest]\npython_files = check_*.py\n"
    tox = "[pytest]\npython_files = check_*.py\n"
    for name, text in (
        ("pyproject.toml", toml_quoted),
        ("pyproject.toml", toml_plain),
        ("pytest.ini", ini_colon),
        ("setup.cfg", cfg),
        ("tox.ini", tox),
    ):
        res = await _with_config(name, text)
        assert res["status"] == UNKNOWN, (name, text, res)
        assert "python_" in res["reason"], res


async def test_default_or_absent_python_options_do_not_block_the_answer():
    defaults = '[tool.pytest.ini_options]\npython_functions = ["test"]\n'
    other = '[tool.pytest.ini_options]\naddopts = "-q"\n'
    for text in (defaults, other):
        res = await _with_config("pyproject.toml", text)
        assert res["status"] == RESOLVABLE, (text, res)


async def test_an_unreadable_config_is_not_taken_for_no_config():
    broken_toml = "[tool.pytest.ini_options\npython_functions = ["
    res = await _with_config("pyproject.toml", broken_toml)
    assert res["status"] == UNKNOWN, res
    res = await _with_config("pytest.ini", b"\xff\xfe\x00 [pytest")
    assert res["status"] == UNKNOWN, res


async def test_a_config_that_cannot_be_read_at_all_is_not_taken_for_no_config():
    class Flaky(FakeGit):
        async def read_file_at_ref(self, repo, ref, path, **kw):
            if path == "pyproject.toml":
                return {"state": "unreadable", "reason": "io", "size": 0}
            return await super().read_file_at_ref(repo, ref, path, **kw)

    files = {_PATH: "def test_a():\n    pass\n", "pyproject.toml": "[x]\n"}
    res = await _status(files, "test_a", Flaky(files))
    assert res["status"] == UNKNOWN, res
    assert "pyproject.toml" in res["reason"]

    huge = {_PATH: "def test_a():\n    pass\n", "pyproject.toml": "# pad\n" * 60000}
    res = await _status(huge, "test_a")
    assert res["status"] == UNKNOWN, res


class _AstWithParse:
    """The ``ast`` module as the worker code sees it, with only ``parse`` replaced.

    Patching ``ast.parse`` itself would also break pytest's own reporting.
    """

    def __init__(self, parse):
        self.parse = parse

    def __getattr__(self, name):
        import ast

        return getattr(ast, name)


def test_parser_exhaustion_is_unknown_whatever_raised_it(monkeypatch):
    # In process on purpose: the patch has to reach the analysis code.
    from hub.services import locator_worker
    from hub.services.test_existence import resolve_ac_locators

    ac = [_ac("test_a")]
    source = {_PATH: "def test_a():\n    pass\n"}
    for exc in (RecursionError, MemoryError):

        def boom(*_a, _exc=exc, **_k):
            raise _exc()

        monkeypatch.setattr(locator_worker, "ast", _AstWithParse(boom))
        res = resolve_ac_locators(ac, source)[0]
        assert res["status"] == UNKNOWN, (exc, res)
        monkeypatch.undo()

        monkeypatch.setattr(locator_worker._PyFile, "resolve", boom)
        res = resolve_ac_locators(ac, source)[0]
        assert res["status"] == UNKNOWN, (exc, res)
        monkeypatch.undo()


# ---- round 3: any other binding of the name, and hooks that change a class ----------


async def test_any_other_binding_of_the_name_makes_it_unknown():
    forms = {
        "del": "def test_a():\n    pass\n\ndel test_a\n",
        "walrus": "def test_a():\n    pass\n\nif (test_a := 1):\n    pass\n",
        "with as": "def test_a():\n    pass\n\nwith open('f') as test_a:\n    pass\n",
        "for target": "def test_a():\n    pass\n\nfor test_a in range(3):\n    pass\n",
        "except as": (
            "def test_a():\n    pass\n\ntry:\n    pass\nexcept E as test_a:\n    pass\n"
        ),
        "match capture": (
            "def test_a():\n    pass\n\nmatch 1:\n    case test_a:\n        pass\n"
        ),
        "global in a function": (
            "def test_a():\n    pass\n\n\ndef rebinder():\n    global test_a\n"
            "    test_a = 1\n"
        ),
        "star import": "from helpers import *\n\n\ndef test_a():\n    pass\n",
    }
    for name, source in forms.items():
        res = await _status({_PATH: source}, "test_a")
        assert res["status"] == UNKNOWN, (name, res)


async def test_class_creation_hooks_in_the_chain_make_it_unknown():
    hooks = {
        "__init_subclass__": (
            "class Base:\n    def __init_subclass__(cls, **kw):\n        pass\n\n\n"
            "class TestX(Base):\n    pass\n"
        ),
        "__class_getitem__": (
            "class Base:\n    def __class_getitem__(cls, item):\n        return cls\n\n\n"
            "class TestX(Base):\n    pass\n"
        ),
        "own hook": ("class TestX:\n    def __init_subclass__(cls):\n        pass\n"),
    }
    for name, source in hooks.items():
        res = await _status({_PATH: source}, "TestX::test_x")
        assert res["status"] == UNKNOWN, (name, res)


# ---- round 3: pytest's configuration in ANY directory above the file ----------------


async def _in_dirs(extra: dict[str, str | bytes], unreadable: set[str] | None = None):
    files = {_PATH: "def test_a():\n    pass\n", **extra}
    git = FakeGit(files)
    git.unreadable = unreadable or set()
    return await _status(files, "test_a", git)


async def test_a_nested_pytest_config_or_collection_hook_makes_it_unknown():
    cases = {
        "nested ini": {"tests/pytest.ini": "[pytest]\npython_functions = check_*\n"},
        "nested toml": {
            "tests/pyproject.toml": "[tool.pytest.ini_options]\npython_classes = 'Check'\n"
        },
        "nested conftest hook": {
            "tests/conftest.py": "def pytest_collection_modifyitems(items):\n    pass\n"
        },
        "root conftest hook": {
            "conftest.py": "def pytest_generate_tests(metafunc):\n    pass\n"
        },
        "makeitem hook": {
            "tests/conftest.py": "def pytest_pycollect_makeitem(collector, name, obj):\n    pass\n"
        },
    }
    for name, extra in cases.items():
        res = await _in_dirs(extra)
        assert res["status"] == UNKNOWN, (name, res)


async def test_a_benign_conftest_and_config_leave_the_answer_alone():
    res = await _in_dirs(
        {
            "tests/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef thing():\n    pass\n",
            "tests/pytest.ini": "[pytest]\naddopts = -q\n",
        }
    )
    assert res["status"] == RESOLVABLE, res


async def test_an_unreadable_conftest_above_the_file_is_unknown():
    res = await _in_dirs({"tests/conftest.py": "x = 1\n"}, {"tests/conftest.py"})
    assert res["status"] == UNKNOWN, res


# ---- round 4: uncertainty about collection comes BEFORE any claim of absence ---------


async def test_an_absent_name_is_unknown_when_collection_can_add_tests():
    # test_generated is not written anywhere, and a conftest.py hook may well
    # create it: absence cannot be claimed.
    generated = {
        "tests/conftest.py": "def pytest_collectstart(collector):\n    pass\n",
    }
    res = await _in_dirs_for("test_generated", generated)
    assert res["status"] == UNKNOWN, res
    broken = {"pyproject.toml": "[tool.pytest.ini_options\npython_functions = ["}
    res = await _in_dirs_for("test_generated", broken)
    assert res["status"] == UNKNOWN, res
    res = await _in_dirs_for("test_generated", {})
    assert res["status"] == MISSING, res  # and without doubt it is still missing


async def _in_dirs_for(node: str, extra: dict[str, str]):
    files = {_PATH: "def test_a():\n    pass\n", **extra}
    return await _status(files, node, FakeGit(files))


async def test_imported_hooks_and_plugins_in_a_conftest_make_it_unknown():
    cases = {
        "imported hook": "from helpers import pytest_collection_modifyitems\n",
        "aliased hook": "from helpers import pytest_collection_modifyitems as hook\n",
        "plugin list": "pytest_plugins = ['helpers.plugin']\n",
        "plugin import": "from helpers import pytest_something\n",
        "star import": "from helpers import *\n",
    }
    for name, conftest in cases.items():
        res = await _in_dirs_for("test_a", {"tests/conftest.py": conftest})
        assert res["status"] == UNKNOWN, (name, res)
        res = await _in_dirs_for("test_gone", {"tests/conftest.py": conftest})
        assert res["status"] == UNKNOWN, (name, res)


async def test_a_vitest_declaration_past_the_scan_limit_is_unknown_not_missing():
    for text in (
        "test" + " " * 2100 + "('late name', () => {});\n",
        "it(" + " " * 2100 + "'late name', () => {});\n",
        "it('" + "x" * 2100 + "', () => {});\n",
    ):
        res = await _vitest(text)
        assert res["status"] == UNKNOWN, (text[:20], res)


async def _vitest(text: str) -> dict:
    clear_locator_cache()
    files = {"web/a.test.ts": text}
    git = FakeGit(files)
    return (
        await resolve_locators_at_ref(
            git,
            "/repo",
            [_ac("another name", "web/a.test.ts")],
            branch="task-42/work",
            base="main",
        )
    )[0]
