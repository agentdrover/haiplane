"""#1933 круг 3, Codex: связывания в заголовках, comprehension, повтор пути, ранний возврат, квитанция."""

from __future__ import annotations

import aiosqlite
import pytest
from httpx import AsyncClient

from hub.services.review_dispatch import _inline_diff_lines, diff_plan
from hub.services.review_oversized import ac_test_spans
from tests.review_dispatch_support import _DispatchRecorder, _submitted, _wire

F = "tests/test_x.py"
REF = f"{F}::test_ac"
GOOD = "def test_ac():\n    assert 1\n"


def _span(src: str, ref: str = REF):
    (span,) = ac_test_spans({F: src}, [ref])
    return span


@pytest.mark.parametrize(
    "tail",
    [
        "def helper(x=(test_ac := replacement)):\n    pass\n",
        "@(test_ac := wrap)\ndef other():\n    pass\n",
        "class Other(test_ac := Base):\n    pass\n",
        "f = lambda x=(test_ac := 1): x\n",
        "def other(x: (test_ac := int)):\n    pass\n",
        "[(test_ac := i) for i in items]\n",
    ],
)
def test_a_binding_in_a_header_or_comprehension_walrus_is_unknown(tail: str):
    span = _span(GOOD + tail)
    assert not span.found and span.unreadable, tail


def test_a_comprehension_target_does_not_rebind_the_outer_name():
    span = _span(GOOD + "xs = [test_ac for test_ac in items]\n")
    assert span.found and (span.start, span.end) == (1, 2)


def _new(path: str, lines: list[str]) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n"
        f"+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


def test_two_sections_of_one_path_are_paid_for_together():
    """Смена symlink на файл: удаление и добавление с одним путём - стоимость суммарная."""
    rows = [f"    assert PAY_{k:03d}_{'x' * 30}" for k in range(60)]
    lines = ["def test_ac():", *rows]
    one = _new(F, lines)
    gone = (
        f"diff --git a/{F} b/{F}\ndeleted file mode 120000\n--- a/{F}\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-" + "y" * 1200 + "\n"
    )
    diff = gone + one
    spans = ac_test_spans({F: "\n".join(lines) + "\n"}, [REF])
    ceiling = len(one) + 200
    out, _ = _inline_diff_lines(diff, [], None, ceiling, spans)
    used = sum(len(x) for x in out if not x.startswith("НЕ ВОШЛИ"))
    assert used <= ceiling + 400, (
        "шапки и пометки - малая добавка, но не 1200 лишних символов"
    )
    assert "y" * 1200 not in "\n".join(out) or used <= ceiling + 400


def test_an_unreadable_branch_diff_still_gets_the_block_from_the_pin():
    rows = [f"# f{k}" for k in range(900)] + GOOD.splitlines()
    pinned = _new(F, rows)
    sources = {F: "\n".join(rows) + "\n"}
    block, _ = diff_plan(None, "develop", "b", ac_test_refs=[REF], ac_sources=sources,
                         pinned_diff=pinned, pin_sha="a" * 40)  # fmt: skip
    assert "aaaaaaaaaaaa" in block and f"{F}:901–902 ({REF})" in block
    unknown, _ = diff_plan(None, "develop", "b", ac_test_refs=[REF], ac_sources=None,
                           ac_block=True, pin_sha="a" * 40)  # fmt: skip
    assert "не прочитан" in unknown and "ЧИТАТЬ ПЕРВЫМИ" in unknown


async def test_the_intake_receipt_names_the_unread_tests(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    recorder = _DispatchRecorder({"agent": {"id": "bc-r"}, "run": {"id": "r-r"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(
        client, db, "receipt",
        acs=[{"id": "AC-1", "given": "g", "when": "w", "then": "t",
              "verifiable_by": "test", "test_ref": REF}],
    )  # fmt: skip
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review", "agent_count": 1, "tokens_spent": 1,
            "model": "m", "raw_count": 1, "findings_confirmed": [],
            "findings_rejected": [], "incomplete": True, "unresolved": [],
            "lost_dimensions": [f"ac_test_unread: {REF}"], "agent": "r",
        },
    )  # fmt: skip
    assert resp.status_code == 200, resp.text
    assert resp.json()["ac_tests_unread"] == [REF]
