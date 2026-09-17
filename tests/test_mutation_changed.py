"""Mutation run over changed functions (#1270): a weak test is seen before review.

Spike #1263 counted 37 confirmed findings in 30 days whose whole content was
"this test does not fail on broken behaviour" — and in 33 of them the reviewer
named the surviving mutation himself. These tests build a throwaway git
repository with one changed function and a test that never looks at its
result, and run the real script on it. The hub's own tree is never mutated
here: the point is the script's behaviour, not the hub's test strength.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "mutation_changed.py"

_BASE_CALC = """\
def add(a, b):
    return a


def untouched(x):
    return x * 2
"""

# `add` changes on the branch; `untouched` does not, and has no test at all —
# any mutant of it would survive, so seeing it named means the scope leaked.
_HEAD_CALC = """\
def add(a, b):
    return a + b


def untouched(x):
    return x * 2
"""

_WEAK_TEST = """\
from calc import add


def test_add_runs():
    add(1, 2)
"""

_RED_TEST = """\
from calc import add


def test_add_is_broken():
    assert add(1, 2) == 42
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _fixture_repo(tmp_path: Path, test_source: str) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "base")
    (repo / "calc.py").write_text(_BASE_CALC)
    (repo / "tests" / "test_calc.py").write_text(test_source)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "calc.py").write_text(_HEAD_CALC)
    _git(repo, "commit", "-q", "-am", "change add")
    return repo


def _run(repo: Path) -> tuple[subprocess.CompletedProcess[str], dict]:
    out = repo.parent / "mutations.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--base",
            "base",
            "--repo",
            str(repo),
            "--json-out",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=240,
    )
    return proc, json.loads(out.read_text())


def test_a_surviving_mutant_of_a_changed_function_is_named(tmp_path):
    """AC-1: the survivor is named by file, function and line; nothing else is mutated."""
    repo = _fixture_repo(tmp_path, _WEAK_TEST)

    proc, report = _run(repo)

    assert proc.returncode == 0, proc.stderr
    assert report["state"] == "ran"
    assert report["functions"] == ["calc.py::add"]
    assert report["survivors"], proc.stdout
    for survivor in report["survivors"]:
        assert survivor["file"] == "calc.py"
        assert survivor["function"] == "add"
        assert survivor["line"] == 2
    # Every mutant that was generated at all lies inside the changed function.
    assert (
        report["mutants"] == report["killed"] + report["survived"] + report["not_run"]
    )
    assert report["survived"] == len(report["survivors"])
    assert "calc.py:2 add" in proc.stdout
    assert "untouched" not in proc.stdout
    # The run leaves the tree exactly as it found it.
    assert (repo / "calc.py").read_text() == _HEAD_CALC


def test_a_red_baseline_is_not_reported_as_no_survivors(tmp_path):
    """AC-2: red tests before the series — the series did not run, and it says so."""
    repo = _fixture_repo(tmp_path, _RED_TEST)

    proc, report = _run(repo)

    assert proc.returncode == 0, proc.stderr
    assert report["state"] == "baseline_red"
    assert report["survivors"] is None
    assert report["mutants"] == 0
    assert "серия мутаций не выполнялась" in proc.stdout
    assert "выживших нет" not in proc.stdout
    assert (repo / "calc.py").read_text() == _HEAD_CALC


def _script_module():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import mutation_changed
    finally:
        sys.path.remove(str(REPO_ROOT / "scripts"))
    return mutation_changed


def test_tests_are_chosen_per_function_and_too_broad_is_named(tmp_path):
    """A function is judged by the tests that name it; a core module is not a full run."""
    mc = _script_module()
    fn = mc.Function("hub/calc.py", "Calc.add", 1, 3)
    texts = {
        tmp_path / "test_add.py": "from hub.calc import Calc\nCalc().add(1, 2)",
        tmp_path / "test_other.py": "import hub.calc\nhub.calc.sub(1, 2)",
        tmp_path / "test_far.py": "import json",
    }

    chosen, why = mc.tests_for(fn, texts)
    assert chosen == [tmp_path / "test_add.py"]
    assert why == ""

    nobody_names = mc.Function("hub/calc.py", "mul", 5, 6)
    chosen, _ = mc.tests_for(nobody_names, texts)
    assert sorted(chosen) == sorted(
        [tmp_path / "test_add.py", tmp_path / "test_other.py"]
    )

    chosen, why = mc.tests_for(nobody_names, texts, limit=1)
    assert chosen == []
    assert "шире предела 1" in why

    chosen, why = mc.tests_for(mc.Function("hub/none.py", "f", 1, 2), texts)
    assert chosen == []
    assert why


def test_type_annotations_are_not_mutated(tmp_path, monkeypatch):
    """`int | None` → `int - None` survives any test and says nothing about it."""
    mutation_changed = _script_module()

    source = (
        "from __future__ import annotations\n\n\n"
        "def pick(a: int | None, b: int) -> int | None:\n"
        "    return a | b\n"
    )
    (tmp_path / "mod.py").write_text(source)
    monkeypatch.chdir(tmp_path)

    mutations = mutation_changed.mutations_for("mod.py", tmp_path / "s.sqlite")

    assert mutations, "the body must still be mutated"
    assert {m.start_pos[0] for m in mutations} == {5}
