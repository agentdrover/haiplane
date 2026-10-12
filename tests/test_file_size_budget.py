"""Бюджет размера файла держится проверкой, а не памятью автора (#1669).

Размер .py-файла ничем не ограничен, а крупный файл даёт крупный дифф, который
deep-ревью не дочитывает. Страж проверяется во все стороны: краснеет на новом
файле выше порога, на waived-файле выше потолка, на записи без причины, берёт
пороги из pyproject.toml по деревьям и сохраняет removed_by при --update.
Первые пять тестов идут на временных деревьях, не на реальном репозитории.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "file_size_budget.py"
LEDGER_REL = Path("docs") / "agent-context" / "file-size-budget.json"


def _write_lines(root: Path, rel: str, lines: int) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n" * lines, encoding="utf-8")


def _make_tree(
    root: Path,
    *,
    hub: int = 1500,
    scripts: int = 1500,
    tests: int = 3000,
    near_pct: int | None = 90,
    waivers: list[dict] | None = None,
) -> None:
    near = "" if near_pct is None else f"near_pct = {near_pct}\n"
    (root / "pyproject.toml").write_text(
        "[tool.haiplane.file_size]\n"
        f"hub = {hub}\nscripts = {scripts}\ntests = {tests}\n{near}",
        encoding="utf-8",
    )
    ledger = root / LEDGER_REL
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({"_comment": [], "headroom_pct": 5, "waivers": waivers or []}),
        encoding="utf-8",
    )


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *args],
        capture_output=True,
        text=True,
    )


def test_new_file_over_threshold_is_named(tmp_path):
    """AC-1: новый файл на 1501 строку без записи красит проверку."""
    _make_tree(tmp_path)
    _write_lines(tmp_path, "hub/big.py", 1501)
    proc = _run(tmp_path)
    assert proc.returncode != 0
    assert "hub/big.py" in proc.stdout
    assert "1501" in proc.stdout
    assert "1500" in proc.stdout

    _write_lines(tmp_path, "hub/big.py", 1500)
    assert _run(tmp_path).returncode == 0


def test_waived_file_cannot_outgrow_its_ceiling(tmp_path):
    """AC-2: потолок = замер + 5%; на потолке зелено, выше красно."""
    entry = {
        "path": "hub/old.py",
        "measured": 2000,
        "max": 2100,
        "removed_by": "unclaimed",
    }
    _make_tree(tmp_path, waivers=[entry])

    _write_lines(tmp_path, "hub/old.py", 2101)
    proc = _run(tmp_path)
    assert proc.returncode != 0
    assert "hub/old.py" in proc.stdout
    assert "2100" in proc.stdout
    assert "2101" in proc.stdout

    _write_lines(tmp_path, "hub/old.py", 2100)
    assert _run(tmp_path).returncode == 0

    _write_lines(tmp_path, "hub/old.py", 1890)
    assert _run(tmp_path).returncode == 0


def _waiver(path: str, measured: int = 1600) -> dict:
    return {"path": path, "measured": measured, "max": 1680, "removed_by": "unclaimed"}


@pytest.mark.parametrize("lines", [None, 10, 1499, 1500])
def test_stale_waiver_must_be_removed(tmp_path, lines):
    """AC-3: запись без файла или с файлом не выше порога красит проверку.

    None — файл удалён; 1500 — ровно на пороге, а порог включительно допустим.
    """
    _make_tree(tmp_path, waivers=[_waiver("hub/w.py")])
    if lines is not None:
        _write_lines(tmp_path, "hub/w.py", lines)
    proc = _run(tmp_path)
    assert proc.returncode != 0
    assert "hub/w.py" in proc.stdout
    assert "снимите" in proc.stdout


def test_waiver_one_above_threshold_is_not_stale(tmp_path):
    """AC-3: граница с другой стороны — 1501 при записи ещё не stale."""
    _make_tree(tmp_path, waivers=[_waiver("hub/w.py")])
    _write_lines(tmp_path, "hub/w.py", 1501)
    assert _run(tmp_path).returncode == 0


@pytest.mark.parametrize(
    ("tree", "limit"), [("hub", 111), ("scripts", 222), ("tests", 333)]
)
def test_thresholds_come_from_pyproject_per_tree(tmp_path, tree, limit):
    """AC-4: каждое дерево меряется своим порогом из pyproject.toml.

    Пороги нестандартные для всех трёх деревьев, чтобы зашитые 1500/3000 не
    проходили; граница проверяется как limit (зелёный) и limit+1 (красный).
    """
    limits = {"hub": 111, "scripts": 222, "tests": 333}
    _make_tree(tmp_path, **limits)
    _write_lines(tmp_path, f"{tree}/f.py", limit)
    assert _run(tmp_path).returncode == 0
    _write_lines(tmp_path, f"{tree}/f.py", limit + 1)
    proc = _run(tmp_path)
    assert proc.returncode != 0
    assert f"{tree}/f.py" in proc.stdout
    assert str(limit) in proc.stdout
    assert str(limit + 1) in proc.stdout


def _grown_waiver(path: str) -> dict:
    return {"path": path, "measured": 2000, "max": 2100, "removed_by": "u"}


@pytest.mark.parametrize(
    ("lines", "grown"), [(1890, False), (2000, False), (2001, True), (2100, True)]
)
def test_waived_file_that_grew_past_its_measure_is_named(tmp_path, lines, grown):
    """AC-1: файл из ledger сверх замера называется, но CI не краснеет."""
    _make_tree(tmp_path, waivers=[_grown_waiver("hub/old.py")])
    _write_lines(tmp_path, "hub/old.py", lines)
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stdout
    data = json.loads(_run(tmp_path, "--json").stdout)
    assert "loud" not in data
    if grown:
        assert data["grown"] == [
            {"path": "hub/old.py", "lines": lines, "measured": 2000, "max": 2100}
        ]
        assert "вырос сверх замера" in proc.stdout
        assert "hub/old.py" in proc.stdout
        assert "2000" in proc.stdout
        assert str(lines) in proc.stdout
    else:
        assert data["grown"] == []
        assert "вырос сверх замера" not in proc.stdout
        assert "запас почти съеден" not in proc.stdout
        assert "hub/old.py" not in proc.stdout


def test_file_above_its_ceiling_or_threshold_is_red_not_a_warning(tmp_path):
    """AC-1/AC-2: верхние границы — выше max файл over, выше порога new."""
    _make_tree(tmp_path, scripts=100, waivers=[_grown_waiver("hub/old.py")])
    _write_lines(tmp_path, "hub/old.py", 2101)
    _write_lines(tmp_path, "scripts/big.py", 101)
    proc = _run(tmp_path, "--json")
    assert proc.returncode != 0
    data = json.loads(proc.stdout)
    assert [i["path"] for i in data["over"]] == ["hub/old.py"]
    assert data["grown"] == []
    assert [i["path"] for i in data["new"]] == ["scripts/big.py"]
    assert data["near"] == []


def test_unwaived_file_near_its_threshold_is_named(tmp_path):
    """AC-2: файл вне ledger у порога называется; near_pct — не дерево."""
    limits = {"hub": 100, "scripts": 200, "tests": 300}
    _make_tree(tmp_path, **limits)
    for tree, limit in limits.items():
        _write_lines(tmp_path, f"{tree}/below.py", limit * 9 // 10 - 1)
        _write_lines(tmp_path, f"{tree}/edge.py", limit * 9 // 10)
        _write_lines(tmp_path, f"{tree}/full.py", limit)
    proc = _run(tmp_path, "--json")
    assert proc.returncode == 0, proc.stdout
    near = {item["path"]: item for item in json.loads(proc.stdout)["near"]}
    expected = {}
    for tree, limit in limits.items():
        assert f"{tree}/below.py" not in near
        for name, lines in (("edge", limit * 9 // 10), ("full", limit)):
            expected[f"{tree}/{name}.py"] = {
                "path": f"{tree}/{name}.py",
                "lines": lines,
                "threshold": limit,
                "used_pct": round(100.0 * lines / limit, 1),
            }
    assert near == expected
    assert "у порога" in _run(tmp_path).stdout
    # near_pct не дерево измерения: каталог с таким именем не обходится
    _write_lines(tmp_path, "near_pct/f.py", 5000)
    assert _run(tmp_path).returncode == 0

    _make_tree(tmp_path, **limits, near_pct=80)
    data = json.loads(_run(tmp_path, "--json").stdout)
    paths = {item["path"] for item in data["near"]}
    assert "hub/below.py" in paths  # 89 >= 80
    assert "tests/below.py" in paths  # 269 >= 240

    _make_tree(tmp_path, **limits, near_pct=None)
    proc = _run(tmp_path)
    assert proc.returncode != 0
    assert "pyproject.toml: нет ключа near_pct" in proc.stdout + proc.stderr
    assert "Traceback" not in proc.stdout + proc.stderr


def test_update_with_missing_near_pct_is_an_error_and_keeps_the_ledger(tmp_path):
    """near_pct читается до любого режима: --update без ключа ledger не трогает."""
    _make_tree(tmp_path, near_pct=None, waivers=[_grown_waiver("hub/old.py")])
    _write_lines(tmp_path, "hub/old.py", 2000)
    before = (tmp_path / LEDGER_REL).read_bytes()
    proc = _run(tmp_path, "--update")
    assert proc.returncode != 0
    assert "pyproject.toml: нет ключа near_pct" in proc.stdout + proc.stderr
    assert (tmp_path / LEDGER_REL).read_bytes() == before


def test_near_list_is_complete_in_json_and_short_in_text(tmp_path):
    """AC-3: JSON отдаёт все near и grown, текст — пять near и счёт."""
    _make_tree(tmp_path, hub=100, scripts=200, tests=300)
    near_files = {
        "hub/a.py": 91,  # 91%
        "hub/b.py": 99,  # 99%
        "scripts/c.py": 198,  # 99%
        "scripts/d.py": 182,  # 91%
        "tests/e.py": 300,  # 100%
        "tests/f.py": 285,  # 95%
        "tests/g.py": 271,  # 90.3%
    }
    for path, lines in near_files.items():
        _write_lines(tmp_path, path, lines)
    waivers = [_grown_waiver(f"hub/w{i}.py") for i in range(6)]
    _make_tree(tmp_path, hub=100, scripts=200, tests=300, waivers=waivers)
    for i in range(6):
        _write_lines(tmp_path, f"hub/w{i}.py", 2001 + i)
    data = json.loads(_run(tmp_path, "--json").stdout)
    assert sorted(i["path"] for i in data["near"]) == sorted(near_files)
    assert sorted(i["path"] for i in data["grown"]) == [
        f"hub/w{i}.py" for i in range(6)
    ]
    text = _run(tmp_path).stdout
    assert text.count("вырос сверх замера") == 1  # заголовок раздела
    for i in range(6):
        assert f"hub/w{i}.py" in text
    top5 = ["tests/e.py", "hub/b.py", "scripts/c.py", "tests/f.py", "hub/a.py"]
    for path in top5:
        assert path in text
    for path in ("scripts/d.py", "tests/g.py"):
        assert path not in text
    assert "и ещё 2 файлов у порога (полный список: --json)" in text
    assert "разделите по темам до того, как файл упрётся в порог" in text


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b'x = "\xe2\x80\xa8"\n' * 1500, 1500),  # U+2028 в литерале
        (b"x = 1\x0c\n" * 1500, 1500),  # form feed
        (b"x = 1\r\n" * 1500, 1500),  # CRLF — одна строка
        (b"x = 1\n" * 1499 + b"x = 1", 1500),  # без завершающего перевода
    ],
    ids=["u2028", "formfeed", "crlf", "no-final-newline"],
)
def test_lines_are_physical_lines(tmp_path, payload, expected):
    """P2-2: строки считаются как wc -l, а не как str.splitlines()."""
    _make_tree(tmp_path)
    (tmp_path / "hub").mkdir()
    (tmp_path / "hub" / "f.py").write_bytes(payload)
    proc = _run(tmp_path, "--json")
    assert proc.returncode == 0, proc.stdout
    # на пороге зелёно; на единицу меньше порога файл не попадает в ledger
    _make_tree(tmp_path, hub=expected - 1)
    out = _run(tmp_path)
    assert out.returncode != 0
    assert f"— {expected} > порог" in out.stdout


def test_update_keeps_removed_by(tmp_path):
    """AC-5: --update обновляет замер и потолок, removed_by сохраняет."""
    old = {
        "path": "hub/old.py",
        "measured": 1600,
        "max": 1680,
        "removed_by": "task:#1",
    }
    _make_tree(tmp_path, waivers=[old])
    _write_lines(tmp_path, "hub/old.py", 2000)
    _write_lines(tmp_path, "hub/new.py", 1600)
    proc = _run(tmp_path, "--update")
    assert proc.returncode == 0
    waivers = {
        w["path"]: w
        for w in json.loads((tmp_path / LEDGER_REL).read_text(encoding="utf-8"))[
            "waivers"
        ]
    }
    assert waivers["hub/old.py"]["measured"] == 2000
    assert waivers["hub/old.py"]["max"] == 2100
    assert waivers["hub/old.py"]["removed_by"] == "task:#1"
    assert waivers["hub/new.py"]["measured"] == 1600
    assert waivers["hub/new.py"]["max"] == 1680
    assert waivers["hub/new.py"]["removed_by"] == "unclaimed"
    assert _run(tmp_path).returncode == 0


_COMMAND = "uv run python scripts/file_size_budget.py"


def _ci_test_job() -> str:
    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    body = text.split("\n  test:\n", 1)[1]
    return re.split(r"\n  [A-Za-z0-9_-]+:\n", body, maxsplit=1)[0]


def _make_recipes() -> dict[str, tuple[list[str], list[str]]]:
    """Цель Makefile -> (зависимости, строки рецепта)."""
    recipes: dict[str, tuple[list[str], list[str]]] = {}
    current = None
    for line in (REPO_ROOT / "Makefile").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_-]+):(.*)$", line)
        if m:
            current = m.group(1)
            recipes[current] = (m.group(2).split(), [])
        elif line.startswith("\t") and current:
            recipes[current][1].append(line.strip())
    return recipes


def _reachable_from_check() -> list[str]:
    recipes = _make_recipes()
    seen: list[str] = []
    todo = ["check"]
    while todo:
        target = todo.pop()
        if target in seen or target not in recipes:
            continue
        seen.append(target)
        todo.extend(recipes[target][0])
    return [line for t in seen for line in recipes[t][1]]


def test_repository_is_green_under_the_size_budget():
    """AC-6: реальное дерево зелёное, шаг есть в ci.yml, Makefile и SIGNATURES."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, cwd=REPO_ROOT
    )
    assert proc.returncode == 0, proc.stdout
    data = json.loads(
        subprocess.run(
            [sys.executable, str(SCRIPT), "--json"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        ).stdout
    )
    assert "grown" in data
    assert "near" in data
    assert "loud" not in data
    assert "tests/test_cli.py" not in {item["path"] for item in data["near"]}
    assert "не ужимать" in (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    # Ровно проверка, без --update: подмена на перезапись ledger прошла бы
    # поиск по подстроке, а проверкой быть перестала бы.
    step = re.search(
        r"- name: File size budget\n(?:(?!\n      - name:).)*", _ci_test_job(), re.S
    )
    assert step, "нет шага File size budget в job test"
    assert re.search(rf"run: {re.escape(_COMMAND)}\s*$", step.group(0), re.M)
    assert "--update" not in step.group(0)
    assert "!cancelled()" in step.group(0)
    assert _COMMAND in _reachable_from_check()
    parity = (REPO_ROOT / "tests" / "test_local_checks_match_ci.py").read_text(
        encoding="utf-8"
    )
    assert '"file_size_budget.py"' in parity
    ledger = json.loads((REPO_ROOT / LEDGER_REL).read_text(encoding="utf-8"))
    assert ledger["waivers"]
