"""Every reader of diff headers goes through the hunk counters (#1856)."""

from __future__ import annotations

from hub.services.call_sites import changed_line_ranges
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
