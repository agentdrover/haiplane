"""Every reader of diff headers goes through the hunk counters (#1856)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from hub.services.base_merge import author_edit_same
from hub.services.call_sites import changed_line_ranges
from hub.services.ci_integrity import parse_diff
from hub.services.diff_scan import diff_lines
from hub.services.review_dispatch import (
    _inline_diff_lines,
    diff_file_sections,
    author_delta_lines,
    _header_paths,
    changed_paths,
    process_surface_reasons,
    split_generated,
)
from hub.services.finding_evidence import _parse_patch_log
from hub.services.review_evidence import diff_size
from hub.services.task_diff import split_files

_SUBPROCESS = ["запуск подпроцессов: subprocess.run"]

# A hunk whose first added line reads "+++ b/evil.py" (text "++ b/evil.py").
_LOOKALIKE = (
    "diff --git a/hub/x.py b/hub/x.py\n--- a/hub/x.py\n+++ b/hub/x.py\n"
    "@@ -0,0 +1,3 @@\n+++ b/evil.py\n+x\n+y\n"
)


def test_task_diff_counts_the_lookalike_as_an_added_line():
    (entry,) = split_files(_LOOKALIKE)
    assert entry["path"] == "hub/x.py" and entry["added"] == 3


def test_diff_size_counts_the_lookalike_as_a_line_not_a_file():
    files, lines = diff_size(_LOOKALIKE)
    assert files == {"hub/x.py"} and lines == 3


def test_changed_line_ranges_ignore_the_lookalike():
    assert changed_line_ranges(_LOOKALIKE) == {"hub/x.py": {1, 2, 3}}
    # -U0: a second hunk after the lookalike still belongs to hub/x.py.
    two = (
        "diff --git a/hub/x.py b/hub/x.py\n--- a/hub/x.py\n+++ b/hub/x.py\n"
        "@@ -0,0 +1 @@\n+++ b/evil.py\n@@ -5,0 +6 @@\n+z\n"
    )
    assert changed_line_ranges(two) == {"hub/x.py": {1, 6}}


def test_patch_log_keeps_the_path_after_a_lookalike_in_the_body():
    log = "abc123\tsubject\n" + _LOOKALIKE + "\x1e"
    parsed = _parse_patch_log(log)
    assert [(h.path, h.new_start, h.new_count) for h in parsed.hunks["abc123"]] == [
        ("hub/x.py", 1, 3)
    ]
    assert parsed.files["abc123"] == {"hub/x.py"}


# --- #1856: git ends a line at LF only; a VT inside a comment is not a newline.

_FAKE_HEADER = "#\x0bdiff --git a/fake.py b/fake.py"


def _one_file_diff(path: str, body: str, count: int) -> str:
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -0,0 +1,{count} @@\n{body}"
    )


def test_a_vt_in_a_comment_does_not_hide_an_author_edit_from_the_merge_check():
    # The second added line reads "+++ subprocess.call(...)" in the diff.
    def diff(arg: str) -> str:
        body = f"+{_FAKE_HEADER}\n+++ subprocess.call({arg})\n"
        return _one_file_diff("hub/x.py", body, 2)

    same, _ = author_edit_same(diff('"old"'), diff('"new"'))
    assert same is False
    assert author_edit_same(diff('"old"'), diff('"old"'))[0] is True


def test_a_vt_does_not_turn_generated_lines_into_one_for_the_delta():
    body = f"+{_FAKE_HEADER.replace('fake.py', 'uv.lock')}\n" + "+y\n" * 200
    assert author_delta_lines(_one_file_diff("hub/x.py", body, 201)) == 201


def test_a_vt_does_not_make_the_integrity_parser_lose_the_next_line():
    body = f"+{_FAKE_HEADER}\n+os._exit(0)\n"
    (entry,) = parse_diff(_one_file_diff("hub/x.py", body, 2))
    assert [text for _, text in entry.added][-1] == "os._exit(0)"


def test_a_quoted_path_reaches_the_attached_diff_on_a_resubmission():
    quoted = '"hub/\\321\\217.py"'
    diff = (
        f'diff --git "a/hub/\\321\\217.py" "b/hub/\\321\\217.py"\n--- {quoted[:1]}a/{quoted[1:]}\n'
        f'+++ "b/{quoted[1:]}\n@@ -0,0 +1 @@\n+marker\n'
    )
    assert changed_paths(diff) == ["hub/\u044f.py"]
    lines, omitted = _inline_diff_lines(diff, [], changed_paths(diff), 100_000)
    assert omitted == [] and "+marker" in "".join(lines)
    # The text of the section is the diff as git printed it.
    assert diff in "".join(lines)


def test_diff_lines_are_public_and_lf_only():
    assert diff_lines("a\x0bb\nc") == ["a\x0bb\n", "c"]


def test_a_vt_does_not_split_a_section_of_the_attached_diff():
    diff = _one_file_diff("hub/x.py", f"+{_FAKE_HEADER}\n+os._exit(0)\n", 2)
    ((path, text),) = diff_file_sections(diff)
    assert path == "hub/x.py" and text == diff


def test_a_quoted_path_names_a_section_that_has_no_plus_header():
    binary = (
        'diff --git "a/img/\\321\\217.png" "b/img/\\321\\217.png"\n'
        "index 1..2 100644\nBinary files differ\n"
    )
    ((path, _),) = diff_file_sections(binary)
    assert path == "img/\u044f.png"


_NO_PLUS_HEADER = {
    "new.py": (
        "diff --git a/old.py b/new.py\nsimilarity index 100%\n"
        "rename from old.py\nrename to new.py\n"
    ),
    "img.png": (
        "diff --git a/img.png b/img.png\nindex 1..2 100644\n"
        "Binary files a/img.png and b/img.png differ\n"
    ),
    "gone.py": (
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\nindex 1..0\n"
        "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    ),
    "run.sh": "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n",
}


def test_sections_without_a_plus_header_are_attached_on_a_resubmission():
    # rename-only, binary, deletion and mode change carry no "+++ b/<path>" that
    # could repair the key: the key must be clean from the "diff --git" line.
    for path, diff in _NO_PLUS_HEADER.items():
        ((key, text),) = diff_file_sections(diff)
        assert key == path and text == diff
        lines, omitted = _inline_diff_lines(diff, [], [path], 100_000)
        assert omitted == [] and diff in "".join(lines), path
    # And CRLF endings leave no CR in the key either.
    crlf = _NO_PLUS_HEADER["run.sh"].replace("\n", "\r\n")
    assert diff_file_sections(crlf)[0][0] == "run.sh"


def test_a_name_with_b_slash_inside_keeps_its_section_on_a_resubmission():
    # " b/" inside a name must not move the cut of the "diff --git" line.
    rename = (
        "diff --git a/x b/old.py b/new.py\nsimilarity index 100%\n"
        "rename from x b/old.py\nrename to new.py\n"
    )
    rename_edit = (
        "diff --git a/x b/old.py b/new.py\nsimilarity index 90%\n"
        "rename from x b/old.py\nrename to new.py\nindex 1..2 100644\n"
        "--- a/x b/old.py\n+++ b/new.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    same_name = {
        "d b/f.py": "diff --git a/d b/f.py b/d b/f.py\nold mode 100644\nnew mode 100755\n",
        "d b/g.png": (
            "diff --git a/d b/g.png b/d b/g.png\nindex 1..2 100644\n"
            "Binary files a/d b/g.png and b/d b/g.png differ\n"
        ),
        "d b/\u044f.py": (
            'diff --git "a/d b/\\321\\217.py" "b/d b/\\321\\217.py"\n'
            "old mode 100644\nnew mode 100755\n"
        ),
    }
    cases = {"new.py": rename, **same_name}
    cases_edit = {"new.py": rename_edit}
    for path, diff in [*cases.items(), *cases_edit.items()]:
        ((key, text),) = diff_file_sections(diff)
        assert key == path and text == diff, path
        lines, omitted = _inline_diff_lines(diff, [], [path], 100_000)
        assert omitted == [] and diff in "".join(lines), path


def test_a_section_whose_key_cannot_be_read_is_attached_not_dropped():
    odd = "diff --git a/p b/q\nsome future header line\n"
    ((key, _),) = diff_file_sections(odd)
    assert key == ""
    lines, omitted = _inline_diff_lines(odd, [], ["elsewhere.py"], 100_000)
    assert omitted == [] and odd in "".join(lines)


def test_a_quoted_name_keeps_its_trailing_tab_in_the_section_key():
    # git quotes a name with a TAB; the decoded name ends in it and no strip()
    # may take it off, or delta_paths (spelled with the TAB) never match.
    rename = (
        'diff --git "a/old.py\\t" "b/new.py\\t"\nsimilarity index 100%\n'
        'rename from "old.py\\t"\nrename to "new.py\\t"\n'
    )
    plus = (
        'diff --git "a/new.py\\t" "b/new.py\\t"\nindex 1..2 100644\n'
        '--- "a/new.py\\t"\n+++ "b/new.py\\t"\n@@ -1 +1 @@\n-a\n+b\n'
    )
    for diff in (rename, plus):
        ((key, text),) = diff_file_sections(diff)
        assert key == "new.py\t" and text == diff
        lines, omitted = _inline_diff_lines(diff, [], ["new.py\t"], 100_000)
        assert omitted == [] and diff in "".join(lines)
    assert changed_paths(plus) == ["new.py\t"]
    # An unquoted name keeps a trailing space; only the line ending goes.
    spaced = "diff --git a/o b/n \nrename from o\nrename to n \n"
    assert diff_file_sections(spaced)[0][0] == "n "


def test_every_reader_keys_a_path_the_same_way():
    # ONE key rule: quoted names decode as they are, unquoted ones lose only
    # the line ending (and git's service TAB on ---/+++), spaces stay.
    # (name as written in the header, expected key, git's TAB after the name)
    cases = [
        ("new.py ", "new.py ", "\t"),
        ('"new.py\\t"', "new.py\t", ""),
        ('"\\321\\217.py"', "\u044f.py", ""),
        ("d b/f.py", "d b/f.py", "\t"),
        ("node_modules/x b/y.js", "node_modules/x b/y.js", "\t"),
    ]
    for written, key, tab in cases:
        quoted = written.startswith('"')
        a = '"a/' + written[1:] if quoted else "a/" + written
        b = '"b/' + written[1:] if quoted else "b/" + written
        plus = (
            f"diff --git {a} {b}\nindex 1..2 100644\n--- {a}{tab}\n+++ {b}{tab}\n"
            "@@ -1 +1 @@\n-a\n+b\n"
        )
        same = f"diff --git {a} {b}\nold mode 100644\nnew mode 100755\n"
        rename = f"diff --git a/o {b}\nrename from o\nrename to {written}\n"
        assert changed_paths(plus) == [key], written
        assert diff_file_sections(plus)[0][0] == key, written
        assert diff_file_sections(same)[0][0] == key, written
        assert diff_file_sections(rename)[0][0] == key, written
        assert _header_paths(f"rename to {written}") == [key], written
        if key.startswith("node_modules/"):
            assert split_generated(plus)[1] == [key], written


def test_a_lone_plus_header_after_a_hunk_is_code_not_a_new_file():
    # Counters say 1 line, the body has more: "+++ README.md" is NOT a header
    # (no "--- " before it), so nothing after it is trusted or cut.
    for name in ("README.md", "uv.lock"):
        diff = (
            "--- a/hub/x.py\n+++ b/hub/x.py\n@@ -0,0 +1 @@\n+x\n"
            f"+++ {name}\n+subprocess.run(1)\n"
        )
        assert process_surface_reasons(diff) == _SUBPROCESS, name
        assert split_generated(diff) == (diff, []), name
    # A removed line that reads "--- x" is body, not half of a header pair.
    tricky = "--- a/hub/x.py\n+++ b/hub/x.py\n@@ -1 +0,0 @@\n--- x\n+++ README.md\n+subprocess.run(1)\n"
    assert process_surface_reasons(tricky) == _SUBPROCESS
    # A real pair after a hunk still opens a new file.
    pair = "--- a/hub/x.py\n+++ b/hub/x.py\n@@ -0,0 +1 @@\n+x\n--- a/uv.lock\n+++ b/uv.lock\n@@ -0,0 +1 @@\n+y\n"
    assert split_generated(pair)[1] == ["uv.lock"]


def test_real_git_output_for_a_quoted_name_with_a_space(tmp_path: Path):
    # git puts a service TAB after a name with a space even when it is quoted:
    #   +++ "b/\321\217 /README.md"<TAB>
    # Generated by a real git so the test follows git, not our idea of it.
    repo = tmp_path / "repo"
    (repo / "\u044f ").mkdir(parents=True)
    (repo / "\u044f " / "README.md").write_text("git worktree add\n")
    (repo / "\u044f " / "uv.lock").write_text("x\n")
    env = {**os.environ, "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"}
    for args in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True)
    diff = subprocess.run(
        ["git", "diff", "--cached"],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert '+++ "b/\\321\\217 /README.md"\t\n' in diff
    assert changed_paths(diff) == ["\u044f /README.md", "\u044f /uv.lock"]
    assert [k for k, _ in diff_file_sections(diff)] == [
        "\u044f /README.md",
        "\u044f /uv.lock",
    ]
    assert process_surface_reasons(diff) == []  # a document and a lock: no surface
    assert split_generated(diff)[1] == ["\u044f /uv.lock"]
