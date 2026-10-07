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
from hub.services.ci_report import CHECK_FAIL
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


def _parsed(raw: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _red_acs(
    ac_rows: Iterable[Mapping[str, Any]], ac_results: Mapping[str, Any]
) -> list[str]:
    return [
        f"{row['ac_id']} ({ac_results[str(row['ac_id'])]})"
        for row in ac_rows
        if str(row["verifiable_by"] or "") == "test"
        and ac_results.get(str(row["ac_id"])) in (AC_FAIL, AC_NOT_FOUND)
    ]


def proof_gap(
    report: Mapping[str, Any] | None, ac_rows: Iterable[Mapping[str, Any]], sha: str
) -> Gap | None:
    """None — зелёный о ``sha`` доказан; иначе Gap с причиной."""
    if report is None or not sha:
        return Gap(NO_REPORT, [f"отчёта CI о коммите {sha[:12] or '—'} нет"])
    checks = _parsed(report.get("checks"))
    ac_results = _parsed(report.get("ac_results"))
    if checks is None or ac_results is None:
        return Gap(MALFORMED, ["checks или ac_results отчёта не читаются как объект"])
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
