"""scripts/call_sites.py: name candidates for a symbol or a module (#1853).

The script runs as a real subprocess against a real fixture tree: the
contract here is the exit code and the text a person reads, and an in-process
call would prove neither.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "call_sites.py"

LIB = (
    "def f():\n"
    "    return 1\n"
    "\n"
    "\n"
    "async def af():\n"
    "    return 2\n"
    "\n"
    "\n"
    "def _private():\n"
    "    return 3\n"
    "\n"
    "\n"
    "class Box:\n"
    "    def method(self):\n"
    "        return 4\n"
    "\n"
    "\n"
    "def outer():\n"
    "    def inner():\n"
    "        return 5\n"
    "    return inner\n"
)

FILES = {
    "hub/lib.py": LIB,
    "hub/direct.py": "from hub.lib import f\n\n\ndef use():\n    return f()\n",
    "hub/attr.py": "import hub.lib as mod\n\n\ndef use2():\n    return mod.f()\n",
    "hub/method.py": (
        "from hub.lib import f\n\n\nclass K:\n    def run(self):\n        return f()\n"
    ),
    "hub/argument.py": (
        "import asyncio\nfrom hub.lib import f\n\n\n"
        "def hand_over():\n    return asyncio.to_thread(f)\n"
    ),
    "hub/aliased.py": "from hub.lib import f as g\n\n\ndef through_alias():\n    return g()\n",
    "hub/broken.py": "def oops(:\n",
    "hub/only_private.py": "def _hidden():\n    return 1\n\n\nclass C:\n    def m(self):\n        return 1\n",
    "tests/test_lib.py": "from hub.lib import af\n\n\ndef test_it():\n    af()\n",
}


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO,
    )


def _places(out: str) -> set[str]:
    return set(re.findall(r"(?:hub|tests)/[\w/]+\.py:\d+", out))


def test_the_query_lists_name_candidates(tmp_path: Path) -> None:
    root = _tree(tmp_path / "proj", FILES)

    found = _run("--root", str(root), "f")
    assert found.returncode == 0, found.stderr
    assert _places(found.stdout) == {
        "hub/direct.py:5",
        "hub/attr.py:5",
        "hub/method.py:6",
        "hub/argument.py:6",
    }
    assert "through_alias" not in found.stdout
    assert "use2" in found.stdout and "run" in found.stdout
    # The blind zones are named in the header, not left for the reader to guess.
    for word in ("алиас", "registry", "getattr", "одноимённые", "ref = f"):
        assert word in found.stdout, word
    assert "кандидаты по имени" in found.stdout
    # A file nobody could parse is shown, not silently skipped.
    assert "hub/broken.py" in found.stdout

    # A dotted symbol takes the short name.
    dotted = _run("--root", str(root), "lib.f")
    assert dotted.returncode == 0 and _places(dotted.stdout) == _places(found.stdout)

    # No calls is an answer (rc 0) and says so.
    none = _run("--root", str(root), "never_called")
    assert none.returncode == 0, none.stderr
    assert "вызовов нет" in none.stdout

    # --root looks only at that directory.
    other = _tree(tmp_path / "other", {"hub/z.py": "def z():\n    return f()\n"})
    only = _run("--root", str(other), "f")
    assert only.returncode == 0 and _places(only.stdout) == {"hub/z.py:2"}

    # An empty index is not "no calls": nothing was analysed.
    empty = tmp_path / "empty"
    empty.mkdir()
    blank = _run("--root", str(empty), "f")
    assert blank.returncode == 2
    assert "не проанализировано" in blank.stdout
    assert "вызовов нет" not in blank.stdout
    blank_json = _run("--root", str(empty), "f", "--json")
    assert blank_json.returncode == 2
    assert json.loads(blank_json.stdout)["status"] == "not_analysed"


def test_a_module_expands_to_its_top_level_public_functions(tmp_path: Path) -> None:
    root = _tree(tmp_path / "proj", FILES)

    as_json = _run("--root", str(root), "hub/lib.py", "--json")
    assert as_json.returncode == 0, as_json.stderr
    data = json.loads(as_json.stdout)
    assert data["status"] == "ok"
    target = data["targets"][0]
    assert [s["symbol"] for s in target["symbols"]] == ["f", "af", "outer"]
    json_places = {
        f"{site['file']}:{site['line']}"
        for sym in target["symbols"]
        for site in sym["sites"]
    }
    assert "hub/direct.py:5" in json_places and "tests/test_lib.py:5" in json_places
    for name in ("_private", "method", "inner", "Box"):
        assert all(s["symbol"] != name for s in target["symbols"])

    # The text answer carries the same places as the JSON one.
    text = _run("--root", str(root), "hub/lib.py")
    assert text.returncode == 0
    assert _places(text.stdout) == json_places

    # Markdown is a table.
    md = _run("--root", str(root), "hub/lib.py", "--markdown")
    assert md.returncode == 0
    rows = [ln for ln in md.stdout.splitlines() if ln.startswith("|")]
    assert len(rows) >= 3 and re.match(r"^\|[\s|:-]+\|$", rows[1])
    assert _places(md.stdout) == json_places

    # A module without public top-level functions: an answer, rc 0.
    bare = _run("--root", str(root), "hub/only_private.py")
    assert bare.returncode == 0, bare.stderr
    assert "публичных функций нет" in bare.stdout

    # A missing or unparsable module is an error, in JSON too.
    for module in ("hub/missing.py", "hub/broken.py"):
        err = _run("--root", str(root), module)
        assert err.returncode == 2, module
        err_json = _run("--root", str(root), module, "--json")
        assert err_json.returncode == 2, module
        body = json.loads(err_json.stdout)
        assert body["status"] == "error"
        assert body["targets"][0]["error"]


def test_the_indexer_comes_from_this_checkout_not_from_pythonpath(
    tmp_path: Path,
) -> None:
    fake = tmp_path / "fake"
    (fake / "hub" / "services").mkdir(parents=True)
    (fake / "hub" / "__init__.py").write_text("", encoding="utf-8")
    (fake / "hub" / "services" / "__init__.py").write_text("", encoding="utf-8")
    (fake / "hub" / "services" / "call_sites.py").write_text(
        "raise RuntimeError('foreign indexer imported')\n", encoding="utf-8"
    )
    root = _tree(tmp_path / "proj", FILES)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    done = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--root", str(root), "f"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=elsewhere,
        env={**os.environ, "PYTHONPATH": str(fake)},
    )
    assert done.returncode == 0, done.stderr
    assert "foreign indexer" not in done.stderr + done.stdout
    assert "hub/direct.py:5" in done.stdout


def _load_script():
    spec = importlib.util.spec_from_file_location("call_sites_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_foreign_indexer_is_rc_2_and_json_when_asked(monkeypatch, capsys) -> None:
    module = _load_script()
    assert module._indexer_problem() is None
    monkeypatch.setattr(module, "_indexer_file", lambda: "/other/hub/call_sites.py")
    assert module.main(["f", "--json"]) == 2
    body = json.loads(capsys.readouterr().out)
    assert body["status"] == "error" and "не из этой ветки" in body["message"]
    assert module.main(["f"]) == 2


def test_argument_errors_are_json_under_json_and_rc_2(tmp_path: Path) -> None:
    for argv in (["--json"], ["f", "--json", "--unknown"]):
        done = _run(*argv)
        assert done.returncode == 2, argv
        body = json.loads(done.stdout)
        assert body["status"] == "error" and body["message"]
    plain = _run("--unknown")
    assert plain.returncode == 2 and plain.stdout == "" and plain.stderr
