"""Ledger tests и база сравнения (#1932): продолжение tests/test_file_size_budget.py.

Файл вынесен отдельно: тесты AC-1..AC-3 лежат в основном файле, а проверка
сама требует, чтобы файл tests вне ledger не был длиннее порога ревью.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_file_size_budget import (
    LEDGER_REL,
    SCRIPT,
    _git,
    _ledger_waivers,
    _make_tree,
    _old_tests_waiver,
    _run,
    _set_waivers,
    _tests_repo,
    _waiver,
    _write_lines,
)


def test_update_never_raises_a_tests_entry(tmp_path):
    """AC-3: --update опускает measured записи tests, но не поднимает."""
    _tests_repo(tmp_path, waivers=[_old_tests_waiver()])
    _write_lines(tmp_path, "tests/old.py", 930)  # внутри потолка 945
    assert _run(tmp_path, "--update").returncode == 0
    (entry,) = _ledger_waivers(tmp_path)
    assert (entry["measured"], entry["max"]) == (900, 945)

    _write_lines(tmp_path, "tests/old.py", 1000)  # выше потолка
    assert _run(tmp_path, "--update").returncode == 0
    (entry,) = _ledger_waivers(tmp_path)
    assert (entry["measured"], entry["max"]) == (900, 945)
    assert _run(tmp_path).returncode != 0  # запись не подогнана - файл красный

    _write_lines(tmp_path, "tests/old.py", 850)
    assert _run(tmp_path, "--update").returncode == 0
    (entry,) = _ledger_waivers(tmp_path)
    assert (entry["measured"], entry["max"]) == (850, 893)

    # hub: запись по-прежнему перемеривается вверх (поведение #1669).
    (tmp_path / "tests" / "old.py").unlink()
    _make_tree(tmp_path, tests=800, waivers=[_waiver("hub/w.py", 1600)])
    _write_lines(tmp_path, "hub/w.py", 1700)
    assert _run(tmp_path, "--update").returncode == 0
    (entry,) = _ledger_waivers(tmp_path)
    assert entry["measured"] == 1700


def _bootstrap_repo(root: Path) -> None:
    """База без правила (tests=3000) с длинным файлом; рабочее дерево вводит 800."""
    _make_tree(root, tests=3000)
    _write_lines(root, "tests/long.py", 1000)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    (root / "pyproject.toml").write_text(
        "[tool.haiplane.file_size]\nhub = 1500\nscripts = 1500\ntests = 800\n"
        "near_pct = 90\n",
        encoding="utf-8",
    )


def test_a_base_without_the_rule_lets_long_files_of_the_base_be_introduced(tmp_path):
    """База с порогом выше нынешнего правила не знала: файлы, что уже были на ней
    длиннее порога, вносятся по замеру (первый релиз, PR, вводящий порог)."""
    _bootstrap_repo(tmp_path)
    assert _run(tmp_path, "--update", "--base", "HEAD").returncode == 0
    assert _run(tmp_path, "--base", "HEAD").returncode == 0
    # Файл вырос на develop по старым правилам (925 -> 1040 на релизе): --update
    # перемеряет запись вверх, база её потолком не связана.
    _write_lines(tmp_path, "tests/long.py", 1377)
    assert _run(tmp_path, "--update", "--base", "HEAD").returncode == 0
    (entry,) = _ledger_waivers(tmp_path)
    assert entry["measured"] == 1377
    assert _run(tmp_path, "--base", "HEAD").returncode == 0


def test_old_base_new_file_or_rename_is_red(tmp_path):
    """Исключение не узаконивает новый длинный файл и переименование."""
    _bootstrap_repo(tmp_path)
    assert _run(tmp_path, "--update", "--base", "HEAD").returncode == 0
    _write_lines(tmp_path, "tests/test_new_waiver.py", 1000)
    assert _run(tmp_path, "--update", "--base", "HEAD").returncode == 0
    proc = _run(tmp_path, "--base", "HEAD")
    assert proc.returncode != 0
    assert "tests/test_new_waiver.py" in proc.stdout

    (tmp_path / "tests" / "test_new_waiver.py").unlink()
    _git(tmp_path, "mv", "tests/long.py", "tests/long_renamed.py")
    assert _run(tmp_path, "--update", "--base", "HEAD").returncode == 0
    proc = _run(tmp_path, "--base", "HEAD")
    assert proc.returncode != 0
    assert "tests/long_renamed.py" in proc.stdout


def test_old_base_file_that_was_short_is_red(tmp_path):
    """Файл был не длиннее порога на базе и перерос его - не бутстрап."""
    _make_tree(tmp_path, tests=3000)
    _write_lines(tmp_path, "tests/grows.py", 700)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.haiplane.file_size]\nhub = 1500\nscripts = 1500\ntests = 800\n"
        "near_pct = 90\n",
        encoding="utf-8",
    )
    _write_lines(tmp_path, "tests/grows.py", 1000)
    assert _run(tmp_path, "--update", "--base", "HEAD").returncode == 0
    proc = _run(tmp_path, "--base", "HEAD")
    assert proc.returncode != 0
    assert "tests/grows.py" in proc.stdout


def test_update_clamps_to_the_base_ledger_not_to_the_working_one(tmp_path):
    """Правило действует на базе: --update зажимает по ledger базы."""
    _tests_repo(tmp_path, waivers=[_old_tests_waiver()])
    _write_lines(tmp_path, "tests/old.py", 900)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "old file")
    # Рабочий ledger подогнан вверх руками; --update по базе возвращает потолок.
    _write_lines(tmp_path, "tests/old.py", 930)
    _set_waivers(tmp_path, [_old_tests_waiver(measured=930, max_=977)])
    assert _run(tmp_path, "--update", "--base", "HEAD").returncode == 0
    (entry,) = _ledger_waivers(tmp_path)
    assert (entry["measured"], entry["max"]) == (900, 945)


def test_a_malformed_base_ledger_entry_is_unreadable_not_a_traceback(tmp_path):
    _tests_repo(tmp_path, waivers=[{"path": "tests/old.py", "removed_by": "unclaimed"}])
    _write_lines(tmp_path, "tests/old.py", 900)
    _set_waivers(tmp_path, [_old_tests_waiver()])
    proc = _run(tmp_path, "--base", "HEAD")
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr


def test_unreadable_base_is_not_green(tmp_path):
    """Нет базы - не данные: rc 2 с причиной, а не молчаливое «чисто»."""
    _make_tree(tmp_path, tests=800)  # не git-репозиторий
    proc = _run(tmp_path, "--base", "origin/nowhere")
    assert proc.returncode == 2
    assert "не удалось" in (proc.stdout + proc.stderr)

    repo = tmp_path / "r"
    repo.mkdir()
    _tests_repo(repo)
    proc = _run(repo, "--base", "origin/nowhere")  # репозиторий есть, ref нет
    assert proc.returncode == 2

    _git(repo, "rm", "-q", "--cached", str(LEDGER_REL))
    _git(repo, "commit", "-q", "-m", "no ledger")
    assert _run(repo, "--base", "HEAD~1").returncode == 0  # у базы ledger был
    proc = _run(repo, "--base", "HEAD")  # у базы ledger нет - сравнивать не с чем
    assert proc.returncode == 2
    assert "ledger" in (proc.stdout + proc.stderr)


def test_ci_base_is_taken_from_the_ci_environment(tmp_path):
    """База из окружения CI: PR - ветка PR, push - before, иначе origin/develop."""
    from file_size_budget import base_from_environment

    assert base_from_environment({}) == ("origin/develop", False)
    assert base_from_environment({"GITHUB_EVENT_NAME": "workflow_dispatch"}) == (
        "origin/develop",
        False,
    )
    assert base_from_environment(
        {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_BASE_REF": "main"}
    ) == ("origin/main", False)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"before": "a" * 40}), encoding="utf-8")
    env = {"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(event)}
    assert base_from_environment(env) == ("a" * 40, True)
    event.write_text(json.dumps({"before": "0" * 40}), encoding="utf-8")
    assert base_from_environment(env) == ("origin/develop", False)


def test_push_without_readable_before_is_rc2(tmp_path):
    """push без читаемого before - не fallback на origin/develop (== HEAD), а rc 2."""
    from file_size_budget import CompareError, base_from_environment

    event = tmp_path / "event.json"
    for text in ("not json", json.dumps({"after": "x"}), json.dumps({"before": ""})):
        event.write_text(text, encoding="utf-8")
        env = {"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(event)}
        with pytest.raises(CompareError):
            base_from_environment(env)
    with pytest.raises(CompareError):
        base_from_environment({"GITHUB_EVENT_NAME": "push"})
    with pytest.raises(CompareError):
        base_from_environment(
            {"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(tmp_path / "no")}
        )

    repo = tmp_path / "r"
    repo.mkdir()
    _tests_repo(repo)
    event.write_text("not json", encoding="utf-8")
    env = {
        **os.environ,
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_EVENT_PATH": str(event),
    }
    for extra in ((), ("--update",)):  # без --base и --no-base: база из окружения
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(repo), *extra],
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 2, extra
        assert "не удалось" in proc.stderr


def test_update_with_unresolvable_base_is_rc2_and_writes_nothing(tmp_path):
    _tests_repo(tmp_path, waivers=[_old_tests_waiver()])
    _write_lines(tmp_path, "tests/old.py", 900)
    _write_lines(tmp_path, "tests/new.py", 801)
    before = (tmp_path / LEDGER_REL).read_bytes()
    proc = _run(tmp_path, "--update", "--base", "origin/nowhere")
    assert proc.returncode == 2
    assert (tmp_path / LEDGER_REL).read_bytes() == before


def test_base_pyproject_without_tests_is_rc2(tmp_path):
    """Нет порога tests в pyproject базы - ограничение не выключается молча."""
    _make_tree(tmp_path, tests=800)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.haiplane.file_size]\nhub = 1500\nscripts = 1500\nnear_pct = 90\n",
        encoding="utf-8",
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base without tests")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.haiplane.file_size]\nhub = 1500\nscripts = 1500\ntests = 800\n"
        "near_pct = 90\n",
        encoding="utf-8",
    )
    for extra in ((), ("--update",)):
        proc = _run(tmp_path, *extra, "--base", "HEAD")
        assert proc.returncode == 2, extra
        assert "tests" in proc.stderr


def test_base_ledger_entry_without_measured_is_rc2(tmp_path):
    """Битая запись ledger базы: rc 2 и у проверки, и у --update, а не KeyError."""
    bad = {"path": "hub/h.py", "max": 1600, "removed_by": "unclaimed"}
    _tests_repo(tmp_path, waivers=[bad])
    ledger = (tmp_path / LEDGER_REL).read_bytes()
    for extra in ((), ("--update",)):
        proc = _run(tmp_path, *extra, "--base", "HEAD")
        assert proc.returncode == 2, extra
        assert "Traceback" not in proc.stderr
        assert (tmp_path / LEDGER_REL).read_bytes() == ledger


def test_lazy_base_read_failure_during_update_is_rc2_and_writes_nothing(
    tmp_path, monkeypatch, capsys
):
    """Чтение базы, которое откладывается до работы --update, не оставляет следов."""
    import file_size_budget as fsb

    _make_tree(tmp_path, tests=800)
    _write_lines(tmp_path, "tests/long.py", 1000)
    before = (tmp_path / LEDGER_REL).read_bytes()

    def boom(path: str) -> int | None:
        raise fsb.CompareError("git ls-tree: object is gone")

    view = fsb.BaseView(
        ledger={},
        lines=boom,
        thresholds={"hub": 1500, "scripts": 1500, "tests": 3000},
    )
    monkeypatch.setattr(fsb, "load_base", lambda *args: view)
    monkeypatch.setattr(
        sys, "argv", ["file_size_budget.py", "--root", str(tmp_path), "--update"]
    )
    assert fsb.main() == 2
    assert "не удалось" in capsys.readouterr().err
    assert (tmp_path / LEDGER_REL).read_bytes() == before


@pytest.mark.parametrize(
    "toml",
    [
        "[tool.haiplane]\nfile_size = []\n",
        "[tool.haiplane.file_size]\nhub = 1500\nscripts = 1500\ntests = inf\n",
        "[tool.haiplane.file_size]\nhub = 1500\nscripts = 1500\ntests = 'x'\n",
    ],
)
def test_malformed_base_pyproject_is_rc2_not_a_traceback(tmp_path, toml):
    _make_tree(tmp_path, tests=800)
    (tmp_path / "pyproject.toml").write_text(toml, encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "bad base")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.haiplane.file_size]\nhub = 1500\nscripts = 1500\ntests = 800\n"
        "near_pct = 90\n",
        encoding="utf-8",
    )
    ledger = (tmp_path / LEDGER_REL).read_bytes()
    for extra in ((), ("--update",)):
        proc = _run(tmp_path, *extra, "--base", "HEAD")
        assert proc.returncode == 2, extra
        assert "Traceback" not in proc.stderr
    assert (tmp_path / LEDGER_REL).read_bytes() == ledger


def test_base_ledger_with_a_non_finite_number_is_rc2(tmp_path):
    _make_tree(tmp_path, tests=800)
    (tmp_path / LEDGER_REL).write_text(
        '{"headroom_pct": 5, "waivers": [{"path": "hub/h.py", "measured": Infinity,'
        ' "max": 1, "removed_by": "unclaimed"}]}',
        encoding="utf-8",
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    for extra in ((), ("--update",)):
        proc = _run(tmp_path, *extra, "--base", "HEAD")
        assert proc.returncode == 2, extra
        assert "Traceback" not in proc.stderr
