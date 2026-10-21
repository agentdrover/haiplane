"""#1933 круг 4: бюджет вырезок - уникальные диапазоны, подмножество, полный дифф последним шагом."""

from __future__ import annotations

from pathlib import Path

from hub.services import review_oversized
from hub.services.review_dispatch import _inline_diff_lines
from hub.services.review_oversized import ac_test_spans, cutout, first_block

F = "tests/test_x.py"
SYNTACTIC = "диапазон — по тексту файла, не по исполнению"


def _new(path: str, lines: list[str]) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n"
        f"+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


def _fn(name: str, marker: str, size: int) -> list[str]:
    return [f"def {name}():", *[f"    assert {marker}_{k:04d}" for k in range(size)]]


def _file(*parts: list[str], filler: int = 0) -> list[str]:
    return [f"# filler {k:04d} " + "x" * 40 for k in range(filler)] + [
        line for part in parts for line in part
    ]


def test_a_fitting_cutout_survives_when_a_sibling_one_does_not():
    """Находка 1: полный дифф и test_large не влезают, test_small (142) - влезает."""
    lines = _file(_fn("test_small", "SMALL", 2), _fn("test_large", "LARGE", 400))
    diff, src = _new(F, lines), "\n".join(lines) + "\n"
    refs = [f"{F}::test_small", f"{F}::test_large"]
    spans = ac_test_spans({F: src}, refs)
    small, large = len(cutout(spans[0])), len(cutout(spans[1]))
    full = len(diff)
    ceiling = small + 300
    assert small < ceiling < full < large
    out, omitted = _inline_diff_lines(diff, [], None, ceiling, spans)
    text = "\n".join(out)
    assert "SMALL_0001" in text and "LARGE_0399" not in text
    assert (
        f"ac_test_unread: {refs[1]}" in text
        and f"ac_test_unread: {refs[0]}" not in text
    )
    assert omitted == [F]


def test_parametrized_refs_pay_for_one_range_once():
    """Находка 2: test_ac[a] и test_ac[b] - один диапазон, одна вырезка, обе ссылки прочитаны."""
    lines = _file(_fn("test_ac", "PAR", 20), filler=400)
    diff, src = _new(F, lines), "\n".join(lines) + "\n"
    spans = ac_test_spans({F: src}, [f"{F}::test_ac[a]", f"{F}::test_ac[b]"])
    assert (spans[0].start, spans[0].end) == (spans[1].start, spans[1].end)
    one = len(cutout(spans[0]))
    ceiling = int(one * 1.5)
    assert one <= ceiling < 2 * one < len(diff)
    out, _ = _inline_diff_lines(diff, [], None, ceiling, spans)
    text = "\n".join(out)
    assert text.count("ВЫРЕЗКА ТЕСТА AC") == 1 and "PAR_0019" in text
    assert "ac_test_unread" not in text
    assert f"{F}::test_ac[a]" in text and f"{F}::test_ac[b]" in text, (
        "обе ссылки при диапазоне"
    )


def test_the_result_does_not_depend_on_the_order_of_sections():
    """Находка 3: AC-файл (full F, cut мал) и сосед при потолке F - оба порядка равны."""
    lines = _file(_fn("test_ac", "AC", 2), filler=198)
    ac_diff, src = _new(F, lines), "\n".join(lines) + "\n"
    small = _new("hub/services/small.py", [f"v{k} = {k}" for k in range(120)])
    spans = ac_test_spans({F: src}, [f"{F}::test_ac"])
    ceiling = len(ac_diff)
    assert len(small) + len(cutout(spans[0])) < ceiling < len(ac_diff) + len(small)
    results = []
    for diff in (ac_diff + small, small + ac_diff):
        out, omitted = _inline_diff_lines(diff, [], None, ceiling, spans)
        text = "\n".join(out)
        results.append(
            (omitted, "v119 = 119" in text, "AC_0001" in text, "filler 0100" in text)
        )
    assert results[0] == results[1], "порядок разделов не должен менять состав промта"
    assert results[0] == ([F], True, True, False), (
        "сосед на месте, тест AC вырезкой, полный дифф - из остатка"
    )


def test_the_locator_is_declared_syntactic():
    block = first_block(
        ac_test_spans({F: "def test_ac():\n    pass\n"}, [f"{F}::test_ac"])
    )
    assert SYNTACTIC in block and "exec/globals()" in block
    assert SYNTACTIC in (review_oversized.__doc__ or "")
    root = Path(__file__).resolve().parent.parent
    assert SYNTACTIC in (root / "docs/agent-context/invariants.md").read_text()
