"""Commit-scope path parsing and scope comparison (#361, fixed in #555).

Driven by real ``git status`` output rather than hand-written strings: the
defect these tests exist for was invisible to hand-written input, because the
author writing the fixture writes the path he expects rather than the escaped
form git actually emits.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from hub.commit_scope import foreign_paths, parse_porcelain_paths


def _run_git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def _seed(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git("git", "init", "-q", "-b", "main", ".", cwd=repo)
    _run_git("git", "config", "user.email", "t@example.com", cwd=repo)
    _run_git("git", "config", "user.name", "t", cwd=repo)
    (repo / "docs").mkdir()
    (repo / "docs" / "Тест.md").write_text("исходное\n")
    (repo / "app.py").write_text("v1\n")
    _run_git("git", "add", "-A", cwd=repo)
    _run_git("git", "commit", "-m", "init", cwd=repo)
    return repo


def _status_z(repo: Path) -> str:
    """What dirty_paths feeds the parser, including the strip _run applies."""
    return _run_git("git", "status", "--porcelain", "-z", cwd=repo).strip()


def test_non_ascii_path_in_scope_is_not_foreign(tmp_path: Path) -> None:
    """#555 AC-1. Plain --porcelain escapes the name, so a file the task
    declared compared as somebody else's and sent the task to needs_decision.
    """
    repo = _seed(tmp_path)
    (repo / "docs" / "Тест.md").write_text("правка\n")

    paths = parse_porcelain_paths(_status_z(repo))

    assert paths == ["docs/Тест.md"]
    assert foreign_paths(paths, ["docs/Тест.md"]) == []


def test_non_ascii_foreign_path_still_caught(tmp_path: Path) -> None:
    """#555 AC-2. Fixing the false positive must not soften the gate.

    Inside an already-tracked directory git names the file itself, which is
    the case the gate reasons about.
    """
    repo = _seed(tmp_path)
    (repo / "docs" / "Чужое.md").write_text("не моё\n")

    paths = parse_porcelain_paths(_status_z(repo))

    assert paths == ["docs/Чужое.md"]
    assert foreign_paths(paths, ["docs/Тест.md"]) == ["docs/Чужое.md"]


def test_untracked_directory_is_reported_collapsed_and_still_caught(
    tmp_path: Path,
) -> None:
    """git collapses a wholly untracked directory into a single entry.

    Documented rather than worked around: the gate only has to decide whether
    something outside the declared areas is about to be staged, and the
    collapsed form answers that. Written after the file-level expectation
    failed against real output.
    """
    repo = _seed(tmp_path)
    (repo / "чужое").mkdir()
    (repo / "чужое" / "Файл.md").write_text("не моё\n")

    paths = parse_porcelain_paths(_status_z(repo))

    assert paths == ["чужое/"]
    assert foreign_paths(paths, ["docs"]) == ["чужое/"]


def test_rename_yields_both_sides(tmp_path: Path) -> None:
    """#555 AC-3. -z splits a rename into two records instead of ' -> ', and
    the source has no status field of its own. Both sides land in the commit,
    so both must still be reported.
    """
    repo = _seed(tmp_path)
    _run_git("git", "mv", "docs/Тест.md", "docs/Переименован.md", cwd=repo)

    paths = parse_porcelain_paths(_status_z(repo))

    assert sorted(paths) == ["docs/Переименован.md", "docs/Тест.md"]


def test_untracked_and_modified_are_both_seen(tmp_path: Path) -> None:
    """The ordinary case, kept honest against the same real output."""
    repo = _seed(tmp_path)
    (repo / "app.py").write_text("v2\n")
    (repo / "notes.txt").write_text("new\n")

    paths = parse_porcelain_paths(_status_z(repo))

    assert sorted(paths) == ["app.py", "notes.txt"]
    assert foreign_paths(paths, ["app.py"]) == ["notes.txt"]
