"""Тексты закрытого набора ``incomplete_reason`` называют ВСЕ его слова (#2280).

Слово добавлено в ``models.INCOMPLETE_REASONS`` — каждый текст, который
перечисляет причины ревьюеру или читателю отчёта, обязан его знать: иначе
навык, который агенты читают, учит двум причинам при трёх допустимых.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hub.db import MACHINE_REVIEW_CYCLE_SKILL
from hub.models import INCOMPLETE_REASONS
from hub.services.review_dispatch import REPORT_BLOCK_INSTRUCTION

_REPO = Path(__file__).resolve().parent.parent


def _between(text: str, start: str, end: str) -> str:
    head = text.index(start)
    return " ".join(text[head : text.index(end, head)].split())


def _cycle_skill_clause() -> str:
    return _between(
        MACHINE_REVIEW_CYCLE_SKILL,
        "`incomplete_reason` (#",
        "5. `hub_submit_for_review`",
    )


def _reading_skill_clause() -> str:
    text = (_REPO / "skills/hub-review-report-reading/SKILL.md").read_text("utf-8")
    return _between(text, "- Причина неполноты", "- Что докладывать")


@pytest.mark.parametrize(
    "source",
    [_cycle_skill_clause, _reading_skill_clause],
    ids=["machine-review-cycle", "hub-review-report-reading"],
)
@pytest.mark.parametrize("reason", INCOMPLETE_REASONS)
def test_a_skill_clause_names_every_reason_of_the_closed_set(source, reason) -> None:
    assert f"`{reason}`" in source()


def test_the_cycle_skill_does_not_count_the_causes_as_two() -> None:
    assert "из двух" not in _cycle_skill_clause()


def test_the_report_block_offers_every_reason_of_the_closed_set() -> None:
    enum = re.search(r'"incomplete_reason": "([^"]*)"', REPORT_BLOCK_INSTRUCTION)
    assert enum, "блок отчёта потерял поле incomplete_reason"
    assert set(enum.group(1).split("|")) == {*INCOMPLETE_REASONS, ""}
