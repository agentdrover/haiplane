"""classify_conflict treats whole-file add/add as tail_additions (#1233).

Empty ancestor plus nothing after >>>>>>> is also how git records two sides
adding the same new path. The automerge class then concatenates both complete
files; a green validation would push that blob.
"""

from __future__ import annotations

from hub.services import base_merge

# diff3 add/add of a new path: no shared prefix, empty ancestor, conflict is
# the entire file. Not the tail-append class (common body, then both tails).
_WHOLE_FILE_ADD_ADD = (
    "<<<<<<< HEAD\n"
    "def from_ours():\n"
    "    return 1\n"
    "||||||| merged common ancestors\n"
    "=======\n"
    "def from_theirs():\n"
    "    return 2\n"
    ">>>>>>> origin/develop\n"
)


def test_whole_file_add_add_is_not_tail_additions():
    kind, why = base_merge.classify_conflict(_WHOLE_FILE_ADD_ADD)
    assert kind == base_merge.UNRESOLVABLE, (
        f"whole-file add/add classified as {kind}: concatenating both complete "
        f"files is not tail_additions ({why})"
    )
    assert base_merge.resolve_tail_additions(_WHOLE_FILE_ADD_ADD) is None
    resolutions, _ = base_merge.plan_resolution({"new_module.py": _WHOLE_FILE_ADD_ADD})
    assert resolutions == {}, "green validation must not push concatenated add/add files"
