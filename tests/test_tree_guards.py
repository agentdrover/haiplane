"""The ledger of tree guards is complete and honest (#2000).

A tree guard is a test that checks a property of the whole repository, not of a
function: it walks the real tree (``walks_tree``) or runs a repository checker on
it (``repo_checker``). ``make test-changed`` always runs them, because such a test
goes red on a change that touches nothing near it (a new long file, a leftover
name, a broken link).

This file keeps ``docs/agent-context/tree-guards.json`` from going stale:

* the detector below finds every test module that walks a file tree; each one
  must be classified, as a guard or as an exemption with a reason (it walks a
  temporary tree, or a narrow data directory);
* a ledger entry names a file that exists, has a reason, and is in one list only.

The detector FINDS CANDIDATES; whether a candidate walks the real repository is a
decision, written down in the ledger. ``repo_checker`` entries are kept by hand.

Forms the detector knows (the others are invisible to it, by design):

* ``<anything>.rglob(...)`` and ``<anything>.glob(...)`` - a glob with any pattern;
* ``os.walk(...)`` under any alias (``import os as fs``, ``from os import walk``) -
  NOT ``ast.walk(...)``, which walks a syntax tree;
* any call that is handed the word ``"ls-files"`` or ``"grep"`` as an argument,
  directly or inside a list (``git("ls-files")``, ``run(["git", "grep", ...])``).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LEDGER = REPO_ROOT / "docs" / "agent-context" / "tree-guards.json"
_GIT_WORDS = {"ls-files", "grep"}
CRITERIA = {"walks_tree", "repo_checker"}


def tree_walkers(source: str) -> set[str]:
    """The forms of a file-tree walk found in ``source`` (empty = none)."""
    tree = ast.parse(source)
    os_names, walk_names = _os_aliases(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in {"rglob", "glob"}:
                found.add(func.attr)
            elif (
                func.attr == "walk"
                and isinstance(func.value, ast.Name)
                and func.value.id in os_names
            ):
                found.add("os.walk")
        elif isinstance(func, ast.Name) and func.id in walk_names:
            found.add("os.walk")
        if _names_git_listing(node):
            found.add("git ls-files/grep")
    return found


def _os_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Names bound to the ``os`` module and to ``os.walk`` (any alias)."""
    os_names: set[str] = set()
    walk_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            os_names |= {a.asname or a.name for a in node.names if a.name == "os"}
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            walk_names |= {a.asname or a.name for a in node.names if a.name == "walk"}
    return os_names, walk_names


def _names_git_listing(call: ast.Call) -> bool:
    """A call that hands ``"ls-files"`` or ``"grep"`` over as an argument.

    Direct (``git("ls-files")``, ``run(["git", "grep", ...])``) or inside a list:
    the helper that wraps git does not matter, the word does. A candidate that is
    not git at all is classified in the ledger as an exemption.
    """
    for arg in [*call.args, *(kw.value for kw in call.keywords)]:
        elements = arg.elts if isinstance(arg, (ast.List, ast.Tuple)) else [arg]
        words = {e.value for e in elements if isinstance(e, ast.Constant)}
        if words & _GIT_WORDS:
            return True
    return False


def _ledger() -> dict:
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def _candidates() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
        try:
            forms = tree_walkers(path.read_text(encoding="utf-8"))
        except SyntaxError:  # a broken test file fails its own collection
            continue
        if forms:
            found[str(path.relative_to(REPO_ROOT))] = forms
    return found


def test_every_tree_walker_is_classified() -> None:
    """AC-4: a candidate outside both lists is red - decide, then write it down."""
    ledger = _ledger()
    classified = {e["path"] for e in ledger["guards"]} | {
        e["path"] for e in ledger["exempt"]
    }
    unclassified = sorted(set(_candidates()) - classified)
    assert not unclassified, (
        "these tests walk a file tree but are in neither 'guards' nor 'exempt' of "
        f"docs/agent-context/tree-guards.json: {unclassified}. A guard checks a "
        "property of the real repository; an exemption walks a temporary or narrow "
        "directory (say which in 'reason')."
    )


def test_the_ledger_is_well_formed() -> None:
    """AC-4: existing files, a reason on every entry, no file in two places or twice."""
    ledger = _ledger()
    paths: list[str] = []
    for entry in ledger["guards"]:
        assert entry["criterion"] in CRITERIA, entry
        assert entry["reason"].strip(), entry
        paths.append(entry["path"])
    for entry in ledger["exempt"]:
        assert entry["reason"].strip(), entry
        paths.append(entry["path"])
    for path in paths:
        assert (REPO_ROOT / path).is_file(), f"{path} is in the ledger but not on disk"
    duplicates = sorted({p for p in paths if paths.count(p) > 1})
    assert not duplicates, f"in the ledger twice or in both lists: {duplicates}"


def test_walks_tree_entries_really_walk_a_tree() -> None:
    """A 'walks_tree' guard that no longer walks anything has lost its reason."""
    for entry in _ledger()["guards"]:
        if entry["criterion"] != "walks_tree":
            continue
        source = (REPO_ROOT / entry["path"]).read_text(encoding="utf-8")
        assert tree_walkers(source), (
            f"{entry['path']} is a walks_tree guard but the detector finds no walk: "
            "reclassify it (repo_checker, exempt) or remove it from the ledger"
        )


def test_the_detector_tells_a_tree_walk_from_a_syntax_walk() -> None:
    """Positive and negative examples of the forms the detector knows."""
    positive = {
        "rglob": 'from pathlib import Path\nlist(Path("hub").rglob("*.py"))\n',
        "glob": 'list(root.glob("*.json"))\n',
        "os.walk": 'import os\nfor r, d, f in os.walk("hub"): pass\n',
        "git ls-files/grep": (
            'import subprocess\nsubprocess.run(["git", "ls-files"], check=True)\n'
        ),
    }
    aliases = {
        "import os as fs\nfs.walk('hub')\n": "os.walk",
        "from os import walk\nwalk('hub')\n": "os.walk",
        "from os import walk as w\nw('hub')\n": "os.walk",
        "def go():\n    return git('ls-files')\n": "git ls-files/grep",
        "helper(['git', 'grep', '-n', 'x'])\n": "git ls-files/grep",
    }
    for source, form in aliases.items():
        assert form in tree_walkers(source), source
    for form, source in positive.items():
        assert form in tree_walkers(source), (form, source)
    assert not tree_walkers("import ast\nfor n in ast.walk(tree): pass\n")
    assert not tree_walkers('subprocess.run(["git", "status"])\n')
    assert not tree_walkers("node.walk()\n")
