"""Every reader of diff headers goes through the hunk counters (#1856)."""

from __future__ import annotations

from hub.services.base_merge import author_edit_same
from hub.services.call_sites import changed_line_ranges
from hub.services.ci_integrity import parse_diff
from hub.services.diff_scan import diff_lines
from hub.services.review_dispatch import (
    _inline_diff_lines,
    diff_file_sections,
    author_delta_lines,
    changed_paths,
)
from hub.services.finding_evidence import _parse_patch_log
from hub.services.review_evidence import diff_size
from hub.services.task_diff import split_files

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
