"""Мета-тест покрытия свипов поллера сценариями многократного прохода (#1859).

Новый свип без записи в ``SWEEP_COVERAGE`` красит CI: либо сценарий(и) со
ссылками на существующие тесты, либо исключение с ``removed_by`` в формате
ledger бюджета сложности. Перечень свипов берётся из настоящего ``SWEEPS``.
"""

from __future__ import annotations

from pathlib import Path

from hub.poller import SWEEPS
from tests.poller_sweep_support import (
    SWEEP_COVERAGE,
    Coverage,
    check_registry,
    scenario_exists,
    validate_removed_by,
)

NAMES = [sweep.name for sweep in SWEEPS]
OK = "unclaimed — причина"


def test_every_sweep_has_scenarios_or_a_reasoned_exception():
    """AC-1: SWEEPS и реестр сходятся в обе стороны, ссылки живые."""
    problems = check_registry(NAMES, SWEEP_COVERAGE)
    assert not problems, "\n".join(problems)


def test_the_checker_names_every_kind_of_defect(tmp_path: Path):
    """Проверка краснеет на каждом из трёх дефектов реестра и не краснеет зря."""
    (tmp_path / "t.py").write_text(
        "async def test_real():\n    pass\n\n\ndef helper():\n    pass\n"
    )
    good = {n: Coverage(removed_by=OK) for n in NAMES}
    good[NAMES[0]] = Coverage(scenarios=("t.py::test_real",))
    assert check_registry(NAMES, good, tmp_path) == []

    missing = {n: c for n, c in good.items() if n != NAMES[1]}
    assert any("не внесён" in p for p in check_registry(NAMES, missing, tmp_path))

    dangling = dict(good, **{NAMES[0]: Coverage(scenarios=("t.py::test_gone",))})
    assert any("сценария" in p for p in check_registry(NAMES, dangling, tmp_path))
    helper = dict(good, **{NAMES[0]: Coverage(scenarios=("t.py::helper",))})
    assert any("сценария" in p for p in check_registry(NAMES, helper, tmp_path))
    real_helper = dict(
        good,
        **{NAMES[0]: Coverage(scenarios=("tests/poller_sweep_support.py::utc",))},
    )
    assert any("сценария" in p for p in check_registry(NAMES, real_helper))
    no_file = dict(good, **{NAMES[0]: Coverage(scenarios=("gone.py::test_real",))})
    assert any("сценария" in p for p in check_registry(NAMES, no_file, tmp_path))

    removed = dict(good, deleted_sweep=Coverage(removed_by=OK))
    assert any("удалите строку" in p for p in check_registry(NAMES, removed, tmp_path))

    both = dict(good, **{NAMES[1]: Coverage(("t.py::test_real",), OK)})
    assert any("вместе" in p for p in check_registry(NAMES, both, tmp_path))


def test_removed_by_follows_the_complexity_budget_format():
    for ok in ("task:#1859", "task:#1859 — снимет", "permanent — причина", OK):
        assert validate_removed_by(ok) is None, ok
    for bad in ("", "TODO", "permanent —", "unclaimed", "task:#x", "soon"):
        assert validate_removed_by(bad), bad
    broken = dict(SWEEP_COVERAGE, ci_trust=Coverage(removed_by="TODO"))
    assert any("removed_by" in p for p in check_registry(NAMES, broken))


def test_scenario_references_are_checked_against_the_source_not_the_name():
    assert scenario_exists(
        "tests/test_steward_runs_scenarios.py::test_steward_runs_counts_per_channel"
    )
    assert not scenario_exists("tests/test_steward_runs_scenarios.py::nope")
    assert not scenario_exists("tests/test_steward_runs_scenarios.py")
