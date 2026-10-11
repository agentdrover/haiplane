"""Контракт сдачи: model, summary и мутация на каждый AC с тестом (#1436).

Облачный исполнитель дважды подряд сдал без обязательного по промпту: SID-8
(#1383) — без модели, SID-9 (#1384) — без модели, без описания и без мутаций.
Без модели ревью другого семейства (#758) заказывается вслепую; без мутаций не
доказано, что тесты AC ловят поломку. Промпт этого не удерживает — удерживает
проверка здесь, по политике проекта ``submission_contract``.

Чистые функции: вход — тело сдачи и строки AC, выход — перечень нарушений
словами. Решение warn против require принимает шаг сдачи в lifecycle.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from hub.models import SubmissionMutation, TaskSubmitReview

#: Формат поля, который отказ называет прямо: исполнитель без MCP читает REST,
#: и ошибка — единственное место, где он узнает, чего от него ждут.
MUTATIONS_FORMAT = (
    'mutations: [{"ac": "AC-1", "mutation": "что сломано в коде", '
    '"failed_test": "<test_ref этого AC>"}] — по записи на каждый AC с '
    "verifiable_by=test; failed_test совпадает с test_ref именно этого AC"
)

#: Начало записи в карточке при warn — по нему запись находит читатель.
VIOLATION_HEADER = "Контракт сдачи нарушен"


def _test_acs(ac_rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """AC с verifiable_by=test → их test_ref (пустая строка, если не задан)."""
    return {
        str(row["ac_id"]): str(row["test_ref"] or "").strip()
        for row in ac_rows
        if str(row["verifiable_by"] or "") == "test"
    }


def _mutation_violations(
    mutations: list[SubmissionMutation], known: set[str], test_acs: dict[str, str]
) -> list[str]:
    found: list[str] = []
    for index, item in enumerate(mutations):
        ac = item.ac.strip()
        if ac not in known:
            found.append(
                f"mutations[{index}].ac={ac!r}: такого AC у задачи нет "
                f"(есть: {', '.join(sorted(known)) or '—'})"
            )
        if not item.mutation.strip():
            found.append(f"mutations[{index}] ({ac or '—'}): пустое поле mutation")
    for ac_id, test_ref in test_acs.items():
        own = [m for m in mutations if m.ac.strip() == ac_id]
        if not own:
            found.append(
                f"{ac_id} (verifiable_by=test) не покрыт ни одной записью mutations"
            )
            continue
        if test_ref and not any(m.failed_test.strip() == test_ref for m in own):
            named = ", ".join(repr(m.failed_test.strip()) for m in own)
            found.append(
                f"{ac_id}: failed_test {named} не совпадает с test_ref этого AC "
                f"({test_ref!r})"
            )
        elif not test_ref and not any(m.failed_test.strip() for m in own):
            found.append(f"{ac_id}: у записи mutations пустое поле failed_test")
    return found


def violations(
    body: TaskSubmitReview, ac_rows: Iterable[Mapping[str, Any]]
) -> list[str]:
    """Все нарушения контракта сдачи — перечнем, не первым попавшимся."""
    rows = list(ac_rows)
    found: list[str] = []
    if not (body.model or "").strip():
        found.append("model пуст — назовите модель, написавшую сдачу (#758)")
    if not (body.summary or "").strip():
        found.append("summary пуст — опишите, что сдано и чем проверено")
    known = {str(row["ac_id"]) for row in rows}
    found += _mutation_violations(list(body.mutations), known, _test_acs(rows))
    return found


def warning_text(found: list[str]) -> str:
    """Одна запись в карточку при warn — с перечнем и форматом поля."""
    return (
        f"{VIOLATION_HEADER} (режим warn, сдача принята):\n— "
        + "\n— ".join(found)
        + f"\nФормат: {MUTATIONS_FORMAT}."
    )


def mutations_text(mutations: list[SubmissionMutation]) -> str:
    """Мутации сдачи строкой для записи о сдаче; пусто — пустая строка.

    Запись о сдаче — то, что читают карточка и пакет судьи
    (review_evidence.latest_submission_text), поэтому мутации кладутся в неё,
    а не в отдельную таблицу: схема не меняется.
    """
    if not mutations:
        return ""
    lines = [
        f"{m.ac.strip() or '—'}: {m.mutation.strip() or '—'} → падает "
        f"{m.failed_test.strip() or '—'}"
        for m in mutations
    ]
    return "\nМутации по AC (заявлены автором, #1436):\n— " + "\n— ".join(lines)
