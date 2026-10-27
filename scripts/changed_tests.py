"""Run the tests that a diff makes worth running, locally (#2000).

The full suite is the job of CI (7-9 minutes on a busy workstation against ~2.5
minutes in four parallel parts). Locally the useful question is smaller: which
test files can this diff have broken that I would otherwise learn about from a
red CI run?  Measured on 68 red CI runs (25.08-22.10): the author's own tests
catch 28% of the failing files, the author's tests plus the tree guards
(tests that check a property of the whole repository) 75-82%. Names of changed
symbols and the change-map add 7 points for ten times the selection, so they are
not part of the selection.

Selection (each file is printed with its reason):

* a changed or new ``tests/test_*.py`` file;
* a tree guard from ``docs/agent-context/tree-guards.json``;
* a ``tests/test_*.py`` file that contains the path of a changed file which is
  not Python (a template, a JSON ledger, a doc, a fixture or golden file under
  ``tests/``). For a file under ``tests/`` its name alone is enough, as a
  string literal: fixtures are usually joined to a directory constant.

Standard library only. The exit code is pytest's.  This is a convenience for the
author, not evidence: CI stays the authority, a miss costs one CI cycle.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

GUARDS_LEDGER = "docs/agent-context/tree-guards.json"
# Up to this many files the workers cost more than they save: every xdist worker
# imports tests/conftest.py again (measured: -n 4 on 5 files 10.8 s of CPU against
# 2.0-9.3 s with -n 0).
SMALL_SELECTION = 8
DEFAULT_BASE = "develop"


def _git(root: Path, *args: str) -> tuple[int, bytes]:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    return proc.returncode, proc.stdout


def base_branch(root: Path) -> str:
    rc, out = _git(root, "config", "--get", "haiplane.baseBranch")
    name = out.decode("utf-8", "replace").strip()
    return name if rc == 0 and name else DEFAULT_BASE


def _split_nul(raw: bytes) -> list[str]:
    return [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]


def changed_files(root: Path) -> tuple[list[str], str]:
    """(changed paths relative to the root, a warning or '').

    Changed = the working tree against the merge-base with ``origin/<base>`` plus
    the untracked files. A rename is a deletion plus a new file; a deleted path is
    kept (a test that reads it is still worth running; ``select`` drops a deleted
    test file from the run). When the diff cannot be had - no ``origin/<base>``, no
    common ancestor, git failing - the answer is no changed paths and a warning
    saying why: the selection is then the tree guards only, never a guess.
    """
    base = f"origin/{base_branch(root)}"
    rc, mb = _git(root, "merge-base", "HEAD", base)
    anchor = mb.decode().strip() if rc == 0 else ""
    if not anchor:
        return [], f"no merge-base with {base}: only the tree guards are selected"
    rc, out = _git(root, "diff", "--name-only", "-z", "--no-renames", anchor)
    if rc != 0:
        return [], "git diff failed: only the tree guards are selected"
    paths = set(_split_nul(out))
    rc, out = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    if rc != 0:
        return [], "git ls-files failed: only the tree guards are selected"
    return sorted(paths | set(_split_nul(out))), ""


def guards(root: Path) -> list[str]:
    """The tree guards of the ledger that exist on disk, in ledger order."""
    path = root / GUARDS_LEDGER
    if not path.is_file():
        return []
    entries = json.loads(path.read_text(encoding="utf-8")).get("guards", [])
    return [e["path"] for e in entries if (root / e["path"]).is_file()]


def _is_test(path: str) -> bool:
    return path.startswith("tests/test_") and path.endswith(".py")


def _mentions(text: str, path: str) -> bool:
    """Whether a test's text names the changed non-Python ``path``.

    The full path is looked for as it is. Under ``tests/`` the bare name is enough
    (fixtures are usually joined to a directory constant) but only as a string
    literal or the tail of one - ``"view.json"``, ``GOLDEN / "view.json"`` - so a
    call such as ``response.json()`` is not a mention.
    """
    if path in text:
        return True
    if not path.startswith("tests/"):
        return False
    name = re.escape(path.rsplit("/", 1)[-1])
    return re.search(rf"""(?<=["'/]){name}(?=["'])""", text) is not None


def readers_of(root: Path, targets: list[str]) -> dict[str, list[str]]:
    """Test file -> EVERY target it mentions (in the order of ``targets``)."""
    found: dict[str, list[str]] = {}
    if not targets:
        return found
    for test in sorted((root / "tests").glob("test_*.py")):
        text = test.read_text(encoding="utf-8", errors="replace")
        hits = [t for t in targets if _mentions(text, t)]
        if hits:
            found[str(test.relative_to(root))] = hits
    return found


def select(root: Path, changed: list[str]) -> dict[str, list[str]]:
    """Test file -> reasons, stable order."""
    chosen: dict[str, list[str]] = {}
    for path in changed:
        if _is_test(path) and (root / path).is_file():  # a deleted test: nothing to run
            chosen.setdefault(path, []).append("changed")
    for path in guards(root):
        chosen.setdefault(path, []).append("tree guard")
    others = [p for p in changed if not p.endswith(".py")]
    for test, hits in readers_of(root, others).items():
        chosen.setdefault(test, []).append(f"reads {', '.join(hits)}")
    return dict(sorted(chosen.items()))


def unmapped(root: Path, changed: list[str]) -> list[str]:
    """Changed non-Python paths that no test mentions: the selection does not cover them.

    A deleted path counts too (shown with ``(deleted)``): removing a file that a
    test reads without spelling out its path breaks that test just the same.
    """
    targets = [p for p in changed if not p.endswith(".py")]
    mentioned = {t for hits in readers_of(root, targets).values() for t in hits}
    return [
        p if (root / p).exists() else f"{p} (deleted)"
        for p in targets
        if p not in mentioned
    ]


def pytest_command(files: list[str]) -> list[str]:
    workers = "0" if len(files) <= SMALL_SELECTION else "4"
    return [
        "uv",
        "run",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-n",
        workers,
        *files,
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the tests a diff makes worth running."
    )
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--dry-run", action="store_true", help="print, do not run")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    changed, warning = changed_files(root)
    if warning:
        print(f"test-changed: {warning}", file=sys.stderr)
    chosen = select(root, changed)
    for path, reasons in chosen.items():
        print(f"  {path}  ({', '.join(reasons)})")
    print(f"test-changed: {len(chosen)} files")
    loose = unmapped(root, changed)
    if loose:
        shown = ", ".join(loose[:5]) + (" ..." if len(loose) > 5 else "")
        print(f"test-changed: no test mentions {len(loose)} changed file(s): {shown}")
    if any(p.startswith("hub/") and p.endswith(".py") for p in changed):
        print(
            "test-changed: changed hub code is not followed through imports or symbols; CI decides"
        )
    if not chosen:
        print("test-changed: nothing to run (no changed tests and no guards)")
        return 0
    command = pytest_command(list(chosen))
    if args.dry_run:
        print(" ".join(command))
        return 0
    return subprocess.run(command, cwd=root).returncode


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
