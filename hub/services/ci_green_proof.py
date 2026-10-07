"""«Зелёный CI доказан» для сдачи (#1629, эпик #1628).

Правило владельца «push -> CI -> success на sha -> submit» знало только стюард.
Здесь оно становится предикатом над отчётом CI о закреплённом коммите, который
гейт ``ci_before_submit`` вызывает на явной сдаче.

Это НЕ ``review_ci_gate.failed_checks`` (#1405): тот отвечает «что отчёт назвал
красным» и молчит, когда отчёта нет или он неполный. Здесь молчание — отказ в
доказательстве: отчёт обязан быть, валидация обязана быть ``pass``, ни одна
проверка и ни один AC-тест не красные. Семантика ``failed_checks`` не меняется.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from hub.services.ac_tests import FAIL as AC_FAIL, NOT_FOUND as AC_NOT_FOUND
from hub.services.ci_report import AC_STATUSES, CHECK_FAIL, CHECK_OUTCOMES
from hub.services.test_locator import parse_test_locator
from hub.services.validation_run import PASS as VALIDATION_PASS

NO_REPORT = "no_report"
RED = "red"
VALIDATION_NOT_PASS = "validation_not_pass"  # nosec B105 - cause name
MALFORMED = "malformed"

ALERT_HEADER = "CI до сдачи не доказан"


@dataclass(frozen=True)
class Gap:
    """Чем именно зелёный не доказан: причина и перечень по пунктам."""

    cause: str
    violations: list[str] = field(default_factory=list)


def _parsed(raw: Any, allowed: frozenset[str]) -> dict[str, str] | None:
    """Объект {имя: статус} из словаря репортёра; иначе None.

    Пусто или None — повреждённое поле: колонка по умолчанию хранит ``{}``, и
    пустая строка значит, что запись не прошла через репортёра. Значение вне
    словаря (в т.ч. вложенный объект) тоже повреждение, а не «зелёное».
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    if any(not isinstance(v, str) or v.strip() not in allowed for v in value.values()):
        return None
    return {str(k): v.strip() for k, v in value.items()}


def _red_acs(
    ac_rows: Iterable[Mapping[str, Any]], ac_results: Mapping[str, str]
) -> list[str]:
    """Тестовые AC с локатором: красные или без результата вовсе.

    Репортёр пишет результат для КАЖДОГО тестового AC с локатором (pass, fail
    или not_found), поэтому отсутствие результата — недоказанность: AC могли
    добавить после отчёта о том же sha.
    """
    out = []
    for row in ac_rows:
        ac_id = str(row["ac_id"])
        if str(row["verifiable_by"] or "") != "test":
            continue
        if parse_test_locator(str(row["test_ref"] or "")) is None:
            continue
        status = ac_results.get(ac_id)
        if status is None:
            out.append(f"{ac_id} (нет результата в отчёте)")
        elif status in (AC_FAIL, AC_NOT_FOUND):
            out.append(f"{ac_id} ({status})")
    return out


def proof_gap(
    report: Mapping[str, Any] | None, ac_rows: Iterable[Mapping[str, Any]], sha: str
) -> Gap | None:
    """None — зелёный о ``sha`` доказан; иначе Gap с причиной."""
    if report is None or not sha:
        return Gap(NO_REPORT, [f"отчёта CI о коммите {sha[:12] or '—'} нет"])
    checks = _parsed(report.get("checks"), CHECK_OUTCOMES)
    ac_results = _parsed(report.get("ac_results"), AC_STATUSES)
    if checks is None or ac_results is None:
        return Gap(
            MALFORMED,
            [
                "checks или ac_results отчёта повреждены: не объект или статус вне словаря"
            ],
        )
    red = [f"проверка {k}" for k, v in sorted(checks.items()) if v == CHECK_FAIL]
    red += [f"AC {item}" for item in _red_acs(ac_rows, ac_results)]
    if red:
        return Gap(RED, red)
    status = str(report.get("validation_status") or "").strip()
    if status != VALIDATION_PASS:
        return Gap(
            VALIDATION_NOT_PASS, [f"validation_status={status or 'пусто'}, нужен pass"]
        )
    return None


def warning_text(sha: str, gap: Gap) -> str:
    """Одна запись в карточку при warn."""
    return (
        f"{ALERT_HEADER} (режим warn, сдача принята; коммит {sha[:12] or '—'}, "
        f"причина {gap.cause}):\n— " + "\n— ".join(gap.violations)
    )
