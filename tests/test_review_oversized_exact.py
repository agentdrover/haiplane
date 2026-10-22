"""#1993: выбор покрытия AC на файл - точный перебор подмножеств, а не жадность (контрпример #1981)."""

from __future__ import annotations

import itertools
import logging
import random

import pytest

from hub.services.review_oversized import (
    AcTestSpan,
    RangeKey,
    _reserve_ac,
    cutout,
    fit_with_ac_cutouts,
    shown_in,
)

F = "tests/test_x.py"
PLAIN = "hub/other.py"


def _spans(start: int, n_lines: int, width: int, refs: int, tag: str) -> list[AcTestSpan]:
    """Один диапазон ``start..start+n_lines-1`` с ``refs`` ссылками (parametrize)."""
    lines = tuple((start + i, "x" * width) for i in range(n_lines))
    end = start + n_lines - 1
    return [
        AcTestSpan(f"{F}::{tag}[{i}]", F, start, end, lines=lines) for i in range(refs)
    ]


def _file_diff(path: str, hunk_to: int, size: int) -> str:
    """Дифф файла длиной ровно ``size``: один ханк ``+1..hunk_to``, остальное - тело."""
    head = f"diff --git a/{path} b/{path}\n@@ -0,0 +1,{hunk_to} @@\n"
    return head + "+" + "p" * (size - len(head) - 2) + "\n"


def _cost(spans: list[AcTestSpan]) -> int:
    return len(cutout(spans[0], [s.ref for s in spans]))


@pytest.mark.parametrize("plain_first", [True, False])
def test_c_a_b_scenario_picks_c_plus_a_and_keeps_the_plain_file(plain_first: bool):
    """Сценарий Codex: C (10 ссылок, покрыт ханком), A, B; полный дифф не должен вытеснять обычный файл."""
    c = _spans(1, 10, 270, 10, "c")
    a = _spans(100, 3, 280, 1, "a")
    b = _spans(200, 3, 310, 1, "b")
    cc, ca, cb = _cost(c), _cost(a), _cost(b)
    assert ca < cb < cc
    plain_size = 400
    full_size = cc + ca + 1
    ceiling = cc + ca + plain_size
    assert full_size + plain_size > ceiling >= full_size
    test_file = (F, _file_diff(F, 10, full_size))
    plain = (PLAIN, _file_diff(PLAIN, 3, plain_size))
    sections = [plain, test_file] if plain_first else [test_file, plain]
    text, omitted, cuts, unread = fit_with_ac_cutouts(sections, ceiling, c + a + b)
    assert omitted == []
    assert unread == [s.ref for s in b]
    assert len(cuts) == 2 and any("::a[0]" in x for x in cuts)
    assert not any("::b[0]" in x for x in cuts)
    assert PLAIN in text
    assert len(text) + sum(len(x) for x in cuts) <= ceiling


def _brute(
    ranges: dict[RangeKey, list[AcTestSpan]],
    texts: dict[str, str],
    cost: dict[RangeKey, int],
    ceiling: int,
) -> tuple[int, int]:
    """Эталон: полный перебор обоих планов; ``(ссылок прочитано, потрачено)`` лучшего."""
    keys = list(ranges)
    best = (0, 0)
    for r in range(len(keys) + 1):
        for sub in itertools.combinations(keys, r):
            spent = sum(cost[k] for k in sub)
            if spent <= ceiling:
                best = max(best, (sum(len(ranges[k]) for k in sub), -spent))
    text = texts[F]
    if len(text) <= ceiling:
        covered = [k for k in keys if shown_in(text, ranges[k][0])]
        rest = [k for k in keys if k not in covered]
        for r in range(len(rest) + 1):
            for sub in itertools.combinations(rest, r):
                spent = len(text) + sum(cost[k] for k in sub)
                if spent <= ceiling:
                    got = sum(len(ranges[k]) for k in (*covered, *sub))
                    best = max(best, (got, -spent))
    return best[0], -best[1]


def test_exact_choice_matches_brute_force():
    """Случайные наборы до 8 диапазонов, seed фиксирован: ссылки и стоимость равны полному перебору."""
    rng = random.Random(1993)
    checked = 0
    for _ in range(300):
        ranges: dict[RangeKey, list[AcTestSpan]] = {}
        line = 1
        for i in range(rng.randint(1, 8)):
            sp = _spans(line, rng.randint(1, 4), rng.randint(20, 300), rng.randint(1, 6), f"t{i}")
            ranges[(F, line, line + len(sp[0].lines) - 1)] = sp
            line += 20
        cost = {k: _cost(v) for k, v in ranges.items()}
        hunk_to = rng.choice([0, 20, 60, 100])
        size = rng.randint(200, sum(cost.values()) + 400)
        head = f"diff --git a/{F} b/{F}\n"
        text = head + (f"@@ -0,0 +1,{hunk_to} @@\n" if hunk_to else "") + "+" + "p" * size
        texts = {F: text}
        ceiling = rng.randint(100, sum(cost.values()) + len(text))
        left, placed, full, covered = _reserve_ac(ranges, cost, texts, [F], ceiling)
        read = sum(len(ranges[k]) for k in placed | covered)
        spent = ceiling - left
        want = _brute(ranges, texts, cost, ceiling)
        assert (read, spent) == want, (checked, ceiling, want)
        assert spent <= ceiling
        checked += 1
    assert checked == 300


def test_above_the_limit_falls_back_to_greedy_and_logs_it(caplog: pytest.LogCaptureFixture):
    """13 диапазонов на файл: не перебор, а жадно по ссылки/стоимость; потолок цел, в логе пометка."""
    ranges: dict[RangeKey, list[AcTestSpan]] = {}
    for i in range(13):
        sp = _spans(1 + i * 20, 2, 50 + i, 1 + i % 3, f"g{i}")
        ranges[(F, sp[0].start or 0, sp[0].end or 0)] = sp
    cost = {k: _cost(v) for k, v in ranges.items()}
    ceiling = sum(cost.values()) // 2
    with caplog.at_level(logging.WARNING):
        left, placed, full, covered = _reserve_ac(ranges, cost, {}, [F], ceiling)
    assert sum(cost[k] for k in placed) == ceiling - left <= ceiling
    assert placed and not full
    assert any("жадно" in r.getMessage() for r in caplog.records)
    caplog.clear()
    small = dict(list(ranges.items())[:12])
    with caplog.at_level(logging.WARNING):
        _reserve_ac(small, cost, {}, [F], ceiling)
    assert not caplog.records
