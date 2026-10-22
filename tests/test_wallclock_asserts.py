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


def test_nine_bare_patch_call_does_nothing():
    patch("time.monotonic", return_value=0)
    t0 = time.monotonic()
    assert time.monotonic() - t0 < 0.61


def test_eleven_reference_saved_before_the_patch(monkeypatch, fake, t0):
    m = time.monotonic
    monkeypatch.setattr(time, "monotonic", fake)
    assert m() - t0 < 0.62


def test_ten_every_arithmetic_operator_is_evaluated(t0):
    elapsed = time.monotonic() - t0
    assert elapsed < 2 ** -1 - 0.01
    assert elapsed < 1.5 % 1 + 0.01
    assert elapsed < 7 // 10 + 0.02
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


def test_patch_as_context_manager():
    with patch("time.monotonic"):
        elapsed = time.monotonic() - 1
    assert elapsed < 0.5


@patch("time.perf_counter")
def test_patch_as_decorator(fake):
    import time as tm

    elapsed = tm.perf_counter() - 1
    assert elapsed < 0.5


def test_patch_started():
    p = patch("time.monotonic")
    p.start()
    elapsed = time.monotonic() - 1
    assert elapsed < 0.5


def test_reference_saved_after_the_patch(monkeypatch, fake):
    monkeypatch.setattr(time, "monotonic", fake)
    m = time.monotonic
    assert m() < 0.5


def test_same_line_patch_then_read(monkeypatch, fake):
    monkeypatch.setattr(time, "monotonic", fake); elapsed = time.monotonic() - 1
    assert elapsed < 0.5


def test_same_line_patch_then_reference(monkeypatch, fake):
    monkeypatch.setattr(time, "monotonic", fake); m = time.monotonic
    assert m() < 0.5


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
    for needle in (
        "assert time.monotonic() - t0 < 0.61",
        "assert m() - t0 < 0.62",
        "assert elapsed < 2 ** -1 - 0.01",
        "assert elapsed < 1.5 % 1 + 0.01",
        "assert elapsed < 7 // 10 + 0.02",
    ):
        line = _line_of(FOUND_SOURCE, needle)
        assert f"\n  src/mod.py:{line} " in out, (needle, out)
    assert out.count("\n  src/mod.py:") == 13
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


def test_a_file_without_import_time_is_analysed(tmp_path, capsys):
    """Часы чужого объекта в файле без ``import time`` - предупреждение."""
    source = "def test_x(clock):\n    assert clock.monotonic() < 0.5\n"
    _write(tmp_path, source)
    _write_ledger(tmp_path, [])
    code, out = _run(tmp_path, capsys)
    assert code == 0, out
    assert "ПРЕДУПРЕЖДЕНИЕ src/mod.py:2 " in out, out


def test_fingerprint_does_not_depend_on_fstring_quoting():
    """Отпечаток f-строки одинаков при любых кавычках (unparse 3.11 != 3.12)."""
    head = (
        "import time\n\n\ndef test_a(d, t0):\n    assert time.monotonic() - t0 + len("
    )
    tail = ") < 0.3\n"
    one = wc.scan_source(head + "f\"{d['k']}\"" + tail, "m.py").places
    two = wc.scan_source(head + "f'{d[\"k\"]}'" + tail, "m.py").places
    assert len(one) == len(two) == 1
    assert one[0].text == two[0].text
    assert "JoinedStr" in one[0].text


UNPROVEN_SOURCE = """\
import time


def test_undo(monkeypatch, fake):
    monkeypatch.setattr(time, "monotonic", fake)
    monkeypatch.undo()
    t0 = time.monotonic()
    assert time.monotonic() - t0 < 0.5


def test_undo_same_line(monkeypatch, fake):
    monkeypatch.setattr(time, "monotonic", fake); monkeypatch.undo()
    elapsed = time.monotonic() - 1
    assert elapsed < 0.53


def test_read_on_the_undo_line(monkeypatch, fake):
    monkeypatch.setattr(time, "monotonic", fake)
    monkeypatch.undo(); elapsed = time.monotonic() - 1
    assert elapsed < 0.54


def test_not_upper(flag):
    elapsed = time.monotonic() - 1
    assert not elapsed < 0.5


def test_not_lower(flag):
    elapsed = time.monotonic() - 1
    assert not elapsed >= 0.5


def test_or(flag):
    elapsed = time.monotonic() - 1
    assert elapsed < 0.5 or flag


def test_not_reversed(flag):
    elapsed = time.monotonic() - 1
    assert not 0.5 <= elapsed
"""

BOUNDED_SOURCE = """\
import time


def test_context(monkeypatch, fake, t0):
    with monkeypatch.context() as mp:
        mp.setattr(time, "monotonic", fake)
        assert time.monotonic() - t0 < 0.5
    assert time.monotonic() - t0 < 0.63


def test_and(flag):
    elapsed = time.monotonic() - 1
    assert flag and elapsed < 0.64
"""


def test_unproven_forms_are_warnings_not_silence(tmp_path, capsys):
    _write(tmp_path, UNPROVEN_SOURCE)
    _write_ledger(tmp_path, [])
    code, out = _run(tmp_path, capsys)
    assert code == 0, out
    assert "НОВОЕ МЕСТО" not in out
    for needle, message in (
        ("assert time.monotonic() - t0 < 0.5", "подмена отменяется"),
        ("assert not elapsed < 0.5", "логический контекст"),
        ("assert not elapsed >= 0.5", "логический контекст"),
        ("assert elapsed < 0.5 or flag", "логический контекст"),
        ("assert not 0.5 <= elapsed", "логический контекст"),
        ("assert elapsed < 0.53", "подмена отменяется"),
        ("assert elapsed < 0.54", "подмена отменяется"),
    ):
        line = _line_of(UNPROVEN_SOURCE, needle)
        assert f"ПРЕДУПРЕЖДЕНИЕ src/mod.py:{line} " in out, (needle, out)
        row = next(r for r in out.splitlines() if f"src/mod.py:{line} " in r)
        assert message in row, row
    assert out.count("ПРЕДУПРЕЖДЕНИЕ") == 7, out


def test_context_scope_and_and_stay_violations(tmp_path, capsys):
    _write(tmp_path, BOUNDED_SOURCE)
    _write_ledger(tmp_path, [])
    code, out = _run(tmp_path, capsys)
    assert code != 0, out
    for needle in ("assert time.monotonic() - t0 < 0.63", "assert flag and elapsed"):
        line = _line_of(BOUNDED_SOURCE, needle)
        assert f"\n  src/mod.py:{line} " in out, (needle, out)
    assert out.count("\n  src/mod.py:") == 2, out


STAR_SOURCE = """\
from time import *


def test_star():
    t0 = monotonic()
    assert monotonic() - t0 < 0.31
"""

SECOND_PATCH_REAL = """\
import time
from unittest.mock import patch


def test_a_last_patch_is_not_the_clock_one():
    p = patch("time.monotonic")
    p = patch("time.perf_counter")
    p.start()
    elapsed = time.monotonic() - 1
    assert elapsed < 0.5


def test_e_two_assignments_on_one_line():
    p = patch("time.monotonic"); p = patch("os.getcwd")
    p.start()
    elapsed = time.monotonic() - 1
    assert elapsed < 0.52


def test_d_last_patch_is_not_a_clock_at_all():
    p = patch("time.monotonic")
    p = patch("os.getcwd")
    p.start()
    elapsed = time.monotonic() - 1
    assert elapsed < 0.51
"""

SECOND_PATCH_FAKE = """\
import time
from unittest.mock import patch


def test_b_last_patch_is_the_clock_one():
    p = patch("time.perf_counter")
    p = patch("time.monotonic")
    p.start()
    elapsed = time.monotonic() - 1
    assert elapsed < 0.5


def test_f_assignment_and_start_on_one_line():
    p = patch("time.monotonic"); p.start()
    elapsed = time.monotonic() - 1
    assert elapsed < 0.5


def test_c_assignment_after_start_does_not_count():
    p = patch("time.monotonic")
    p.start()
    p = patch("time.perf_counter")
    elapsed = time.monotonic() - 1
    assert elapsed < 0.5
"""


def test_star_import_of_time_gives_clocks(tmp_path, capsys):
    _write(tmp_path, STAR_SOURCE)
    _write_ledger(tmp_path, [])
    code, out = _run(tmp_path, capsys)
    assert code != 0, out
    line = _line_of(STAR_SOURCE, "assert monotonic()")
    assert f"\n  src/mod.py:{line} " in out, out


def test_start_enables_the_last_assigned_patch(tmp_path, capsys):
    _write(tmp_path, SECOND_PATCH_REAL)
    _write_ledger(tmp_path, [])
    code, out = _run(tmp_path, capsys)
    assert code != 0, out
    for needle in (
        "assert elapsed < 0.5\n",
        "assert elapsed < 0.51",
        "assert elapsed < 0.52",
    ):
        line = _line_of(SECOND_PATCH_REAL, needle.strip())
        assert f"\n  src/mod.py:{line} " in out, out
    _write(tmp_path, SECOND_PATCH_FAKE)
    code, out = _run(tmp_path, capsys)
    assert code == 0, out
    assert "ПРЕДУПРЕЖДЕНИЕ" not in out, out
