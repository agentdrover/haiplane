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


def _modified(path: str, rows: list[str]) -> str:
    """Дифф существующего файла: один ханк в начале (``rows`` с префиксами)."""
    old = sum(1 for r in rows if r[0] != "+")
    new = sum(1 for r in rows if r[0] != "-")
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,{old} +1,{new} @@\n" + "".join(f"{r}\n" for r in rows)
    )


def test_a_skipped_cutout_does_not_block_a_fitting_full_diff():
    """Codex 4.1: полный дифф влезает, лишняя большая вырезка не влезла - она непрочитана, дифф есть."""
    src_lines = [
        "x = 0",
        *_fn("test_small", "SMALLX", 1),
        *_fn("test_large", "LARGEX", 300),
    ]
    src = "\n".join(src_lines) + "\n"
    diff = _modified(F, ["-x = 1", "+x = 0"])
    refs = [f"{F}::test_small", f"{F}::test_large"]
    spans = ac_test_spans({F: src}, refs)
    small, large = len(cutout(spans[0])), len(cutout(spans[1]))
    ceiling = small + len(diff) + 200
    assert large > ceiling
    out, omitted = _inline_diff_lines(diff, [], None, ceiling, spans)
    text = "\n".join(out)
    assert omitted == [] and "+x = 0" in text and "SMALLX_0000" in text
    assert (
        f"ac_test_unread: {refs[1]}" in text
        and f"ac_test_unread: {refs[0]}" not in text
    )


def test_plain_files_are_chosen_the_same_whatever_the_order_of_sections():
    """Codex 4.2: оба файла влезают поодиночке, вместе нет - выбор не зависит от порядка."""
    big = _new("hub/c.py", [f"c{k} = {k}" for k in range(40)])
    small = _new("hub/b.py", [f"b{k} = {k}" for k in range(10)])
    ceiling = len(big) + 5
    assert len(small) < len(big) < ceiling < len(big) + len(small)
    picked = []
    for diff in (big + small, small + big):
        out, omitted = _inline_diff_lines(diff, [], None, ceiling, [])
        picked.append((omitted, "b9 = 9" in "\n".join(out)))
    assert picked[0] == picked[1] == (["hub/c.py"], True)


def test_a_repeated_plain_path_is_shown_whole_or_not_at_all():
    """Codex 4.3: смена symlink на файл (удаление+добавление одного пути) - файл целиком или никак."""
    gone = (
        "diff --git a/hub/l.py b/hub/l.py\ndeleted file mode 120000\n--- a/hub/l.py\n"
        "+++ /dev/null\n@@ -1 +0,0 @@\n-target\n"
    )
    added = _new("hub/l.py", [f"n{k} = {k}" for k in range(60)])
    ceiling = len(gone) + 20
    out, omitted = _inline_diff_lines(gone + added, [], None, ceiling, [])
    text = "\n".join(out)
    assert omitted == ["hub/l.py"] and "-target" not in text and "n59 = 59" not in text


def test_cutouts_joined_for_the_prompt_do_not_exceed_the_ceiling():
    """Codex 4.4: разделитель join не должен добавлять символы сверх рассчитанного."""
    src = "\n".join([*_fn("test_a", "AA", 3), *_fn("test_b", "BB", 3)]) + "\n"
    spans = ac_test_spans({F: src}, [f"{F}::test_a", f"{F}::test_b"])
    total = sum(len(cutout(s)) for s in spans)
    out, _ = _inline_diff_lines(_new(F, src.splitlines() * 40), [], None, total, spans)
    cuts = [x for x in out if x.startswith("=== ВЫРЕЗКА")]
    assert len(cuts) == 2
    assert len("\n".join(cuts)) + 1 <= total


# --- круг 5: на шаге A представление AC-файла - ДЕШЁВОЕ из двух, выбранное до шага B ---


def _big_ac(n: int = 900):
    rows = ["def test_ac():", *["    pass #" + "x" * 153] * n]
    return _new(F, rows), "\n".join(rows) + "\n"


def _first_with_ceiling(ceiling: int, diffs):
    diff, src = _big_ac()
    spans = ac_test_spans({F: src}, [f"{F}::test_ac"])
    results = []
    for parts in diffs(diff):
        out, omitted = _inline_diff_lines("".join(parts), [], None, ceiling, spans)
        text = "\n".join(out)
        results.append(
            (omitted, "ВЫРЕЗКА" in text, "ac_test_unread" in text, "x = 1" in text)
        )
    return results


def test_the_cheaper_representation_of_an_ac_file_is_chosen_before_plain_files():
    """Вердикт gen 4: полный дифф дешевле вырезки; обычный файл не вытесняет AC (оба потолка, оба порядка)."""
    diff, src = _big_ac()
    spans = ac_test_spans({F: src}, [f"{F}::test_ac"])
    other = _new("hub/other.py", ["x = 1" + " " * 1500])
    full, cut = len(diff), len(cutout(spans[0]))
    assert full < cut and len(other) + full > full + 1000
    lo = full + 1300  # other (1618) уже не помещается рядом с полным диффом
    hi = full + len(other) + 5
    assert lo < cut and hi < cut + 1000
    for ceiling, expect in ((lo, (["hub/other.py"], False, False, False)),
                            (hi, ([], False, False, True))):  # fmt: skip
        results = _first_with_ceiling(ceiling, lambda d: [(d, other), (other, d)])
        assert results[0] == results[1] == expect, (ceiling, results)


def test_a_cheaper_full_diff_keeps_room_for_the_neighbour():
    """Машинное ревью gen 4: full 19038 < cut 20108, сосед 392, потолок = cut - вместе влезают."""
    lines = _file(_fn("test_ac", "AC", 300))
    ac, src = _new(F, lines), "\n".join(lines) + "\n"
    spans = ac_test_spans({F: src}, [f"{F}::test_ac"])
    neigh = _new("hub/neigh.py", [f"n{k} = {k}" for k in range(20)])
    ceiling = len(cutout(spans[0]))
    assert len(ac) + len(neigh) <= ceiling < len(cutout(spans[0])) + len(neigh)
    for parts in ((ac, neigh), (neigh, ac)):
        out, omitted = _inline_diff_lines("".join(parts), [], None, ceiling, spans)
        text = "\n".join(out)
        assert omitted == [] and "AC_0299" in text and "n19 = 19" in text
        assert "ВЫРЕЗКА" not in text


def test_the_step_b_docstring_tells_the_real_order():
    from hub.services.review_oversized import fit_with_ac_cutouts

    doc = fit_with_ac_cutouts.__doc__ or ""
    assert "от меньшего к большему, затем по пути" in doc
    assert "по порядку диффа, из остатка" not in doc


def test_a_lone_carriage_return_makes_the_line_numbers_unknown():
    """Codex 5: ast и git считают одиночный CR по-разному - диапазон не выдаётся."""
    src = "x = 1\rdef test_ac():\n    pass\n"
    (span,) = ac_test_spans({F: src}, [f"{F}::test_ac"])
    assert not span.found and span.unreadable
    crlf = "x = 1\r\ndef test_ac():\r\n    pass\r\n"
    assert ac_test_spans({F: crlf}, [f"{F}::test_ac"])[0].start == 2


# --- #1981: полный дифф AC-файла - самостоятельное покрытие, выбор и оплата до шага B ---


def _codex_case(inside_n: int = 900, outside_n: int = 1100):
    """Сценарий Codex: test_ac целиком в ханке, test_outside вне ханка (виден лишь заголовок)."""
    inside = ["def test_ac():"] + ["    pass #" + "x" * 153] * inside_n
    outside = ["def test_outside():"] + ["    pass #" + "y" * 153] * outside_n
    ac = _modified(F, ["+" + s for s in inside] + [" " + outside[0]])
    refs = [f"{F}::test_ac", f"{F}::test_outside"]
    spans = ac_test_spans({F: "\n".join(inside + outside) + "\n"}, refs)
    return ac, spans


def _run(parts, ceiling, spans):
    out, omitted = _inline_diff_lines("".join(parts), [], None, ceiling, spans)
    return "\n".join(out), omitted


def test_full_diff_covers_its_ac_even_when_a_sibling_cutout_cannot_fit():
    ac, spans = _codex_case()
    other = _new("hub/other.py", ["x = 1" + " " * 1500])
    refs = [s.ref for s in spans]
    assert len(cutout(spans[0])) > 150_000 and len(cutout(spans[1])) > 153_000
    for ceiling, other_in in ((150_000, False), (153_000, True)):
        for parts in ((ac, other), (other, ac)):
            text, omitted = _run(parts, ceiling, spans)
            assert "ВЫРЕЗКА ТЕСТА AC" not in text, (ceiling, "дубль диапазона")
            assert text.count("pass #" + "x" * 153) == 900, ceiling
            assert f"ac_test_unread: {refs[0]}" not in text
            assert f"ac_test_unread: {refs[1]}" in text
            assert ("x = 1" in text) is other_in and (omitted == []) is other_in
            assert len(text) <= ceiling + 4000, "разделители - малая добавка"


def test_budget_properties_hold_over_combinations():
    """Свойства, не цифра: обычный файл не вытесняет AC, диапазон не дублируется, порядок не важен."""
    for inside_n, outside_n, ceiling_pad in (
        (900, 1100, -3000),
        (900, 1100, 0),
        (40, 600, 500),
        (40, 5, 100),
        (300, 300, -50),
    ):
        ac, spans = _codex_case(inside_n, outside_n)
        other = _new("hub/other.py", ["x = 1" + " " * 1500])
        ceiling = len(ac) + ceiling_pad
        results = []
        for parts in ((ac, other), (other, ac)):
            text, omitted = _run(parts, ceiling, spans)
            results.append((text, omitted))
            for s in spans:
                assert text.count(f"{s.path}:{s.start}–{s.end}") <= 1
            if "x = 1" in text:
                # обычный файл включён: ни одна непрочитанная вырезка не влезла бы
                # в бюджет, занятый им
                room = ceiling - (len(text) - len(other))
                for s in spans:
                    if f"ac_test_unread: {s.ref}" in text:
                        assert len(cutout(s)) > room, (inside_n, ceiling_pad, s.ref)
        assert results[0] == results[1], "порядок разделов не меняет состав"
