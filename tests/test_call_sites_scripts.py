"""scripts/ joins the call-site index without inventing new false alarms (#1935).

Measured 20.10: a changed function in scripts/ came back ``no_callers`` even
when a sibling script called it (``_collect_targets`` -> scripts/mutation_changed.py),
because ``INDEX_SUBDIRS`` only walked ``hub`` and ``tests``. The fix walks
``scripts/`` too, but a bare name match there is cheap: two scripts commonly
each define their own ``main()`` with no relation at all, so a call FROM
scripts/ counts for a symbol DEFINED in scripts/ only when the caller is the
same file or imports the defining module. For symbols defined in hub/, the
#1254 only_tests meaning is untouched: a call from scripts/ is not counted.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hub.services import call_sites


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
            "HOME": str(repo),
        },
    )


@pytest.fixture(autouse=True)
def _isolated_index_cache():
    call_sites.clear_index_cache()
    yield
    call_sites.clear_index_cache()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "hub").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "scripts").mkdir()
    (root / "hub" / "__init__.py").write_text("")
    _git(root, "init", "-q")
    return root


def _commit(repo: Path, message: str = "base") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _diff(repo: Path) -> str:
    _git(repo, "add", "-A")
    return _git(repo, "diff", "-U0", "--cached", "HEAD").stdout


# ---- AC-1: a scripts/ function called from the same file and an importing sibling ----


def test_a_function_called_from_scripts_has_callers(repo: Path):
    (repo / "scripts" / "a.py").write_text(
        "def helper(value):\n    return value\n\n\ndef run():\n    return helper(1)\n"
    )
    _commit(repo)

    (repo / "scripts" / "a.py").write_text(
        "def helper(value):\n"
        "    return value + 1\n"
        "\n"
        "\n"
        "def run():\n"
        "    return helper(1)\n"
    )
    (repo / "scripts" / "b.py").write_text(
        "import a\n\n\ndef main():\n    return a.helper(2)\n"
    )

    report = call_sites.analyse(str(repo), _diff(repo))

    assert report.analysed, report.reason
    helper = next(s for s in report.symbols if s.symbol == "helper")
    assert helper.state != call_sites.NO_CALLERS, helper.statement()
    by_file = {s.file for s in helper.sites}
    assert by_file == {"scripts/a.py", "scripts/b.py"}, helper.statement()


def test_a_namespace_package_import_of_a_script_is_recognized_too(repo: Path):
    """``scripts/`` ships no ``__init__.py``, but it is still a valid implicit
    namespace package: ``import scripts.a`` works exactly like ``import a``
    when run from the repo root. A caller spelled that way must not read as
    "did not import it" — that would be the same false no_callers this task
    exists to remove, just for a different import style."""
    (repo / "scripts" / "a.py").write_text(
        "def helper(value):\n    return value\n\n\ndef run():\n    return helper(1)\n"
    )
    _commit(repo)

    (repo / "scripts" / "a.py").write_text(
        "def helper(value):\n"
        "    return value + 1\n"
        "\n"
        "\n"
        "def run():\n"
        "    return helper(1)\n"
    )
    (repo / "scripts" / "b.py").write_text(
        "import scripts.a\n\n\ndef main():\n    return scripts.a.helper(2)\n"
    )

    report = call_sites.analyse(str(repo), _diff(repo))

    helper = next(s for s in report.symbols if s.symbol == "helper")
    assert helper.state != call_sites.NO_CALLERS, helper.statement()
    by_file = {s.file for s in helper.sites}
    assert "scripts/b.py" in by_file, (
        "import scripts.a (namespace package spelling) must count as an "
        f"import of scripts/a.py, not just bare 'import a': {helper.statement()}"
    )


def test_a_submodule_imported_via_from_package_import_submodule_is_recognized(
    repo: Path,
):
    """Real pattern in scripts/agent_eval/{runner,scenarios}.py: ``from
    agent_eval import contract as ct``. ``node.module`` alone is just
    ``agent_eval`` — it can never equal a changed symbol's full module path
    (``agent_eval.contract``); only the combination with the imported name
    does. Without it, a changed scripts/pkg/contract.py function called this
    way reads as only_tests or no_callers despite the real call."""
    (repo / "scripts" / "pkg").mkdir()
    (repo / "scripts" / "pkg" / "__init__.py").write_text("")
    (repo / "scripts" / "pkg" / "contract.py").write_text(
        "def build_run(value):\n    return value\n"
    )
    (repo / "scripts" / "pkg" / "runner.py").write_text(
        "from pkg import contract as ct\n\n\ndef main():\n    return ct.build_run(1)\n"
    )
    _commit(repo)

    (repo / "scripts" / "pkg" / "contract.py").write_text(
        "def build_run(value):\n    return value + 1\n"
    )

    report = call_sites.analyse(str(repo), _diff(repo))

    build_run = next(s for s in report.symbols if s.symbol == "build_run")
    assert build_run.state not in (call_sites.NO_CALLERS, call_sites.ONLY_TESTS), (
        build_run.statement()
    )
    by_file = {s.file for s in build_run.sites}
    assert "scripts/pkg/runner.py" in by_file, (
        "'from pkg import contract as ct' must count as an import of "
        f"scripts/pkg/contract.py: {build_run.statement()}"
    )


# ---- AC-3: a name match alone is not an import, and hub/'s only_tests is unchanged ----


def test_scripts_sites_need_an_import_and_hub_only_tests_is_kept(repo: Path):
    (repo / "scripts" / "a.py").write_text("def main():\n    return 'a'\n")
    # b.py's own main() is CALLED, by b.py itself — a real nominal-name
    # candidate for the "main" index entry, same as a.py's. Without the
    # import filter this reads as a call site of scripts/a.py:main too.
    (repo / "scripts" / "b.py").write_text(
        "def main():\n    return 'b'\n\n\ndef run():\n    return main()\n"
    )
    (repo / "hub" / "core.py").write_text("def guard(value):\n    return bool(value)\n")
    (repo / "scripts" / "c.py").write_text(
        "from hub.core import guard\n\n\ndef run():\n    return guard(1)\n"
    )
    (repo / "tests" / "test_core.py").write_text(
        "from hub.core import guard\n\n\ndef test_guard():\n    assert guard(1)\n"
    )
    _commit(repo)

    (repo / "scripts" / "a.py").write_text("def main():\n    return 'a changed'\n")
    (repo / "hub" / "core.py").write_text(
        "def guard(value):\n    return bool(value) and value != 0\n"
    )

    # Fixture sanity: the raw index must actually carry b.py's self-call
    # under the "main" name, or the filter below would pass vacuously.
    raw_index, _ = call_sites.build_call_index(str(repo))
    assert any(s.file == "scripts/b.py" for s in raw_index.get("main", [])), (
        "fixture is broken: scripts/b.py must produce a nominal 'main' site"
    )

    report = call_sites.analyse(str(repo), _diff(repo))

    main_report = next(
        s
        for s in report.symbols
        if s.symbol == "main" and s.defined_in == "scripts/a.py"
    )
    assert main_report.state == call_sites.NO_CALLERS, main_report.statement()
    assert all(s.file != "scripts/b.py" for s in main_report.sites), (
        "scripts/b.py:main calls itself via run() but never imports a — a "
        "name match alone must not read as a call site of scripts/a.py:main"
    )

    guard_report = next(s for s in report.symbols if s.symbol == "guard")
    assert guard_report.state == call_sites.ONLY_TESTS, guard_report.statement()
    assert [s.file for s in guard_report.sites] == ["tests/test_core.py"], (
        "a call from scripts/c.py must not count toward a hub/ symbol — "
        "#1254's only_tests meaning does not change"
    )


# ---- AC-4: the index cache still keys off scripts/ cleanliness ----


def test_a_dirty_script_misses_the_cache(repo: Path, monkeypatch):
    (repo / "scripts" / "a.py").write_text("def helper(value):\n    return value\n")
    (repo / "hub" / "core.py").write_text("def guard(value):\n    return bool(value)\n")
    _commit(repo)

    diff = "+++ b/hub/core.py\n@@ -1,1 +1,1 @@\n+x\n"
    call_sites.analyse(str(repo), diff)
    assert len(call_sites._index_cache) == 1, "a clean tree is cached"

    # The warm entry from the clean HEAD is left in place — not cleared —
    # because AC-4 is about a lookup that must NOT be served, not about an
    # empty cache: an uncommitted change under scripts/ must defeat even an
    # already-populated cache entry for this HEAD, exactly as it would for
    # an uncommitted change under hub/.
    (repo / "scripts" / "x.py").write_text("def new_thing():\n    return 1\n")

    real = call_sites.build_call_index
    calls: list[int] = []

    def counting(path, subdirs=call_sites.INDEX_SUBDIRS):
        calls.append(1)
        return real(path, subdirs)

    monkeypatch.setattr(call_sites, "build_call_index", counting)

    call_sites.analyse(str(repo), diff)
    call_sites.analyse(str(repo), diff)
    assert len(calls) == 2, (
        "an uncommitted scripts/ file must defeat the cache on every call, "
        "not just the first — the pre-existing warm entry must not be served"
    )
    assert len(call_sites._index_cache) == 1, (
        "the stale warm entry is untouched: a dirty-tree read never consults "
        "or overwrites the cache, it just builds directly"
    )
