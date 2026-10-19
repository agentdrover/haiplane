"""Every reader of diff headers names the same files (#1856).

The inputs are REAL git output from a temporary repository: C-quoted and
non-ASCII names, a space, " b/" inside a name, a TAB in a name, rename-only,
binary, deletion, mode change, a generated file, and a merge commit shown with
``git show --cc`` and ``git log -p --cc``. A reader that keeps its own idea of
where a file starts or what its name is fails here, not in production.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hub.services.base_merge import _author_edit
from hub.services.call_sites import changed_line_ranges
from hub.services.ci_integrity import parse_diff
from hub.services.finding_evidence import _parse_patch_log
from hub.services.review_dispatch import (
    _diff_touched_paths,
    author_delta_lines,
    changed_paths,
    diff_file_sections,
    file_line_counts,
    split_generated,
)
from hub.services.task_diff import split_files

# name -> what the change is. Written with git's own quoting in the diff.
LIVE_TEXT = [
    "я /README.md",
    "я /doc.md",
    "d b/f.py",
    "tab\tname.py",
    "mod.py",
]
GENERATED = "node_modules/я b/x.js"
BINARY = "img.bin"
MODE_ONLY = "run.sh"
RENAMED_TO = "new.py"
RENAMED_FROM = "x b/old.py"
DELETED = "gone.py"


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout


def _write(repo: Path, name: str, text: str | bytes) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")


@pytest.fixture(scope="module")
def inputs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    root = tmp_path_factory.mktemp("agree")
    repo = root / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    g = lambda *a: _git(repo, env, *a)  # noqa: E731
    g("init", "-q", "-b", "main")
    _write(repo, RENAMED_FROM, "".join(f"line {i}\n" for i in range(20)))
    _write(repo, MODE_ONLY, "echo hi\n")
    _write(repo, DELETED, "gone\n")
    _write(repo, "mod.py", "a\nb\nc\n")
    _write(repo, "я m b/m.py", "1\n2\n3\n")
    g("add", "-A")
    g("commit", "-q", "-m", "base")
    g("branch", "side")
    # --- the change commit
    g("mv", RENAMED_FROM, RENAMED_TO)
    (repo / MODE_ONLY).chmod(0o755)
    g("rm", "-q", DELETED)
    _write(repo, "mod.py", "a\nB\nc\n")
    for name in LIVE_TEXT[:-1]:
        _write(repo, name, "text\n")
    _write(repo, GENERATED, "x\n")
    _write(repo, BINARY, b"\x00\x01\x02binary\x00")
    _write(repo, "я m b/m.py", "1\nmain\n3\n")
    g("add", "-A")
    g("commit", "-q", "-m", "change")
    plain = g("diff", "HEAD~1", "HEAD")
    numstat = g("diff", "--numstat", "HEAD~1", "HEAD")
    # --- a merge with a hand-resolved conflict, shown as a combined diff
    g("checkout", "-q", "side")
    _write(repo, "я m b/m.py", "1\nside\n3\n")
    g("add", "-A")
    g("commit", "-q", "-m", "side change")
    g("checkout", "-q", "main")
    subprocess.run(
        ["git", "merge", "side", "-m", "merge"], cwd=repo, env=env, capture_output=True
    )
    _write(repo, "я m b/m.py", "1\nresolved\n3\n")
    g("add", "-A")
    g("commit", "-q", "-m", "merge")
    show = g("show", "--cc", "HEAD")
    log = g("log", "-p", "--cc", "-1", "--format=%x1e%H%x09%s", "HEAD")
    return {
        "numstat": numstat,
        "plain": plain,
        "show-cc": show,
        "log-cc": log,
        "plain+show-cc": plain + show,
    }


EXPECTED = {
    "plain": {*LIVE_TEXT, GENERATED, BINARY, MODE_ONLY, RENAMED_TO, "я m b/m.py"},
    "show-cc": {"я m b/m.py"},
    "log-cc": {"я m b/m.py"},
}
EXPECTED["plain+show-cc"] = EXPECTED["plain"] | EXPECTED["show-cc"]
# the plain file list is the same text under another name
PLAIN_ONLY_DELETED = {"plain": {DELETED}, "plain+show-cc": {DELETED}}


def _live(kind: str) -> set[str]:
    return EXPECTED[kind]


def _with_deleted(kind: str) -> set[str]:
    return EXPECTED[kind] | PLAIN_ONLY_DELETED.get(kind, set())


@pytest.mark.parametrize("kind", ["plain", "show-cc", "log-cc", "plain+show-cc"])
def test_every_reader_names_the_same_files(inputs: dict[str, str], kind: str):
    diff = inputs[kind]
    live, everything = _live(kind), _with_deleted(kind)
    # The SUBJECT of a review is the files that carry a "+++" header: no deleted
    # file, no rename without edits, no binary file, no mode change (as always).
    subject = live - {BINARY, MODE_ONLY, RENAMED_TO}

    assert set(changed_paths(diff)) == subject, "changed_paths"
    assert {p for p, _ in file_line_counts(diff)} == subject, "file_line_counts"
    assert {k for k, _ in diff_file_sections(diff) if k} == everything, "sections"
    # one section per file; the commit header of `git show` is a section of its own
    sections = diff_file_sections(diff)
    assert (
        len(sections)
        == {
            "plain": len(everything),
            "show-cc": 2,
            "log-cc": 2,
            "plain+show-cc": len(everything) + 1,
        }[kind]
    ), "diff_file_sections"
    assert "".join(text for _, text in sections) == diff
    # only text BEFORE the first file (a commit header) may be left without a key
    assert "" not in [k for k, _ in diff_file_sections(diff)][1:]
    assert {e["path"] for e in split_files(diff) if e["path"] != "(diff preamble)"} == (
        everything
    ), "task_diff.split_files"
    assert {f.new for f in parse_diff(diff)} == everything, "ci_integrity.parse_diff"
    touched = {p for p in _diff_touched_paths(diff) if p}
    assert everything <= touched, "_diff_touched_paths"
    if kind != "plain+show-cc":  # --git and --cc spell the same file's key differently
        keys = {k for k in _author_edit(diff) if k}  # "" holds the commit header
        assert len(keys) == len(everything), "base_merge._author_edit"
    # numbers of real git: --numstat for the plain diff, three lines in the merge
    stat = [ln.split("\t") for ln in inputs["numstat"].splitlines()]
    plain_lines = sum(
        int(a) + int(d)
        for a, d, name in stat
        if a != "-" and "node_modules" not in name
    )
    merge_lines = 3  # "- main", " -side", "++resolved"
    expected_lines = {
        "plain": plain_lines,
        "show-cc": merge_lines,
        "log-cc": merge_lines,
        "plain+show-cc": plain_lines + merge_lines,
    }[kind]
    assert author_delta_lines(diff) == expected_lines, "author_delta_lines"
    # -U0-style ranges name files that have text hunks and are not deleted
    ranged = set(changed_line_ranges(diff))
    assert ranged <= live and ranged >= (live - {BINARY, MODE_ONLY, RENAMED_TO}), (
        "changed_line_ranges"
    )
    if kind in ("plain", "plain+show-cc"):
        assert split_generated(diff)[1] == [GENERATED], "split_generated"


def test_patch_log_names_the_merge_file(inputs: dict[str, str]):
    parsed = _parse_patch_log(inputs["log-cc"])
    (sha,) = parsed.files
    assert parsed.files[sha] == {"я m b/m.py"}
    assert [h.path for h in parsed.hunks[sha]] == ["я m b/m.py"]


def test_a_combined_section_without_plus_lines_is_keyed_by_its_diff_line():
    # A binary conflict resolution is named only by "diff --cc P" (7efb59b1).
    for head in ("diff --cc hub/x.bin", "diff --combined hub/x.bin"):
        ((key, _),) = diff_file_sections(f"{head}\nindex 1,2..3\nBinary files differ\n")
        assert key == "hub/x.bin", head
    ((key, _),) = diff_file_sections('diff --cc "\\321\\217 /x.bin"\nindex 1,2..3\n')
    assert key == "\u044f /x.bin"
