"""The SUBJECT of a review is read as it always was (#1856).

``changed_paths`` and ``file_line_counts`` choose what a reviewer is told to
read and when a file is "too big". #1856 made their parsing robust (a hunk body
is not a header, quoted names, ``diff --cc``) and must not change what they
answer for correct git output. The two functions below are frozen copies of
the ones on develop before the change; real git output is the corpus.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hub.services.review_dispatch import (
    _inline_diff_lines,
    changed_paths,
    file_line_counts,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def frozen_changed_paths(diff: str) -> list[str]:
    paths: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("+++ "):
            continue
        raw = line[4:].strip()
        if not raw or raw == "/dev/null":
            continue
        if raw.startswith("b/"):
            raw = raw[2:]
        if raw and raw not in paths:
            paths.append(raw)
    return paths


def frozen_file_line_counts(diff: str) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    current = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            raw = line[4:].strip()
            path = raw[2:] if raw.startswith("b/") else raw
            current = "" if path == "/dev/null" else path
            counts.setdefault(current, 0)
            continue
        if current and (line.startswith("+") or line.startswith("-")):
            counts[current] += 1
    counts.pop("", None)
    return sorted(counts.items(), key=lambda item: -item[1])


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    root = tmp_path_factory.mktemp("subject")
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

    def g(*a: str) -> str:
        return _git(repo, env, *a)

    def put(name: str, data: str | bytes) -> None:
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())

    g("init", "-q", "-b", "main")
    put("src/old_name.py", "".join(f"line {i}\n" for i in range(30)))
    put("tool.sh", "echo hi\n")
    put("dead.py", "".join(f"gone {i}\n" for i in range(857)))
    put("mod.py", "a\nb\nc\nd\n")
    put("pic.png", b"\x89PNG\x00\x00old")
    put("m.py", "1\n2\n3\n")
    g("add", "-A")
    g("commit", "-q", "-m", "base")
    g("branch", "side")
    out: dict[str, str] = {}

    # each scenario is one commit on top of the previous state
    def commit(label: str, *, rev: str = "HEAD~1..HEAD", extra: tuple[str, ...] = ()):
        g("add", "-A")
        g("commit", "-q", "-m", label)
        out[label] = g("diff", "-M", *extra, "HEAD~1", "HEAD")

    g("mv", "src/old_name.py", "src/new_name.py")
    commit("rename-only")
    (repo / "tool.sh").chmod(0o755)
    commit("mode-only")
    put("pic.png", b"\x89PNG\x00\x00new!")
    commit("binary")
    g("rm", "-q", "dead.py")
    commit("delete")
    put("mod.py", "a\nB\nc\nd\n")
    put("a b/c d.py", "x\ny\n")
    put("sp b/e.py", "z\n")
    commit("modify-and-add")
    # rename-only plus the deletion of ANOTHER file, in one diff
    g("mv", "src/new_name.py", "src/third.py")
    g("rm", "-q", "mod.py")
    commit("rename-and-delete")
    put("big.py", "".join(f"n {i}\n" for i in range(5)))
    put("mod2.py", "q\n")
    g("add", "-A")
    g("commit", "-q", "-m", "more")
    out["several"] = g("diff", "-M", "HEAD~3", "HEAD")
    # a conflict merge shown as a combined diff
    put("m.py", "1\nmain\n3\n")
    g("add", "-A")
    g("commit", "-q", "-m", "main change")
    g("checkout", "-q", "side")
    put("m.py", "1\nside\n3\n")
    g("add", "-A")
    g("commit", "-q", "-m", "side change")
    g("checkout", "-q", "main")
    subprocess.run(
        ["git", "merge", "side", "-m", "merge"], cwd=repo, env=env, capture_output=True
    )
    put("m.py", "1\nresolved\n3\n")
    g("add", "-A")
    g("commit", "-q", "-m", "merge")
    out["merge-cc"] = g("show", "--cc", "HEAD")
    return out


SCENARIOS = [
    "rename-only",
    "mode-only",
    "binary",
    "delete",
    "modify-and-add",
    "rename-and-delete",
    "several",
    "merge-cc",
]


@pytest.mark.parametrize("name", SCENARIOS)
def test_subject_equals_the_frozen_develop_answer(corpus: dict[str, str], name: str):
    diff = corpus[name]
    assert changed_paths(diff) == frozen_changed_paths(diff)
    assert file_line_counts(diff) == frozen_file_line_counts(diff)


@pytest.mark.parametrize(
    "sha",
    ["ac9dd151", "5e226906", "752d4d67"],  # the commits Codex checked
)
def test_subject_of_real_commits_equals_the_frozen_answer(sha: str):
    probe = subprocess.run(
        ["git", "cat-file", "-e", sha], cwd=REPO_ROOT, capture_output=True
    )
    if probe.returncode != 0:
        pytest.skip(f"{sha} is not in this clone")
    for args in (["show", "--format=", sha], ["diff", "-M", f"{sha}^", sha]):
        diff = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, check=True, capture_output=True, text=True
        ).stdout
        if '"' in "".join(ln for ln in diff.splitlines() if ln.startswith("+++ ")):
            continue  # quoted names are decoded now: the one intended difference
        assert changed_paths(diff) == frozen_changed_paths(diff), sha
        assert file_line_counts(diff) == frozen_file_line_counts(diff), sha


def test_rename_only_plus_a_deletion_still_reads_the_whole_diff(corpus: dict[str, str]):
    # Codex 1: with the rename as the only "changed path" the delta narrowed
    # to it and the deletion fell out of the attached diff without a word.
    diff = corpus["rename-and-delete"]
    assert changed_paths(diff) == []
    lines, omitted = _inline_diff_lines(diff, [], changed_paths(diff), 100_000)
    assert omitted == []
    assert diff in "".join(lines)  # rename AND deletion are both attached


def test_a_binary_mode_or_rename_only_delta_has_no_changed_paths(
    corpus: dict[str, str],
):
    # Codex 2: "no changed path" is what makes the delta "code did not change,
    # read the whole diff" instead of a small retrial that drops deep to lite.
    for name in ("binary", "mode-only", "rename-only"):
        assert changed_paths(corpus[name]) == [], name


def test_a_deleted_file_is_not_a_file_over_the_line_ceiling(corpus: dict[str, str]):
    # Codex 3: 857 deleted lines are no subject and cannot demand incomplete=true.
    assert file_line_counts(corpus["delete"]) == []
    assert all(n < 800 for _, n in file_line_counts(corpus["several"]))
