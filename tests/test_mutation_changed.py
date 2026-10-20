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


def test_the_report_names_what_it_was_measured_against(tmp_path):
    """#1919: head, merge-base and the tip of the base are in the JSON.

    A PR run adopts this file only when merge_base and base_tip equal the base of
    its own merge commit, so a report without them can never be adopted.
    """
    repo = _fixture_repo(tmp_path, _WEAK_TEST)

    proc, report = _run(repo)

    assert proc.returncode == 0, proc.stderr
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    base = subprocess.check_output(
        ["git", "rev-parse", "base"], cwd=repo, text=True
    ).strip()
    assert report["head"] == head
    assert report["merge_base"] == base
    assert report["base_tip"] == base


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
    """`int | None` → `int - None` and `*,` → `/,` survive any test and say nothing."""
    mutation_changed = _script_module()

    source = (
        "from __future__ import annotations\n\n\n"
        "def pick(a: int | None, *, b: int) -> int | None:\n"
        "    return a | b * 2\n"
    )
    (tmp_path / "mod.py").write_text(source)
    monkeypatch.chdir(tmp_path)

    mutations = mutation_changed.mutations_for("mod.py", tmp_path / "s.sqlite")

    assert mutations, "the body must still be mutated"
    assert {m.start_pos[0] for m in mutations} == {5}
    # The keyword-only `*` is not multiplication; the one in the body is.
    stars = [
        m
        for m in mutations
        if m.operator_name.startswith("core/ReplaceBinaryOperator_Mul_")
    ]
    assert stars and all(m.start_pos[0] == 5 for m in stars)


# ---- #1921: the series is split into legs by the mutant itself ----------------

_MANY_BASE = """\
def score(a, b, c):
    return 0
"""

_MANY_HEAD = """\
def score(a, b, c):
    if a > b:
        return a + b - c
    return a * c + b
"""

_MANY_TEST = """\
from many import score


def test_score_one_branch():
    assert score(5, 1, 2) == 4


def test_score_other_branch_weakly():
    assert score(1, 5, 2) is not None
"""


def _import_script():
    import importlib.util

    spec = importlib.util.spec_from_file_location("mutation_changed_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


class _Spec:
    """The five attributes of a cosmic-ray MutationSpec the key is built from."""

    def __init__(self, start, end, operator="core/ReplaceBinaryOperator_Add_Sub", n=0):
        self.start_pos, self.end_pos = start, end
        self.operator_name, self.occurrence = operator, n


def test_mutants_split_by_sorted_index_is_a_partition():
    """AC-1: the legs are a partition, and the owner does not depend on input order."""
    import random

    script = _import_script()
    function = script.Function("m.py", "f", 1, 99)
    other = script.Function("m.py", "g", 100, 200)
    specs = [
        _Spec((line, col), (line, col + 1), operator=op, n=n)
        for line in range(2, 9)
        for col in (0, 4)
        for op, n in (("core/A", 0), ("core/B", 1))
    ]

    def build(order_seed: int | None):
        items = list(specs)
        if order_seed is not None:
            random.Random(order_seed).shuffle(items)
        half = len(items) // 2
        return [
            script._Target(function, items[:half], []),
            script._Target(other, items[half:], []),
        ]

    def owners(total: int, order_seed: int | None) -> dict:
        owned: dict = {}
        for index in range(1, total + 1):
            for target in script.shard_targets(build(order_seed), index, total):
                for mutation in target.mutations:
                    key = script.mutation_key(target.function.path, mutation)
                    assert key not in owned or total == 1
                    owned.setdefault(key, set()).add(index)
        return owned

    for total in (1, 2, 3, 4, 7):
        baseline = owners(total, None)
        # a partition: every mutant has exactly one owner leg, none is lost
        assert all(len(legs) == 1 for legs in baseline.values())
        assert len(baseline) == len(specs)
        # the same owner whatever order the targets list their mutations in
        for seed in (1, 2, 3):
            assert owners(total, seed) == baseline, (total, seed)
        sizes = [
            sum(len(t.mutations) for t in script.shard_targets(build(None), i, total))
            for i in range(1, total + 1)
        ]
        assert sum(sizes) == len(specs) and max(sizes) - min(sizes) <= 1, sizes
    # leg 1 of 1 is the whole series, untouched
    whole = build(None)
    assert script.shard_targets(whole, 1, 1) is whole


def test_parse_shard_refuses_what_it_does_not_understand():
    script = _import_script()
    assert script.parse_shard("2/3") == (2, 3)
    for bad in ("", "3", "0/3", "4/3", "1/0", "a/b", "1/2/3"):
        try:
            script.parse_shard(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} must be refused")


def _many_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "base")
    (repo / "many.py").write_text(_MANY_BASE)
    (repo / "tests" / "test_many.py").write_text(_MANY_TEST)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "many.py").write_text(_MANY_HEAD)
    _git(repo, "commit", "-q", "-am", "change score")
    return repo


def _run_leg(repo: Path, shard: str, name: str) -> dict:
    out = repo.parent / name
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--base",
            "base",
            "--repo",
            str(repo),
            "--shard",
            shard,
            "--baseline-timeout",
            "120",
            "--budget-seconds",
            "300",
            "--json-out",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(out.read_text())


def test_merged_legs_equal_the_sequential_series(tmp_path):
    """AC-2: three legs, merged, say what one sequential run says (budget not hit)."""
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location(
        "merge_mutation_legs", SCRIPT.parent / "merge_mutation_legs.py"
    )
    merger = module_from_spec(spec)
    spec.loader.exec_module(merger)

    repo = _many_repo(tmp_path)
    whole = _run_leg(repo, "1/1", "whole.json")
    assert whole["state"] == "ran" and whole["mutants"] >= 6, whole
    assert whole["survived"] >= 1 and whole["killed"] >= 1, whole

    legs = [_run_leg(repo, f"{i}/3", f"leg{i}.json") for i in (1, 2, 3)]
    assert [leg["shard"] for leg in legs] == ["1/3", "2/3", "3/3"]
    assert sum(leg["mutants"] for leg in legs) == whole["mutants"]
    merged = merger.merge([(f"leg{i}", leg) for i, leg in enumerate(legs, 1)])

    assert merged["state"] == "ran", merged
    # not_run may differ (the per-mutant timeout depends on the runner); what was
    # judged must not
    for key in ("mutants", "killed", "survived"):
        assert merged[key] == whole[key], (key, merged, whole)
    assert merged["functions"] == whole["functions"]
    assert merged["untested"] == whole["untested"]

    def named(report):
        return sorted(
            (s["file"], s["line"], s["operator"]) for s in report["survivors"]
        )

    assert named(merged) == named(whole)
    # the legs share no survivor
    seen = [tuple(x) for leg in legs for x in named(leg)]
    assert len(seen) == len(set(seen))
