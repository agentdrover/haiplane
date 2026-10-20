"""#1933 круг 3: белый список привязок, предел обхода, общий выбор представления, sha в блоке."""

from __future__ import annotations

import pytest

from hub.services.review_dispatch import _inline_diff_lines, diff_plan
from hub.services.review_oversized import UNREADABLE, ac_test_spans

F = "tests/test_x.py"
REF = f"{F}::test_ac"
SHA = "a" * 40


def _span(src: str, ref: str = REF):
    (span,) = ac_test_spans({F: src}, [ref])
    return span


def _new(path: str, lines: list[str]) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n"
        f"+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


_GOOD = "def test_ac():\n    assert 1\n"


def test_a_deep_elif_chain_is_unknown_or_scanned_never_a_crash():
    """F1: 1100 elif - обход итеративный; привязка в самом хвосте цепочки видна."""
    chain = "if x == 0:\n    pass\n" + "".join(
        f"elif x == {i}:\n    pass\n" for i in range(1, 1100)
    )
    quiet = _span(chain + _GOOD)
    assert quiet.found and quiet.unreadable is False
    rebound = _span(chain + "elif x == 5000:\n    test_ac = 1\n" + _GOOD)
    assert not rebound.found and rebound.unreadable


def test_a_file_over_the_node_limit_is_unknown():
    assert _span("x = [" + ",".join(["1"] * 400_000) + "]\n" + _GOOD).unreadable


@pytest.mark.parametrize(
    "src",
    [
        "class TestOld:\n    def test_ac(self):\n        pass\nTestOld = Replacement\n",
        "def TestOld():\n    pass\nclass TestOld:\n    def test_ac(self):\n        pass\n",
        "class TestOld:\n    def test_ac(self):\n        pass\n"
        "match cmd:\n    case TestOld:\n        pass\n",
        "class TestOld:\n    def test_ac(self):\n        pass\n"
        "match cmd:\n    case 1:\n        class TestOld:\n            pass\n",
    ],
)
def test_a_class_name_rebound_in_any_way_is_unknown(src: str):
    span = _span(src, f"{F}::TestOld::test_ac")
    assert not span.found and span.unreadable


@pytest.mark.parametrize(
    "binding",
    [
        "def test_ac():\n    pass\nclass test_ac:\n    pass\n",
        "def test_ac():\n    pass\nfor test_ac in items:\n    pass\n",
        "def test_ac():\n    pass\nwith ctx() as test_ac:\n    pass\n",
        "def test_ac():\n    pass\ntry:\n    pass\nexcept E as test_ac:\n    pass\n",
        "def test_ac():\n    pass\nmatch v:\n    case [test_ac, *_]:\n        pass\n",
        "def test_ac():\n    pass\n(test_ac := 5)\n",
        "def test_ac():\n    pass\ndel test_ac\n",
        "def test_ac():\n    pass\nfrom x import *\n",
        "def test_ac():\n    pass\nimport test_ac.sub\n",
        "def test_ac():\n    pass\ndef test_ac():\n    pass\n",
        "def test_ac():\n    pass\nglobal test_ac\n",
    ],
)
def test_a_function_name_rebound_in_any_way_is_unknown(binding: str):
    span = _span(binding)
    assert not span.found and span.unreadable, binding


def test_only_the_plain_cases_are_ranges():
    plain = "class TestKit:\n    def test_ac(self):\n        assert 1\n"
    assert _span(plain, f"{F}::TestKit::test_ac").start == 2
    assert _span("import os\nx = 1\n" + _GOOD).start == 3
    nested_only = "if True:\n    def test_ac():\n        pass\n"
    assert _span(nested_only).unreadable


def test_the_whole_procedure_is_one_safe_step(monkeypatch):
    """F1: любой сбой внутри определения диапазона - «не прочитан», не исключение."""
    import hub.services.review_oversized as mod

    def boom(*a, **k):
        raise RecursionError("deep")

    monkeypatch.setattr(mod, "_resolve", boom)
    span = _span(_GOOD)
    assert span.unreadable and not span.found


def test_a_full_diff_that_fits_beats_a_cutout_that_does_not():
    """F2: полный дифф файла влезает, вырезка (с номерами строк) дороже - файл не теряется."""
    rows = [f"    assert BODY_{k:04d}_{'x' * 40}" for k in range(300)]
    lines = ["def test_ac():", *rows]
    diff = _new(F, lines)
    spans = ac_test_spans({F: "\n".join(lines) + "\n"}, [REF])
    from hub.services.review_oversized import cutout

    full, cut = len(diff) + 1, len(cutout(spans[0]))
    assert full < cut
    out, omitted = _inline_diff_lines(diff, [], None, (full + cut) // 2, spans)
    text = "\n".join(out)
    assert omitted == [] and "BODY_0299" in text
    assert "ac_test_unread" not in text and "ВЫРЕЗКА" not in text


def test_the_block_names_the_pinned_sha_and_asks_to_check_head():
    big = _new(F, [f"# f{k}" for k in range(900)] + _GOOD.splitlines())
    src = {F: "\n".join([f"# f{k}" for k in range(900)] + _GOOD.splitlines()) + "\n"}
    cloud, _ = diff_plan(big, "develop", "b", ac_test_refs=[REF], ac_sources=src,
                         pin_sha=SHA)  # fmt: skip
    head = cloud.split("\n")[0]
    assert SHA[:12] in head and "git rev-parse HEAD" in head and "git show" in head
    assert not head.startswith("ЧИТАТЬ ПЕРВЫМИ:"), "шапка - не строка с test_ref"
    local, _ = diff_plan(big, "develop", "b", ac_test_refs=[REF], ac_sources=src,
                         pin_sha=SHA, inline_ceiling=100_000)  # fmt: skip
    assert SHA[:12] in local.split("\n")[0] and "git show" not in local.split("\n")[0]


def test_an_unreadable_pin_does_not_fall_back_to_the_branch_sizes():
    branch = _new(F, [f"# f{k}" for k in range(900)] + _GOOD.splitlines())
    block, _ = diff_plan(branch, "develop", "b", ac_test_refs=[REF], ac_sources=None,
                         ac_block=True, pin_sha=SHA)  # fmt: skip
    assert "НЕ ПОМЕСТЯТСЯ" not in block, "размеры ветки - не размеры коммита сдачи"
    assert f"ЧИТАТЬ ПЕРВЫМИ: {UNREADABLE} ({REF})" in block


def test_the_prompt_does_not_promise_no_second_pass():
    big = _new(F, [f"# f{k}" for k in range(900)] + _GOOD.splitlines())
    block, _ = diff_plan(big, "develop", "b")
    assert "проход не гарантирован" in block
    assert "не заказывает" not in block
