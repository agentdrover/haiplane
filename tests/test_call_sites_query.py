"""scripts/call_sites.py: name candidates for a symbol or a module (#1853).

The script runs as a real subprocess against a real fixture tree: the
contract here is the exit code and the text a person reads, and an in-process
call would prove neither.
"""

from __future__ import annotations

import json
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
    assert blank.returncode != 0
    assert "не проанализировано" in blank.stdout
    assert "вызовов нет" not in blank.stdout
    blank_json = _run("--root", str(empty), "f", "--json")
    assert blank_json.returncode != 0
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
        assert err.returncode != 0, module
        err_json = _run("--root", str(root), module, "--json")
        assert err_json.returncode != 0, module
        body = json.loads(err_json.stdout)
        assert body["status"] == "error"
        assert body["targets"][0]["error"]
