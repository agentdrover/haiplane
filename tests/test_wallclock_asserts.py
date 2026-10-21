"""Новый assert с порогом настенного времени < 1 с красит CI (#1989).

Тест, сравнивающий настоящие часы с порогом меньше секунды, падает на
загруженной машине без дефекта в коде (#1984: 0.317 при пороге 0.3). Проверка
читает исходники как AST, а не ищет имена; места, существовавшие до неё, живут
в ledger с владельцем. Здесь проверяется грамматика (что находится и что нет),
ведение ledger и то, что проверка подключена к CI и к `make check` без
возможности её молча отключить.

Исходники-фикстуры самостоятельны: каждый пишется во временный каталог и
прогоняется скриптом как отдельное дерево.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import wallclock_asserts as wc  # noqa: E402

FOUND_SOURCE = """\
import time
import time as t
from time import monotonic, perf_counter as pc


def test_one():
    t0 = time.monotonic()
    assert time.monotonic() - t0 < 0.3


def test_two(s):
    e = pc() - s
    assert e < 0.02 + 0.1


def test_three(items):
    t0 = monotonic()
    slowest = 0.0
    for _ in items:
        slowest = max(slowest, monotonic() - t0)
    assert slowest < 0.3


def test_four():
    elapsed = monotonic() - 1
    assert 0.5 > elapsed


def test_five():
    elapsed = monotonic() - 1
    assert 0 <= elapsed < 0.5


def test_six(t0):
    d = t.time() - t0
    d = d * 2
    assert d <= 0.9


def test_seven(monkeypatch, fake):
    m = time.monotonic
    t0 = m()
    monkeypatch.setattr(time, "monotonic", fake)
    assert m() - t0 < 0.3


def test_eight_patch_after_the_first_read(monkeypatch, fake, t0):
    time.monotonic()
    monkeypatch.setattr(time, "monotonic", fake)
    assert time.monotonic() - t0 < 0.7
"""

IGNORED_SOURCE = """\
import time


def test_exactly_one():
    elapsed = time.monotonic() - 1
    assert elapsed < 1.0


def test_large_threshold():
    elapsed = time.monotonic() - 1
    assert 0 <= elapsed < 3


def test_lower_bound_only():
    elapsed = time.monotonic() - 1
    assert elapsed >= 0.9


def test_parameter_threshold(budget):
    elapsed = time.monotonic() - 1
    assert elapsed < budget


def test_fake_clock(monkeypatch, fake, x):
    monkeypatch.setattr(time, "monotonic", fake)
    time.monotonic()
    elapsed = len(x)
    assert elapsed < 0.5


def test_fake_clock_is_the_source(monkeypatch, fake):
    monkeypatch.setattr(time, "monotonic", fake)
    elapsed = time.monotonic() - 1
    assert elapsed < 0.5


def test_not_a_clock(x):
    assert len(x) < 0.5


class TestAttr:
    def test_clock_on_an_object(self, clock):
        elapsed = clock.monotonic() - 1
        assert elapsed < 0.5
"""

TWO_CLASSES = """\
import time


class A:
    def test_x(self):
        t0 = time.monotonic()
        assert time.monotonic() - t0 < 0.3


class B:
    def test_x(self):
        t0 = time.monotonic()
        assert time.monotonic() - t0 < 0.3
"""

ONE_PLACE = """\
import time


def test_a():
    t0 = time.monotonic()
    assert time.monotonic() - t0 < 0.3
"""


def _run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], *extra: str
) -> tuple[int, str]:
    code = wc.main(
        [
            "--root",
            str(tmp_path),
            "--target",
            "src",
            "--ledger",
            str(tmp_path / "ledger.json"),
            *extra,
        ]
    )
    out = capsys.readouterr()
    return code, out.out + out.err


def _write(tmp_path: Path, source: str, name: str = "mod.py") -> None:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / name).write_text(source, encoding="utf-8")


def _write_ledger(tmp_path: Path, entries: list[dict]) -> None:
    (tmp_path / "ledger.json").write_text(
        json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8"
    )


def _entry(**over: object) -> dict:
    base = {
        "path": "src/mod.py",
        "function": "test_a",
        "assert": "time.monotonic() - t0 < 0.3",
        "count": 1,
        "removed_by": "unclaimed: старое место",
    }
    base.update(over)
    return base


def _line_of(source: str, needle: str) -> int:
    return next(i for i, row in enumerate(source.splitlines(), 1) if needle in row)


def test_subsecond_wallclock_asserts_are_found(tmp_path, capsys):
    """AC-1: семь форм найдены с файл:строка, rc != 0."""
    _write(tmp_path, FOUND_SOURCE)
    _write_ledger(tmp_path, [])
    code, out = _run(tmp_path, capsys)
    assert code != 0, out
    for needle in (
        "assert time.monotonic() - t0 < 0.3",
        "assert e < 0.02 + 0.1",
        "assert slowest < 0.3",
        "assert 0.5 > elapsed",
        "assert 0 <= elapsed < 0.5",
        "assert d <= 0.9",
        "assert m() - t0 < 0.3",
        "assert time.monotonic() - t0 < 0.7",
    ):
        line = _line_of(FOUND_SOURCE, needle)
        # Строка нарушения, а не предупреждения: у предупреждения другой префикс.
        assert f"\n  src/mod.py:{line} " in out, (needle, out)
    assert out.count("\n  src/mod.py:") == 8
    assert "ПРЕДУПРЕЖДЕНИЕ" not in out


def test_non_wallclock_or_large_thresholds_are_ignored(tmp_path, capsys):
    """AC-2: порог >= 1, нижняя граница, параметр, fake-часы - не нарушения."""
    _write(tmp_path, IGNORED_SOURCE)
    _write_ledger(tmp_path, [])
    code, out = _run(tmp_path, capsys)
    assert code == 0, out
    # Часы через атрибут объекта: не нарушение, а предупреждение с местом.
    assert "ПРЕДУПРЕЖДЕНИЕ" in out, out
    attr_line = max(
        i
        for i, row in enumerate(IGNORED_SOURCE.splitlines(), 1)
        if "assert elapsed < 0.5" in row
    )
    assert f"ПРЕДУПРЕЖДЕНИЕ src/mod.py:{attr_line} " in out, out
    assert out.count("ПРЕДУПРЕЖДЕНИЕ") == 1, out
    assert "НОВОЕ МЕСТО" not in out


def test_ledger_is_kept_per_assert(tmp_path, capsys):
    """AC-3: кратность, устаревшая запись, removed_by, дубль, методы, --update."""
    _write(tmp_path, ONE_PLACE)

    # Совпадение: rc 0, отчёт называет число записей.
    _write_ledger(tmp_path, [_entry()])
    code, out = _run(tmp_path, capsys)
    assert code == 0, out

    # Переформатирование не меняет отпечаток.
    _write(tmp_path, ONE_PLACE.replace("- t0 < 0.3", "-t0<0.3"))
    code, out = _run(tmp_path, capsys)
    assert code == 0, out

    # Кратность 2 при 1 в ledger.
    _write(
        tmp_path,
        ONE_PLACE.replace(
            "    assert time.monotonic() - t0 < 0.3\n",
            "    assert time.monotonic() - t0 < 0.3\n"
            "    assert time.monotonic() - t0 < 0.3\n",
        ),
    )
    code, out = _run(tmp_path, capsys)
    assert code != 0 and "кратность" in out, out

    # --update записывает кратность; запись с count 2 при одном месте устарела.
    code, out = _run(tmp_path, capsys, "--update")
    assert code == 0, out
    entries = json.loads((tmp_path / "ledger.json").read_text("utf-8"))["entries"]
    assert [(e["count"], e["removed_by"]) for e in entries] == [
        (2, "unclaimed: старое место")
    ]
    _write(tmp_path, ONE_PLACE)
    code, out = _run(tmp_path, capsys)
    assert code != 0 and "ПЕРЕЖИЛА" in out, out
    _write_ledger(tmp_path, [_entry()])
    code, out = _run(tmp_path, capsys)
    assert code == 0, out

    # Место удалено: запись устарела.
    _write(tmp_path, "import time\n")
    code, out = _run(tmp_path, capsys)
    assert code != 0 and "ПЕРЕЖИЛА" in out, out

    _write(tmp_path, ONE_PLACE)
    # removed_by: пусто, unclaimed без причины, мусор.
    for bad in ("", "unclaimed", "unclaimed:", "permanent: ", "TODO", "task:#"):
        _write_ledger(tmp_path, [_entry(removed_by=bad)])
        code, out = _run(tmp_path, capsys)
        assert code != 0 and "REMOVED_BY" in out, (bad, out)
    for good in ("task:#1990", "permanent: нужно по смыслу", "unclaimed: причина"):
        _write_ledger(tmp_path, [_entry(removed_by=good)])
        code, out = _run(tmp_path, capsys)
        assert code == 0, (good, out)

    # Дубль записи.
    _write_ledger(tmp_path, [_entry(), _entry()])
    code, out = _run(tmp_path, capsys)
    assert code != 0 and "ДУБЛЬ" in out, out

    # Одноимённые методы двух классов - разные записи.
    _write(tmp_path, TWO_CLASSES)
    _write_ledger(tmp_path, [])
    code, out = _run(tmp_path, capsys, "--update")
    assert code == 0, out
    entries = json.loads((tmp_path / "ledger.json").read_text("utf-8"))["entries"]
    assert sorted(e["function"] for e in entries) == ["A.test_x", "B.test_x"]

    # --update: новое место получает пустой removed_by, существующие сохраняются.
    _write(tmp_path, ONE_PLACE + "\n\n" + TWO_CLASSES.split("\n\n\n", 1)[1])
    _write_ledger(
        tmp_path,
        [
            {**e, "removed_by": "task:#1990" if e["function"] == "A.test_x" else ""}
            for e in entries
        ],
    )
    code, out = _run(tmp_path, capsys, "--update")
    assert code == 0, out
    after = {
        e["function"]: e
        for e in json.loads((tmp_path / "ledger.json").read_text("utf-8"))["entries"]
    }
    assert after["A.test_x"]["removed_by"] == "task:#1990"
    assert after["test_a"]["removed_by"] == ""
    code, out = _run(tmp_path, capsys)
    assert code != 0, "пустой removed_by нового места обязан краснеть"

    # Дерево репозитория: ledger сходится с кодом.
    assert wc.main([]) == 0
    capsys.readouterr()


def test_ci_and_make_run_the_wallclock_check():
    """AC-4: шаг CI без обходов, цель Makefile в check, сигнатура в SIGNATURES."""
    import yaml

    ci = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text("utf-8"))
    steps = [
        s
        for s in ci["jobs"]["static"]["steps"]
        if "scripts/wallclock_asserts.py" in str(s.get("run", ""))
    ]
    assert len(steps) == 1, steps
    step = steps[0]
    assert "continue-on-error" not in step
    assert "if" not in step
    assert "|| true" not in step["run"]
    assert "||" not in step["run"]

    makefile = (REPO_ROOT / "Makefile").read_text("utf-8")
    check_deps = (
        next(row for row in makefile.splitlines() if row.startswith("check:"))
        .split(":", 1)[1]
        .split()
    )
    holders = []
    for target in check_deps:
        body = []
        lines = makefile.splitlines()
        start = lines.index(f"{target}:")
        for row in lines[start + 1 :]:
            if not row.startswith("\t"):
                break
            body.append(row)
        if "scripts/wallclock_asserts.py" in "\n".join(body):
            holders.append(target)
    assert holders, check_deps

    from tests import test_local_checks_match_ci as parity

    assert "wallclock_asserts.py" in parity.SIGNATURES
    assert (
        parity._uncovered_steps(
            (REPO_ROOT / ".github/workflows/ci.yml").read_text("utf-8"), makefile
        )
        == []
    )
