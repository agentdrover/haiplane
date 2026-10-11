"""Гейт красного теста: доказательство берётся из прогона, а не из слов (#913).

Багфикс сдавался без доказательства воспроизведения. Мутации сдачи (#1436) —
заявление автора, и хаб не мог их проверить: отчёт CI принимается только о
закреплённом коммите (ci_report.py), понятия «до фикса» в нём не было. Теперь
CI на ветке task-* прогоняет изменённые тестовые файлы поверх кода merge-base
(scripts/red_test_baseline.py) и отдаёт статус каждого теста полем
``baseline`` того же отчёта. Здесь решается, что из этого относится к AC.

Правила, ради которых модуль существует:

* Красное — только ``failed`` (assert). ``error`` — ImportError, падение
  сборки, упавшая фикстура — значит, что тест упал по неверной причине и
  ничего не воспроизвёл.
* Отсутствие baseline — «не доказано» с причиной, а не тихий пропуск.
* Слова и мутации автора доказательством не считаются: функции сюда их даже
  не принимают.

Чистые функции: вход — строки AC и baseline, выход — доказанное и причины по
AC словами. Режим warn против require решает шаг сдачи в lifecycle.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from hub.services.test_locator import parse_test_locator

FAILED = "failed"
PASSED = "passed"
ERROR = "error"
SKIPPED = "skipped"
#: Статусы, которые шаг CI может назвать тесту; иное хаб не принимает.
BASELINE_STATUSES = frozenset({FAILED, PASSED, ERROR, SKIPPED})
#: Прогон состоялся — только такой baseline что-то говорит о тестах.
STATE_RAN = "ran"

#: Начало записи в карточке, когда всё доказано.
EVIDENCE_HEADER = "Красный тест до фикса доказан"
#: Начало записи в карточке при warn — по нему запись находит читатель.
ALERT_HEADER = "Красный тест до фикса не доказан"
#: Что говорит отказ о словах автора: читатель без MCP видит только его.
NOT_EVIDENCE = (
    "Доказательство — только прогон CI поверх кода merge-base (поле baseline "
    "отчёта о закреплённом коммите): mutations и summary сдачи за "
    "воспроизведение не считаются."
)


@dataclass(frozen=True)
class Proof:
    """Один AC, чей тест упал на коде базы."""

    ac_id: str
    test_ref: str
    status: str


def parse_baseline(raw: str | None) -> dict[str, Any]:
    """Baseline из строки отчёта; нечитаемое или пустое — пустой словарь."""
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def validate_baseline(baseline: Mapping[str, Any]) -> None:
    """Отказ ValueError на статусе, которого хаб не знает."""
    tests = baseline.get("tests") or {}
    if not isinstance(tests, Mapping):
        raise ValueError("baseline.tests must be an object {nodeid: status}")
    for nodeid, status in tests.items():
        if status not in BASELINE_STATUSES:
            raise ValueError(
                f"unknown baseline status {status!r} for {nodeid!r}; "
                f"expected one of {sorted(BASELINE_STATUSES)}"
            )


def _combined(statuses: list[str]) -> str:
    """Один статус на параметризованный тест: красный, если упал хоть один.

    Красный тест часто — новый кейс в существующем parametrize: на базе он
    падает, соседние проходят, и это воспроизведение, а не «зелёный до фикса».
    """
    for status in (FAILED, ERROR, PASSED):
        if status in statuses:
            return status
    return SKIPPED


def baseline_status(baseline: Mapping[str, Any], test_ref: str) -> str:
    """Статус теста в baseline; ``""`` — теста в прогоне нет.

    Параметризованный тест без ``[...]`` в test_ref собирается из своих
    вариантов. Тест, чей файл не собрался на базе, — ``error``.
    """
    tests = baseline.get("tests") or {}
    if not isinstance(tests, Mapping):
        return ""
    if test_ref in tests:
        return str(tests[test_ref])
    variants = [str(v) for k, v in tests.items() if str(k).startswith(test_ref + "[")]
    if variants:
        return _combined(variants)
    errors = baseline.get("collection_errors") or {}
    path = test_ref.split("::", 1)[0]
    if isinstance(errors, Mapping) and path in errors:
        return ERROR
    return ""


def _test_acs(ac_rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    return {
        str(row["ac_id"]): str(row["test_ref"] or "").strip()
        for row in ac_rows
        if str(row["verifiable_by"] or "") == "test"
        and str(row["test_ref"] or "").strip()
    }


def _reason(ac_id: str, ref: str, status: str, baseline: Mapping[str, Any]) -> str:
    """Причина «не доказано» по одному AC словами."""
    if parse_test_locator(ref) is None:
        return f"{ac_id} {ref}: test_ref не nodeid — прогнать нечем"
    if status == PASSED:
        return f"{ac_id} {ref}: зелёный до фикса — на коде базы тест прошёл"
    if status == ERROR:
        path = ref.split("::", 1)[0]
        errors = baseline.get("collection_errors") or {}
        if (
            isinstance(errors, Mapping)
            and path in errors
            and ref not in (baseline.get("tests") or {})
        ):
            return (
                f"{ac_id} {ref}: error — файл не собрался на коде базы "
                f"({str(errors[path])[:160]}); упал по неверной причине"
            )
        return (
            f"{ac_id} {ref}: error — упал по неверной причине (импорт, "
            "фикстура, сборка), а не на assert"
        )
    if status == SKIPPED:
        return f"{ac_id} {ref}: skipped на коде базы — ничего не воспроизведено"
    return (
        f"{ac_id} {ref}: нет в baseline — CI не прогнал этот тест на коде "
        "merge-base (тестовый файл не менялся в диффе?)"
    )


def evaluate(
    ac_rows: Iterable[Mapping[str, Any]],
    baseline: Mapping[str, Any] | None,
    *,
    head_sha: str,
) -> tuple[list[Proof], list[str]]:
    """(доказанное, причины «не доказано») по каждому AC с test_ref."""
    acs = _test_acs(ac_rows)
    if not acs:
        return [], ["ни у одного AC нет test_ref — воспроизводить нечем"]
    if not baseline or baseline.get("state") != STATE_RAN:
        why = _missing_reason(baseline, head_sha)
        return [], [
            f"{ac_id} {ref}: нет baseline — {why}" for ac_id, ref in acs.items()
        ]
    proofs: list[Proof] = []
    found: list[str] = []
    for ac_id, ref in acs.items():
        status = baseline_status(baseline, ref)
        if status == FAILED and parse_test_locator(ref) is not None:
            proofs.append(Proof(ac_id, ref, status))
        else:
            found.append(_reason(ac_id, ref, status, baseline))
    return proofs, found


def _missing_reason(baseline: Mapping[str, Any] | None, head_sha: str) -> str:
    sha = (head_sha or "")[:12] or "—"
    if not head_sha:
        return "коммит сдачи не закреплён, отчёт CI не с чем сверить"
    if baseline is None:
        return f"CI не присылал отчёт о коммите {sha}"
    if not baseline:
        return f"отчёт CI о коммите {sha} пришёл без поля baseline"
    state = baseline.get("state") or "—"
    detail = str(baseline.get("reason") or "").strip()
    return f"шаг baseline о коммите {sha} не прогнал тесты (state={state}" + (
        f": {detail[:160]})" if detail else ")"
    )


def _proof_lines(proofs: list[Proof]) -> str:
    return "\n— ".join(f"{p.ac_id} {p.test_ref} — {p.status}" for p in proofs)


def evidence_text(
    proofs: list[Proof], baseline: Mapping[str, Any], *, head_sha: str
) -> str:
    """Запись в карточку, когда каждый AC доказан."""
    merge_base = str(baseline.get("merge_base") or "")[:12] or "—"
    return (
        f"{EVIDENCE_HEADER} (#913): прогон CI о коммите {head_sha[:12]}, "
        f"тесты ветки поверх кода merge-base {merge_base}:\n— " + _proof_lines(proofs)
    )


def warning_text(
    found: list[str], proofs: list[Proof], baseline: Mapping[str, Any] | None
) -> str:
    """Одна запись в карточку при warn: недоказанное и доказанное."""
    text = f"{ALERT_HEADER} (bug_red_test=warn, сдача принята):\n— " + "\n— ".join(
        found
    )
    if proofs:
        merge_base = str((baseline or {}).get("merge_base") or "")[:12] or "—"
        text += f"\nДоказано (merge-base {merge_base}):\n— " + _proof_lines(proofs)
    return text + f"\n{NOT_EVIDENCE}"
