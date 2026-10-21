"""#1933 круг 2: границы тестов AC - из ast файла на закреплённом sha, не из текста ханков."""

from __future__ import annotations

import aiosqlite
from httpx import AsyncClient

from hub.services.review_dispatch import (
    REVIEW_FILE_LINE_CAP,
    _inline_diff_lines,
    diff_plan,
)
from hub.services.review_oversized import UNREADABLE, ac_test_spans
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
    dup = _span(src, f"{F}::test_ac")
    assert dup.unreadable and not dup.found, "повторное определение - неизвестно"
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
    first = [ln for ln in block.splitlines() if ln.startswith("ЧИТАТЬ ПЕРВЫМИ:")]
    assert first == [f"ЧИТАТЬ ПЕРВЫМИ: {UNREADABLE} ({ref})"]


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


def _big_file_diff(path: str, marker: str, size: int) -> tuple[str, str, str]:
    """``(дифф нового файла, его текст, ref теста в конце)`` примерно ``size`` символов."""
    rows = ["# " + "x" * 40] * (size // 43) + ["def test_ac():", f"    assert {marker}"]
    return _new(path, rows), "\n".join(rows) + "\n", f"{path}::test_ac"


def test_the_reserve_for_ac_cutouts_is_kept_when_a_plain_file_could_take_it():
    """Codex 2.1: больше бюджета не должно вытеснять тест AC ранним обычным файлом."""
    plain = _new("hub/plain.py", [f"p{k} = {k}" + " " * 40 for k in range(26)])
    mid, mid_src, mid_ref = _big_file_diff("tests/test_mid.py", "AC_MID", 1000)
    big, big_src, big_ref = _big_file_diff("tests/test_big.py", "AC_BIG", 4400)
    spans = ac_test_spans({"tests/test_mid.py": mid_src, "tests/test_big.py": big_src},
                          [mid_ref, big_ref])  # fmt: skip
    lines, _ = _inline_diff_lines(plain + mid + big, [], None, 1500, spans)
    text = "\n".join(lines)
    assert "AC_MID" in text and "AC_BIG" in text
    assert "ac_test_unread" not in text


def test_a_test_outside_the_hunks_of_a_file_in_the_prompt_gets_a_cutout():
    """Codex 2.2: файл в промте, а тело теста вне ханков - вырезка всё равно нужна."""
    src = (
        "\n".join(
            [f"v{k} = {k}" for k in range(1000)]
            + ["def test_ac():", "    assert OUTSIDE"]
        )
        + "\n"
    )
    diff = (
        f"diff --git a/{F} b/{F}\n--- a/{F}\n+++ b/{F}\n@@ -1,2 +1,2 @@\n"
        "-v0 = 0\n+v0 = 1\n v1 = 1\n"
    )
    spans = ac_test_spans({F: src}, [f"{F}::test_ac"])
    lines, omitted = _inline_diff_lines(diff, [], None, 100_000, spans)
    assert omitted == [] and "OUTSIDE" in "\n".join(lines)


def test_an_unreadable_pinned_diff_still_states_the_block_as_unknown():
    """Codex 2.3: ветка сократилась, закреплённый дифф не прочитан - блок есть, всё неизвестно."""
    ref = f"{F}::test_ac"
    branch = _new(F, ["def test_ac():", "    pass"])
    block, _ = diff_plan(branch, "develop", "b", ac_test_refs=[ref], ac_sources=None,
                         ac_block=True)  # fmt: skip
    assert f"ЧИТАТЬ ПЕРВЫМИ: {UNREADABLE} ({ref})" in block
    assert "НЕ ПОМЕСТЯТСЯ" not in block


async def test_the_pinned_diff_is_read_as_a_commit_object_not_by_name(monkeypatch):
    """Codex 2.5: integrity_read(sha), а не branch_diff, который смотрит origin/<имя>."""
    from hub.integrations.registry import plugins
    from hub.services.review_ac_inputs import pinned_diff as _pinned_diff

    calls = []

    class Git:
        async def integrity_read(self, repo, base, sha):
            calls.append((repo, base, sha))
            return {"diff": "THE DIFF"}

        async def branch_diff(self, *a, **k):  # pragma: no cover - не должен звониться
            raise AssertionError("by name")

    monkeypatch.setattr(plugins, "git_ops", Git())
    assert await _pinned_diff(("/ws", "develop"), "a" * 40) == "THE DIFF"
    assert calls == [("/ws", "develop", "a" * 40)]
    assert await _pinned_diff(None, "a" * 40) is None

    class Broken:
        async def integrity_read(self, *a):
            raise RuntimeError("git down")

    monkeypatch.setattr(plugins, "git_ops", Broken())
    assert await _pinned_diff(("/ws", "develop"), "a" * 40) is None


def test_a_deeply_nested_expression_is_unknown_not_a_crash():
    """Codex 2.6: RecursionError парсера - «не прочитан», заказ не падает."""
    src = "x = " + "+".join(["1"] * 30000) + "\ndef test_ac():\n    pass\n"
    assert _span(src, f"{F}::test_ac").unreadable


def test_nested_class_refs_and_ambiguous_names():
    """Codex 2.7-2.8: вложенный класс находится; условное переопределение - неоднозначно."""
    src = (
        "class Outer:\n    class Inner:\n        def test_ac(self):\n            pass\n"
    )
    assert _span(src, f"{F}::Outer::Inner::test_ac[case]").start == 3
    for cond in (
        "def test_ac():\n    pass\nif True:\n    def test_ac():\n        pass\n",
        "def test_ac():\n    pass\ntest_ac = wrap(test_ac)\n",
        "from x import test_ac\ndef test_ac():\n    pass\n",
    ):
        span = _span(cond, f"{F}::test_ac")
        assert not span.found and span.unreadable, cond


def test_line_numbers_follow_the_parser_not_str_splitlines():
    """\x0c в тексте режет splitlines, но не ast: номера строк не должны уезжать."""
    src = "x = 1\x0c\n\ndef test_ac():\n    assert TARGET\n"
    span = _span(src, f"{F}::test_ac")
    assert (span.start, span.end) == (3, 4) and span.lines[1][
        1
    ].strip() == "assert TARGET"


async def test_an_unread_pin_makes_the_block_mandatory_and_a_missing_pin_does_not(
    db, monkeypatch
):
    from hub.integrations.registry import plugins
    from hub.services.review_ac_inputs import ac_inputs as _ac_inputs

    class Git:
        async def integrity_read(self, repo, base, sha):
            return None

    monkeypatch.setattr(plugins, "git_ops", Git())
    refs = [f"{F}::test_ac"]
    ctx = ("/ws", "develop")
    task = {"id": 1, "submission_sha": "a" * 40}
    assert await _ac_inputs(db, task, ctx, refs, "branch diff", False) == (
        None,
        None,
        True,
    )
    assert await _ac_inputs(db, {"id": 1}, ctx, refs, "x", False) == (None, None, False)
    assert await _ac_inputs(db, task, ctx, [], "x", False) == (None, None, False)


def test_every_label_carries_the_test_ref_in_trailing_parentheses():
    """Находка ревью 2: промт велит копировать test_ref «из скобок блока» - они нужны всегда."""
    import re

    from hub.services.review_oversized import unread_ac_tests

    src = "def test_ac():\n    pass\n"
    refs = [f"{F}::test_ac", f"{F}::test_gone", "web/src/panel.test.ts::still toggles"]
    spans = ac_test_spans({F: src}, refs) + ac_test_spans({F: None}, [f"{F}::test_ac"])
    for span in spans:
        copied = re.search(r"\((.*)\)$", span.label())
        assert copied and copied.group(1) == span.ref, span.label()
        assert unread_ac_tests([f"ac_test_unread: {copied.group(1)}"], [span.ref]) == [
            span.ref
        ]
