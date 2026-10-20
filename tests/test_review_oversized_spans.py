"""#1933: разбор диффа в review_oversized — края, на которых функция AC теряется или путается."""

from __future__ import annotations

import json

import aiosqlite
from httpx import AsyncClient

from hub.services.review_dispatch import (
    _inline_diff_lines,
    diff_file_sections,
    diff_plan,
)
from hub.services.review_oversized import ac_test_spans, unread_ac_tests
from hub.services.steward_corridor import report_outcome
from tests.review_dispatch_support import _DispatchRecorder, _submitted, _wire

F = "tests/test_x.py"


def _new(path: str, lines: list[str]) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n"
        f"+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


def _span(diff: str, ref: str):
    (span,) = ac_test_spans(diff_file_sections(diff), [ref])
    return span


def test_a_comment_after_the_signature_does_not_swallow_the_next_test():
    diff = _new(
        F,
        ["def test_a():  # AC-1", "    assert 1", "", "def test_b():", "    assert 2"],
    )
    span = _span(diff, f"{F}::test_a")
    assert (span.start, span.end, span.partial) == (1, 2, False)


def test_brackets_inside_strings_do_not_move_the_end_of_the_signature():
    diff = _new(
        F, ['def test_a(value="("):', "    assert 1", "def test_b():", "    pass"]
    )
    assert (_span(diff, f"{F}::test_a").end) == 2


def test_a_one_line_body_ends_on_its_own_line():
    diff = _new(F, ["def test_a(): assert 1", "def test_b():", "    pass"])
    assert _span(diff, f"{F}::test_a").end == 1


def test_a_module_function_is_not_confused_with_a_method_of_the_same_name():
    lines = [
        "class TestOther:",
        "    def test_ac(self):",
        "        assert 'METHOD'",
        "",
        "def test_ac():",
        "    assert 'MODULE'",
    ]
    span = _span(_new(F, lines), f"{F}::test_ac")
    assert (span.start, span.end) == (5, 6)


def test_a_method_of_a_nested_class_is_not_a_method_of_the_outer_one():
    lines = [
        "class TestKit:",
        "    class Inner:",
        "        def test_ac(self):",
        "            pass",
    ]
    assert not _span(_new(F, lines), f"{F}::TestKit::test_ac").found


def test_a_multiline_class_header_does_not_hide_its_method():
    lines = [
        "class TestKit(",
        "    Base,",
        "):",
        "    def test_ac(self):",
        "        assert 1",
    ]
    span = _span(_new(F, lines), f"{F}::TestKit::test_ac")
    assert (span.start, span.end) == (4, 5)


def test_a_parameter_id_with_colons_is_dropped_before_splitting():
    span = _span(_new(F, ["def test_ac(x):", "    pass"]), f"{F}::test_ac[one::two]")
    assert span.found and span.start == 1


def test_decorators_belong_to_the_function():
    lines = [
        "x = 1",
        "@pytest.mark.parametrize(",
        "    'a', [1, 2],",
        ")",
        "@pytest.mark.slow",
        "def test_ac(a):",
        "    assert a",
    ]
    span = _span(_new(F, lines), f"{F}::test_ac")
    assert (span.start, span.end) == (2, 7)


def _modified(hunks: list[tuple[int, list[str]]]) -> str:
    """Дифф существующего файла: ханки ``(начало, строки с префиксом)``."""
    out = f"diff --git a/{F} b/{F}\n--- a/{F}\n+++ b/{F}\n"
    for at, rows in hunks:
        old = sum(1 for r in rows if r[0] != "+")
        new = sum(1 for r in rows if r[0] != "-")
        out += f"@@ -{at},{old} +{at},{new} @@ def test_ac(x):\n"
        out += "".join(f"{r}\n" for r in rows)
    return out


def test_a_function_cut_by_a_gap_between_hunks_is_marked_partial():
    rows_1 = [" def test_ac(x):", "+    assert FIRST", "     pass"]
    rows_2 = ["     pass", "+    assert LAST", "     pass"]
    span = _span(_modified([(10, rows_1), (60, rows_2)]), f"{F}::test_ac")
    assert span.partial and span.start == 10
    assert "в диффе виден не весь тест" in span.label()


def test_a_function_closed_by_a_visible_dedent_is_complete():
    rows = [" def test_ac(x):", "+    assert FIRST", "     pass", " ", " def other():"]
    span = _span(_modified([(10, rows)]), f"{F}::test_ac")
    assert (span.start, span.end, span.partial) == (10, 12, False)


def test_unread_ac_tests_keep_the_order_of_the_task_and_do_not_repeat():
    refs = ["a.py::t1", "a.py::t2"]
    lost = [
        "ac_test_unread: a.py::t2",
        "ac_test_unread:  a.py::t1 ",
        "ac_test_unread: a.py::t2",
    ]
    assert unread_ac_tests(lost, refs) == refs


def test_a_test_outside_the_delta_gets_a_cutout_in_the_local_prompt():
    big = _new(
        F,
        [f"# filler {n}" for n in range(1, 900)]
        + ["def test_ac():", "    assert OUT_OF_DELTA"],
    )
    small = _new("hub/small.py", ["x = 1"])
    ref = f"{F}::test_ac"
    spans = ac_test_spans(diff_file_sections(big + small), [ref])
    lines, _ = _inline_diff_lines(big + small, [], ["hub/small.py"], 5000, spans)
    text = "\n".join(lines)
    assert "OUT_OF_DELTA" in text and "ВЫРЕЗКА ТЕСТА AC" in text
    block, _ = diff_plan(
        big + small, "develop", "b", delta_paths=["hub/small.py"],
        delta_note="дельта", ac_test_refs=[ref], inline_ceiling=5000,
    )  # fmt: skip
    assert "OUT_OF_DELTA" in block


def test_the_cloud_numbers_come_from_the_pinned_diff():
    ref = f"{F}::test_ac"
    moved = _new(
        F,
        ["# moved by one line"]
        + [f"# f{n}" for n in range(900)]
        + ["def test_ac():", "    pass"],
    )
    pinned = _new(F, [f"# f{n}" for n in range(900)] + ["def test_ac():", "    pass"])
    block, _ = diff_plan(moved, "develop", "b", ac_test_refs=[ref], ac_diff=pinned)
    assert f"{F}:901–902 ({ref})" in block, "строки файла на коммите сдачи"
    assert f"{F}:902–903" in diff_plan(moved, "develop", "b", ac_test_refs=[ref])[0]


async def test_an_unread_ac_test_with_incomplete_false_is_a_clean_run_nowhere(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Событие приёма читает тот же body.incomplete, что и хранилище."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-9"}, "run": {"id": "r-9"}})
    _wire(monkeypatch, recorder)
    ref = f"{F}::test_ac"
    task_id = await _submitted(
        client, db, "forced-incomplete",
        acs=[{"id": "AC-1", "given": "g", "when": "w", "then": "t",
              "verifiable_by": "test", "test_ref": ref}],
    )  # fmt: skip
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review", "agent_count": 1, "tokens_spent": 1,
            "model": "m", "raw_count": 1, "findings_confirmed": [],
            "findings_rejected": [], "incomplete": False, "unresolved": [],
            "lost_dimensions": [f"ac_test_unread: {ref}"], "agent": "r",
        },
    )  # fmt: skip
    assert resp.status_code == 200, resp.text
    rows = await db.execute_fetchall(
        "SELECT payload FROM events WHERE kind='machine_review_completed' AND task_id=?",
        (task_id,),
    )
    payload = json.loads(rows[-1][0])
    expected = report_outcome(confirmed=[], unresolved=[], incomplete=True, raw_count=1)
    assert payload["outcome"] == expected
