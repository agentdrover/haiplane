"""scripts/changed_tests.py picks the tests a diff makes worth running (#2000).

Real git in a temporary repository: the selection reads ``git diff`` against the
merge-base with ``origin/<base>``, the untracked files and the tree-guard ledger,
and a mock of git would only test an idea of git.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import changed_tests  # noqa: E402


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
        },
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def write(root: Path, rel: str, text: str = "x\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_repo(tmp_path: Path, base: str = "develop", remote: bool = True) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", base)
    write(root, "tests/test_a.py")
    write(root, "tests/test_b.py", 'PATH = "data/ledger.json"\n')
    write(root, "tests/test_guard.py")
    write(root, "tests/test_other.py")
    write(root, "tests/test_gone.py")
    write(root, "data/ledger.json", "{}\n")
    write(root, "data/old.json", "{}\n")
    write(root, "tests/fixtures/vectors.json", "{}\n")
    write(root, "tests/golden/view.json", "{}\n")
    write(root, "docs/note.md", "note\n")
    write(
        root,
        changed_tests.GUARDS_LEDGER,
        '{"guards": [{"path": "tests/test_guard.py"}, {"path": "tests/test_nope.py"}]}',
    )
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    if remote:
        bare = tmp_path / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        git(root, "remote", "add", "origin", str(bare))
        git(root, "push", "-q", "origin", base)
    git(root, "checkout", "-q", "-b", "task-1/x")
    return root


def test_selection_by_diff(tmp_path: Path) -> None:
    """AC-1: changed tests, guards and readers of changed non-py files; nothing else."""
    root = make_repo(tmp_path)
    write(root, "tests/test_a.py", "changed\n")  # a changed test
    write(root, "data/ledger.json", '{"k": 1}\n')  # read by tests/test_b.py
    write(root, "tests/test_с пробелом.py")  # new, untracked, space and Cyrillic
    (root / "tests/test_gone.py").unlink()  # deleted: nothing to run
    (root / "data/old.json").unlink()  # deleted, but a test still reads its path
    write(root, "tests/test_reads_old.py", 'PATH = "data/old.json"\n')
    git(root, "mv", "tests/test_other.py", "tests/test_renamed.py")  # delete + add
    write(root, "docs/other.md")  # read by nobody
    write(root, "tests/fixtures/vectors.json", '{"v": 2}\n')  # changed fixture
    write(
        root, "tests/test_fixture_reader.py", 'FILE = "tests/fixtures/vectors.json"\n'
    )
    write(root, "tests/golden/view.json", '{"g": 2}\n')  # changed golden file
    write(root, "tests/test_golden_reader.py", 'NAME = GOLDEN / "view.json"\n')

    changed, warning = changed_tests.changed_files(root)
    assert warning == ""
    assert "data/old.json" in changed  # a deleted path is kept for the readers
    assert "tests/test_gone.py" in changed  # ...and dropped from the run by select
    assert "tests/test_other.py" in changed  # the old name of a rename: deleted
    assert "tests/test_с пробелом.py" in changed
    assert changed == sorted(changed)

    chosen = changed_tests.select(root, changed)
    assert chosen["tests/test_a.py"] == ["changed"]
    assert chosen["tests/test_с пробелом.py"] == ["changed"]
    assert chosen["tests/test_renamed.py"] == ["changed"]
    assert chosen["tests/test_guard.py"] == ["tree guard"]
    assert chosen["tests/test_b.py"] == ["reads data/ledger.json"]
    assert chosen["tests/test_reads_old.py"] == ["changed", "reads data/old.json"]
    # A non-Python file UNDER tests/ is looked for by its path or by its bare name
    # (fixtures are usually joined to a directory constant).
    assert chosen["tests/test_fixture_reader.py"] == [
        "changed",
        "reads tests/fixtures/vectors.json",
    ]
    assert chosen["tests/test_golden_reader.py"] == [
        "changed",
        "reads tests/golden/view.json",
    ]
    assert "tests/test_gone.py" not in chosen
    assert "tests/test_other.py" not in chosen
    assert "tests/test_nope.py" not in chosen  # a ledger entry with no file on disk
    assert list(chosen) == sorted(chosen)

    (tmp_path / "clean").mkdir()
    clean = make_repo(tmp_path / "clean")
    assert changed_tests.select(clean, changed_tests.changed_files(clean)[0]) == {
        "tests/test_guard.py": ["tree guard"]
    }


def test_the_base_branch_comes_from_the_clone_config(tmp_path: Path) -> None:
    """AC-1: ``haiplane.baseBranch`` decides what the diff is against."""
    root = make_repo(tmp_path, base="main")
    git(root, "config", "haiplane.baseBranch", "main")
    write(root, "tests/test_a.py", "changed\n")
    assert changed_tests.base_branch(root) == "main"
    changed, warning = changed_tests.changed_files(root)
    assert warning == ""
    assert changed == ["tests/test_a.py"]


def test_missing_base_falls_back_to_guards_with_a_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-2: no ``origin/<base>`` is not "no changes": a reason, and the guards."""
    root = make_repo(tmp_path, remote=False)
    write(root, "tests/test_with_changes.py")
    changed, warning = changed_tests.changed_files(root)
    assert "origin/develop" in warning
    assert changed == []  # no guess from the working tree: the guards only

    rc = changed_tests.main(["--root", str(root), "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "origin/develop" in captured.err
    assert "tests/test_guard.py" in captured.out
    assert "tests/test_with_changes.py" not in captured.out


def test_n_switches_at_the_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: few files run in-process, many on workers; the exit code is pytest's."""
    small = [f"tests/test_{n}.py" for n in range(changed_tests.SMALL_SELECTION)]
    big = [*small, "tests/test_one_more.py"]
    assert changed_tests.pytest_command(small)[6:8] == ["-n", "0"]
    assert changed_tests.pytest_command(big)[6:8] == ["-n", "4"]
    assert changed_tests.pytest_command(big)[8:] == big

    root = make_repo(tmp_path)
    write(root, "tests/test_a.py", "changed\n")
    calls: list[list[str]] = []

    real_run = subprocess.run

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if command[0] != "uv":  # git keeps working; only pytest is replaced
            return real_run(command, **kwargs)  # type: ignore[call-overload]
        calls.append(command)
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(changed_tests.subprocess, "run", fake_run)
    assert changed_tests.main(["--root", str(root)]) == 7
    assert calls and calls[0][:3] == ["uv", "run", "pytest"]
    assert "tests/test_a.py" in calls[0] and "tests/test_guard.py" in calls[0]


def test_nothing_to_run_without_guards_or_changes(tmp_path: Path) -> None:
    """No changed tests and no ledger: nothing is run and the exit code is 0."""
    root = tmp_path / "plain"
    root.mkdir()
    git(root, "init", "-q", "-b", "develop")
    write(root, "README.md")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    assert changed_tests.main(["--root", str(root), "--dry-run"]) == 0


def test_a_changed_file_that_no_test_mentions_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A selection that covers nothing for a change must not look like a full one."""
    root = make_repo(tmp_path)
    write(root, "docs/other.md", "unmentioned\n")
    (root / "docs/note.md").unlink()  # a deleted file nobody names is reported too
    (root / "hub").mkdir()
    write(root, "hub/code.py", "x = 1\n")
    rc = changed_tests.main(["--root", str(root), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert (
        "no test mentions 2 changed file(s): docs/note.md (deleted), docs/other.md"
        in out
    )
    assert "changed hub code is not followed" in out


def test_one_test_reading_two_changed_files_names_both_and_calls_are_not_reads(
    tmp_path: Path,
) -> None:
    """Every target a test mentions is a reason (and is not reported as unmapped);
    ``response.json()`` is a call, not a mention of a fixture called json."""
    root = make_repo(tmp_path)
    write(root, "tests/fixtures/vectors.json", '{"v": 3}\n')
    write(root, "tests/golden/view.json", '{"g": 3}\n')
    write(root, "tests/test_both.py", 'A = D / "vectors.json"\nB = G / "view.json"\n')
    write(root, "tests/test_noise.py", "x = response.json()\ny = view.json()\n")
    changed, _ = changed_tests.changed_files(root)
    chosen = changed_tests.select(root, changed)
    assert chosen["tests/test_both.py"] == [
        "changed",
        "reads tests/fixtures/vectors.json, tests/golden/view.json",
    ]
    assert chosen["tests/test_noise.py"] == [
        "changed"
    ]  # no "reads": it only calls json()
    assert changed_tests.unmapped(root, changed) == []
