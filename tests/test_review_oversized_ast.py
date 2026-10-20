"""#1933 круг 2: границы тестов AC - из ast файла на закреплённом sha, не из текста ханков."""

from __future__ import annotations

import aiosqlite
from httpx import AsyncClient

from hub.services.review_dispatch import (
    REVIEW_FILE_LINE_CAP,
    _inline_diff_lines,
    diff_plan,
)
from hub.services.review_oversized import ac_test_spans
from tests.review_dispatch_support import _DispatchRecorder, _submitted, _wire

F = "tests/test_x.py"


def _span(source: str | None, ref: str):
    (span,) = ac_test_spans({F: source}, [ref])
    return span


def _new(path: str, lines: list[str]) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n"
        f"+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


def test_literals_and_nesting_do_not_move_the_ranges():
    """F3: литерал без отступа, 'def test_ac' в строке, кавычка в заголовке."""
    src = (
        "def test_ac():\n"
        '    text = """\n'
        "def test_ac():\n"
        "not indented at all\n"
        '"""\n'
        "    assert REAL\n"
        "\n"
        'def test_other(x="\\""):\n'
        "    pass\n"
    )
    ac, other = _span(src, f"{F}::test_ac"), _span(src, f"{F}::test_other")
    assert (ac.start, ac.end) == (1, 6)
    assert (other.start, other.end) == (8, 9)


def test_owner_and_boundaries_come_from_the_tree():
    """F4: метод своего класса; имя-префикс; вложенный def; повторное определение."""
    src = (
        "class TestOther:\n"
        "    def test_ac(self):\n"
        "        pass\n"
        "class TestKit:\n"
        "    def test_ac_other(self):\n"
        "        def test_ac():\n"
        "            pass\n"
        "    def test_ac(self):\n"
        "        assert KIT\n"
        "def test_ac_other():\n"
        "    pass\n"
        "def test_ac():\n"
        "    pass\n"
        "def test_ac():\n"
        "    assert LAST\n"
    )
    assert (_span(src, f"{F}::TestKit::test_ac").start) == 8
    assert (_span(src, f"{F}::TestOther::test_ac").start) == 2
    assert (_span(src, f"{F}::test_ac").start) == 14, "последнее определение"
    assert not _span(src, f"{F}::TestKit::test_gone").found
    assert not _span(
        "class A:\n    class B:\n        def t(self): pass\n", f"{F}::A::t"
    ).found


def test_decorators_and_async_belong_to_the_function():
    src = "@a\n@pytest.mark.parametrize(\n    'x', [1],\n)\nasync def test_ac(x):\n    pass\n"
    span = _span(src, f"{F}::test_ac[1]")
    assert (span.start, span.end) == (1, 6)


def test_an_unreadable_or_broken_file_is_unknown_not_a_range():
    for src in (None, "def test_ac(:\n"):
        span = _span(src, f"{F}::test_ac")
        assert not span.found and span.unreadable
        assert "не прочитан" in span.label()
    (gone,) = ac_test_spans({F: "x = 1\n"}, [f"{F}::test_ac"])
    assert not gone.unreadable and "не найден в диффе" in gone.label()
    assert ac_test_spans(None, [f"{F}::t"])[0].unreadable


def _big(n: int = REVIEW_FILE_LINE_CAP + 50) -> list[str]:
    return [f"# filler {k}" for k in range(n)] + [
        "def test_ac():",
        "    assert AC_BODY",
    ]


def test_oversized_is_counted_on_the_pinned_diff_and_ranges_from_its_files():
    """F5: ветка сдвинулась и сократилась - oversized и номера всё равно по sha."""
    ref = f"{F}::test_ac"
    pinned = _new(F, _big())
    shrunk = _new(F, ["def test_ac():", "    pass"])  # дифф текущей ветки
    src = {F: "\n".join(_big()) + "\n"}
    block, _ = diff_plan(shrunk, "develop", "b", ac_test_refs=[ref], ac_sources=src,
                         pinned_diff=pinned)  # fmt: skip
    n = REVIEW_FILE_LINE_CAP + 50
    assert f"ЧИТАТЬ ПЕРВЫМИ: {F}:{n + 1}–{n + 2} ({ref})" in block


def test_an_unreadable_pinned_file_is_not_a_range_from_the_branch():
    ref = f"{F}::test_ac"
    block, _ = diff_plan(_new(F, _big()), "develop", "b", ac_test_refs=[ref],
                         ac_sources={F: None})  # fmt: skip
    first = [ln for ln in block.splitlines() if ln.startswith("ЧИТАТЬ ПЕРВЫМИ")]
    assert first == [
        f"ЧИТАТЬ ПЕРВЫМИ: {ref} — не найден: файл на закреплённом коммите не прочитан"
    ]


def test_ac_cutouts_get_their_budget_before_the_other_files():
    """F2: следующий файл влезает целиком и съедал бы остаток - бронь это исключает."""
    ref = f"{F}::test_ac"
    src_lines = ["# " + "x" * 60] * 300 + ["def test_ac():", "    assert AC_BODY"]
    other = _new("hub/other.py", [f"o{k} = {k}" + " " * 40 for k in range(120)])
    diff = _new(F, src_lines) + other
    ceiling = len(other) + 120  # other влезает почти впритык, вырезке остаётся мало
    spans = ac_test_spans({F: "\n".join(src_lines) + "\n"}, [ref])
    lines, omitted = _inline_diff_lines(diff, [], None, ceiling, spans)
    text = "\n".join(lines)
    assert "AC_BODY" in text, "assert теста AC попал в промт"
    assert F in omitted


async def test_a_foreign_ref_does_not_raise_incomplete_or_buy_a_top_up(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """F1: чужой ref в lost_dimensions не меняет отчёт и не заказывает добор."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-7"}, "run": {"id": "r-7"}})
    _wire(monkeypatch, recorder)
    ref = f"{F}::test_ac"
    task_id = await _submitted(
        client, db, "foreign-ref",
        acs=[{"id": "AC-1", "given": "g", "when": "w", "then": "t",
              "verifiable_by": "test", "test_ref": ref}],
    )  # fmt: skip
    calls = len(recorder.calls)
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review", "agent_count": 1, "tokens_spent": 1,
            "model": "m", "raw_count": 1, "findings_confirmed": [],
            "findings_rejected": [], "incomplete": False, "unresolved": [],
            "lost_dimensions": ["ac_test_unread: tests/foreign.py::test_unknown"],
            "agent": "r",
        },
    )  # fmt: skip
    assert resp.status_code == 200, resp.text
    assert resp.json()["incomplete"] is False
    assert len(recorder.calls) == calls, "добор лестницы не заказан"


async def test_a_truncated_missing_or_failing_read_is_unknown():
    from hub.services.review_oversized import read_sources

    class Git:
        async def read_file_at_ref(self, repo, ref, path, *, limit_chars=0):
            if path == "boom.py":
                raise RuntimeError("git down")
            return {
                "ok.py": {"state": "present", "content": "x = 1\n", "truncated": False},
                "cut.py": {"state": "present", "content": "x = 1", "truncated": True},
            }.get(path, {"state": "missing", "content": ""})

    refs = [f"{p}::test_a" for p in ("ok.py", "cut.py", "gone.py", "boom.py")]
    got = await read_sources(Git(), "/repo", "a" * 40, refs)
    assert got == {"ok.py": "x = 1\n", "cut.py": None, "gone.py": None, "boom.py": None}
    assert await read_sources(Git(), "/repo", "", refs) is None
    assert await read_sources(Git(), None, "a" * 40, refs) is None
