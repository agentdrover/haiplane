"""Бюджет размера файла держится проверкой, а не памятью автора (#1669).

Размер .py-файла ничем не ограничен, а крупный файл даёт крупный дифф, который
deep-ревью не дочитывает. Страж проверяется во все стороны: краснеет на новом
файле выше порога, на waived-файле выше потолка, на записи без причины, берёт
пороги из pyproject.toml по деревьям и сохраняет removed_by при --update.
Первые пять тестов идут на временных деревьях, не на реальном репозитории.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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
    waivers: list[dict] | None = None,
) -> None:
    (root / "pyproject.toml").write_text(
        "[tool.haiplane.file_size]\n"
        f"hub = {hub}\nscripts = {scripts}\ntests = {tests}\n",
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
    """AC-2: потолок = замер + 5%; 90% потолка называется вслух."""
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
    proc = _run(tmp_path)
    assert proc.returncode == 0
    assert "запас почти съеден" in proc.stdout


def test_stale_waiver_must_be_removed(tmp_path):
    """AC-3: запись без файла или с файлом не выше порога красит проверку."""
    waivers = [
        {
            "path": "hub/gone.py",
            "measured": 1600,
            "max": 1680,
            "removed_by": "unclaimed",
        },
        {
            "path": "hub/small.py",
            "measured": 1600,
            "max": 1680,
            "removed_by": "unclaimed",
        },
    ]
    _make_tree(tmp_path, waivers=waivers)
    _write_lines(tmp_path, "hub/small.py", 10)
    proc = _run(tmp_path)
    assert proc.returncode != 0
    assert "hub/gone.py" in proc.stdout
    assert "hub/small.py" in proc.stdout
    assert "снимите" in proc.stdout


def test_thresholds_come_from_pyproject_per_tree(tmp_path):
    """AC-4: каждое дерево меряется своим порогом из pyproject.toml."""
    _make_tree(tmp_path, tests=2000)
    _write_lines(tmp_path, "tests/test_big.py", 2500)
    _write_lines(tmp_path, "scripts/big.py", 2500)
    proc = _run(tmp_path)
    assert proc.returncode != 0
    assert "tests/test_big.py" in proc.stdout
    assert "scripts/big.py" in proc.stdout
    assert "2000" in proc.stdout
    assert "1500" in proc.stdout

    _make_tree(tmp_path, tests=3000)
    proc = _run(tmp_path)
    assert proc.returncode != 0
    assert "scripts/big.py" in proc.stdout
    assert "tests/test_big.py" not in proc.stdout


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


def test_repository_is_green_under_the_size_budget():
    """AC-6: реальное дерево зелёное, шаг есть в ci.yml, Makefile и SIGNATURES."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, cwd=REPO_ROOT
    )
    assert proc.returncode == 0, proc.stdout
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/file_size_budget.py" in ci
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "scripts/file_size_budget.py" in makefile
    parity = (REPO_ROOT / "tests" / "test_local_checks_match_ci.py").read_text(
        encoding="utf-8"
    )
    assert '"file_size_budget.py"' in parity
    ledger = json.loads((REPO_ROOT / LEDGER_REL).read_text(encoding="utf-8"))
    assert ledger["waivers"]
