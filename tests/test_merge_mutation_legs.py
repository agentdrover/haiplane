"""#1921: the reports of the mutation legs become one report, or an explicit error."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "merge_mutation_legs", ROOT / "scripts" / "merge_mutation_legs.py"
)
assert _spec is not None and _spec.loader is not None
merger = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merger)

HEAD = "a" * 40
BASE = "b" * 40


def _leg(i: int, total: int = 3, **over) -> dict:
    leg = {
        "state": "ran",
        "base": "origin/develop",
        "head": HEAD,
        "merge_base": BASE,
        "base_tip": BASE,
        "shard": f"{i}/{total}",
        "reason": "",
        "functions": ["a.py::f", "a.py::g"],
        "untested": ["a.py::h: нет тестов"],
        "mutants": 4,
        "killed": 3,
        "survived": 1,
        "not_run": 0,
        "survivors": [
            {"file": "a.py", "function": "f", "line": 10 + i, "operator": "core/X"}
        ],
        "seconds": 10.0 * i,
    }
    leg.update(over)
    return leg


def _merge(*legs: dict) -> dict:
    return merger.merge([(f"leg{n}", leg) for n, leg in enumerate(legs, 1)])


def test_ran_legs_add_up_and_keep_one_report_shape() -> None:
    merged = _merge(_leg(1), _leg(2), _leg(3))
    assert merged["state"] == "ran"
    assert (merged["mutants"], merged["killed"], merged["survived"]) == (12, 9, 3)
    assert merged["not_run"] == 0 and merged["legs"] == 3
    assert merged["functions"] == ["a.py::f", "a.py::g"], "the union, no duplicates"
    assert merged["untested"] == ["a.py::h: нет тестов"]
    assert [s["line"] for s in merged["survivors"]] == [11, 12, 13]
    assert merged["seconds"] == 30.0, "legs run side by side: the longest one"
    assert (merged["head"], merged["merge_base"], merged["base_tip"]) == (
        HEAD,
        BASE,
        BASE,
    )
    # the keys of a whole series' report are all there
    for key in (
        "state",
        "base",
        "reason",
        "functions",
        "untested",
        "mutants",
        "killed",
        "survived",
        "not_run",
        "survivors",
        "seconds",
    ):
        assert key in merged, key


@pytest.mark.parametrize(
    "legs",
    [
        pytest.param([], id="no_leg_reports_at_all"),
        pytest.param([_leg(1), _leg(3)], id="a_leg_is_missing"),
        pytest.param([_leg(1), _leg(1), _leg(2), _leg(3)], id="a_leg_twice"),
        pytest.param([_leg(1), _leg(2, total=4), _leg(3)], id="different_totals"),
        pytest.param([_leg(1), _leg(2, shard=""), _leg(3)], id="no_shard_mark"),
        pytest.param([_leg(1), _leg(2, head="c" * 40), _leg(3)], id="other_head"),
        pytest.param([_leg(1), _leg(2, base_tip="d" * 40), _leg(3)], id="other_base"),
        pytest.param([_leg(1), _leg(2, state="no_tests"), _leg(3)], id="legs_disagree"),
    ],
)
def test_any_doubt_is_an_error_with_a_reason(legs: list[dict]) -> None:
    merged = _merge(*legs)
    assert merged["state"] == "error", merged
    assert merged["reason"], "an error without a reason is a silent failure"
    assert merged.get("survivors") is None


def test_a_red_baseline_on_one_leg_is_not_ran() -> None:
    red = _leg(2, state="baseline_red", reason="rc=1", mutants=0, killed=0, survived=0)
    merged = _merge(_leg(1), red, _leg(3))
    assert merged["state"] == "baseline_red"
    assert "[leg 2/3] rc=1" in merged["reason"]
    assert merged["survivors"] is None and merged["mutants"] == 0


def test_an_error_leg_wins_over_a_red_baseline() -> None:
    merged = _merge(_leg(1, state="baseline_red"), _leg(2, state="error"), _leg(3))
    assert merged["state"] == "error"


@pytest.mark.parametrize("state", ["no_changed_functions", "no_tests"])
def test_a_common_state_without_a_series_is_kept(state: str) -> None:
    legs = [
        _leg(i, state=state, mutants=0, killed=0, survived=0, survivors=None)
        for i in (1, 2, 3)
    ]
    merged = _merge(*legs)
    assert merged["state"] == state and merged["survivors"] is None
    assert merged["mutants"] == 0


def test_the_cli_reads_the_directories_of_downloaded_artifacts(tmp_path: Path) -> None:
    for i in (1, 2, 3):
        folder = tmp_path / "legs" / f"hub-ci-mutations-leg-{i}"
        folder.mkdir(parents=True)
        (folder / "mutation-report.json").write_text(json.dumps(_leg(i)))
    out = tmp_path / "mutation-report.json"
    assert merger.main(["--legs-dir", str(tmp_path / "legs"), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["state"] == "ran"

    (tmp_path / "legs" / "hub-ci-mutations-leg-2" / "mutation-report.json").unlink()
    assert merger.main(["--legs-dir", str(tmp_path / "legs"), "--out", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["state"] == "error" and "missing" in report["reason"]

    (tmp_path / "legs" / "hub-ci-mutations-leg-1" / "mutation-report.json").write_text(
        "{"
    )
    assert merger.main(["--legs-dir", str(tmp_path / "legs"), "--out", str(out)]) == 0
    assert "unreadable" in json.loads(out.read_text())["reason"]

    empty = tmp_path / "nothing"
    empty.mkdir()
    assert merger.main(["--legs-dir", str(empty), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["state"] == "error"
