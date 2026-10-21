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

from hub.services import ci_trust
from hub.services.ci_contract import CiContract
from hub.services.ac_tests import FAIL as AC_FAIL, NOT_FOUND as AC_NOT_FOUND
from hub.services.ci_report import AC_STATUSES, CHECK_FAIL, CHECK_OUTCOMES
from hub.services.test_locator import parse_test_locator
from hub.services.validation_run import PASS as VALIDATION_PASS

NO_REPORT = "no_report"
UNVERIFIED = "unverified"
NOT_GREEN = "not_green"
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


# #1988: the reasons scripts/ci_report_to_hub.py writes when the collector itself
# failed (it could not read the task card, or crashed). Every other "unknown"
# validation has its own reason in the same field (prose in validation_commands,
# a timeout, the deadline) and says nothing about missing results.
COLLECTOR_FAILURE_PREFIXES = ("card_read_failed:", "collector_crashed:")


def _collector_failure(status: str, report: Mapping[str, Any]) -> str:
    """The collector's failure reason when the report is its failure result, else "".

    It comes from the CI artifact (data, not instruction): flattened to one
    line, clipped, never trusted.
    """
    if status != "unknown":
        return ""
    why = " ".join(str(report.get("reason") or "").split())[:200]
    return why if why.startswith(COLLECTOR_FAILURE_PREFIXES) else ""


def _no_results_text(why: str) -> str:
    return f"CI-отчёт без результатов: сборщик не дал результатов ({why})"


def _not_pass_text(status: str, report: Mapping[str, Any]) -> str:
    why = _collector_failure(status, report)
    if why:
        return _no_results_text(why) + "; нужен validation_status=pass"
    return f"validation_status={status or 'пусто'}, нужен pass"


def proof_gap(
    report: Mapping[str, Any] | None,
    ac_rows: Iterable[Mapping[str, Any]],
    sha: str,
    contract: CiContract,
) -> Gap | None:
    """None — зелёный о ``sha`` доказан; иначе Gap с причиной.

    ``contract`` — контракт CI проекта задачи (#1848): отчёт подтверждён только
    под ним, и «зелёный» значит успех прогона и ``pass`` его обязательных проверок.
    """
    if report is None or not sha:
        return Gap(NO_REPORT, [f"отчёта CI о коммите {sha[:12] or '—'} нет"])
    # #1687: a report GitHub did not confirm is not evidence of anything. The
    # row of a caller that never says (a plain dict) is unverified too.
    trust, why = ci_trust.trust_of(report, contract.fingerprint)
    if trust != ci_trust.VERIFIED:
        return Gap(UNVERIFIED, [ci_trust.unverified_note(why)])
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
    status = str(report.get("validation_status") or "").strip()
    if red:
        # #1988: a collector that failed reports no AC results at all, so the
        # missing results are the symptom; say the cause first.
        why = _collector_failure(status, report)
        if why:
            red.insert(0, _no_results_text(why))
        return Gap(RED, red)
    if status != VALIDATION_PASS:
        return Gap(VALIDATION_NOT_PASS, [_not_pass_text(status, report)])
    # #1687: green is the run ending in success with lint, format, types and
    # tests each pass; an absent key or a skip is not a pass.
    not_green = ci_trust.green_gap(report, contract)
    if not_green:
        return Gap(NOT_GREEN, [not_green])
    return None


def warning_text(sha: str, gap: Gap) -> str:
    """Одна запись в карточку при warn."""
    return (
        f"{ALERT_HEADER} (режим warn, сдача принята; коммит {sha[:12] or '—'}, "
        f"причина {gap.cause}):\n— " + "\n— ".join(gap.violations)
    )
