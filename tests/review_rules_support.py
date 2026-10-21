"""Обязательства трёх правил ревью для тестов навыков (#1866)."""

from __future__ import annotations

import re

#: Три правила ревью (#1866): заголовок и обязательства КАЖДОГО правила. Тело
#: проверяется, а не заголовок: правило с пустым телом ничему не учит.
SKILL_RULES = {
    "Свежий взгляд до пуша": (
        "чистом контексте",
        "обязательность Codex определяют только признаки выше",
        "P1 и P2 исправь до пуша",
        "все места класса",
        "проверенный sha",
    ),
    "Класс ошибки на доработке": (
        "все места этого класса",
        "в пределах области задачи",
        "out_of_scope",
    ),
    "С третьего возврата": (
        "review_cycle` ≥ 3",
        "до кода запиши в ленту",
        "почему подход",
        "какой другой подход",
    ),
}
SEED_RULES = {
    "Свежий взгляд до пуша": (
        "субагентов нет",
        "по чек-листу",
        "P1 и P2 исправь до пуша",
        "все места класса",
        "машинное ревью хаба",
    ),
    "Класс ошибки на доработке": (
        "все места класса",
        "в пределах области задачи",
        "out_of_scope",
    ),
    "С третьего возврата": (
        "review_cycle ≥ 3",
        "до кода запиши в ленту",
        "почему прежний подход",
        "какой берёшь",
    ),
}


def _rule_body(text: str, heading: str) -> str:
    """Тело одного правила: от заголовка до следующего пункта списка."""
    flat = " ".join(text.split())
    start = flat.index(heading)
    nxt = re.search(r" - (?:\*\*)?[А-ЯA-Z]", flat[start + len(heading) :])
    end = start + len(heading) + nxt.start() if nxt else len(flat)
    return flat[start:end]


def assert_rules(text: str, rules: dict[str, tuple[str, ...]]) -> None:
    for heading, needles in rules.items():
        body = _rule_body(text, heading)
        for needle in needles:
            assert needle in body, (heading, needle)
