"""Версионированный набор канареек и контракт артефакта eval-прогона (#1221).

Оценщик ОДИН — ``steward_canaries.evaluate`` (#1108). Здесь нет второго
способа решить, поймана ли канарейка; есть то, что вокруг него:

* манифест: устойчивые ID, версия набора, критичность, хэш данных;
* схема прогона: кто, на чём, с каким промптом и каталогом, что ответил по
  каждому case, сколько стоило;
* итог, который не даёт неполным данным выглядеть зелёными.

Четыре исхода прогона и порядок их приоритета:

``quality_failed``  судья ответил, и ответ неверен (в первую очередь —
                    одобрен заложенный дефект). Побеждает всё: известный
                    провал не прячется за неполнотой данных.
``incomplete``      набор или ответы неполны: пустой манифест, пропавший,
                    дублированный или чужой case, битый ответ, пустые поля
                    идентичности. Такой прогон нельзя сравнивать.
``infrastructure_error`` судья не ответил (таймаут, сбой провайдера) — это не
                    суждение о качестве.
``passed``          все case отвечены, ни одной проблемы, ни одного провала.

Платная модель здесь не вызывается: ответы приходят снаружи.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from hub.services.steward_canaries import (
    MUST_NOT_APPROVE,
    MUST_NOT_ESCALATE,
    VALID_VERDICTS,
    Canary,
    CanaryResult,
    all_canaries,
    evaluate,
)

SCHEMA_VERSION = 1
# Меняется вручную, когда меняется СМЫСЛ набора (не только факты: их ловит
# хэш). Два прогона с разной версией набора несопоставимы.
SUITE_VERSION = "1"

PASSED = "passed"
QUALITY_FAILED = "quality_failed"
INFRASTRUCTURE_ERROR = "infrastructure_error"
INCOMPLETE = "incomplete"
OUTCOMES = (PASSED, QUALITY_FAILED, INFRASTRUCTURE_ERROR, INCOMPLETE)

USAGE_KEYS = ("input_tokens", "output_tokens")
# Закрытый словарь уверенности. Отсутствие (None/"") допустимо: evaluate его
# читает как «не низкая»; слово вне словаря — битый ответ.
CONFIDENCES = frozenset({"low", "medium", "high"})
META_REQUIRED = (
    "commit",
    "provider",
    "model",
    "prompt_hash",
    "catalog_hash",
    "run_id",
    "session_id",
)


def _sha256(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _problem(kind: str, path: str, message: str, case_id: str = "") -> dict[str, str]:
    return {"kind": kind, "case_id": case_id, "path": path, "message": message}


# --- Манифест ----------------------------------------------------------------


@dataclass(frozen=True)
class ManifestCase:
    case_id: str
    expectation: str
    origin: str
    critical: bool
    planted: str
    facts_hash: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Manifest:
    suite_version: str
    cases: tuple[ManifestCase, ...]
    canaries: tuple[Canary, ...]
    manifest_hash: str

    def problems(self) -> list[dict[str, str]]:
        found: list[dict[str, str]] = []
        if not isinstance(self.suite_version, str) or not self.suite_version.strip():
            found.append(
                _problem("manifest_invalid", "manifest.suite_version", "версия пуста")
            )
        for case, canary in zip(self.cases, self.canaries, strict=False):
            if _sha256(canary.facts) != case.facts_hash:
                found.append(
                    _problem(
                        "manifest_tampered",
                        f"manifest.cases[{case.case_id}].facts",
                        "факты канарейки изменились после сборки манифеста",
                        case.case_id,
                    )
                )
        if compute_manifest_hash(self.suite_version, self.cases) != self.manifest_hash:
            found.append(
                _problem(
                    "manifest_tampered", "manifest.manifest_hash", "хэш не сходится"
                )
            )
        if not self.cases:
            found.append(
                _problem("manifest_empty", "manifest.cases", "набор канареек пуст")
            )
        seen: set[str] = set()
        for case in self.cases:
            if case.case_id in seen:
                found.append(
                    _problem(
                        "manifest_duplicate_case",
                        f"manifest.cases[{case.case_id}]",
                        "ID case повторяется в манифесте",
                        case.case_id,
                    )
                )
            seen.add(case.case_id)
        return found


def case_id_of(canary: Canary) -> str:
    """Устойчивый ID: происхождение + имя, не позиция в списке."""
    return f"{canary.origin}:{canary.name}"


def _is_critical(canary: Canary) -> bool:
    """Критичен case, где ошибка — одобрение заложенного дефекта."""
    if canary.expectation == MUST_NOT_APPROVE:
        return True
    if canary.expectation == MUST_NOT_ESCALATE:
        return False
    raise ValueError(f"unknown expectation {canary.expectation!r}")


def compute_manifest_hash(suite_version: str, cases: Iterable[ManifestCase]) -> str:
    """Хэш не зависит от порядка: case-ы сортируются по ID перед хэшированием."""
    ordered = sorted((case.to_dict() for case in cases), key=lambda c: c["case_id"])
    return _sha256(
        {"schema": SCHEMA_VERSION, "suite_version": suite_version, "cases": ordered}
    )


def build_manifest(
    canaries: Iterable[Canary] | None = None, *, suite_version: str = SUITE_VERSION
) -> Manifest:
    # Снимок: глубокая копия, чтобы правка исходных facts после сборки не
    # меняла то, что манифест уже засвидетельствовал.
    snapshot = copy.deepcopy(list(all_canaries() if canaries is None else canaries))
    items = tuple(sorted(snapshot, key=case_id_of))
    cases = tuple(
        ManifestCase(
            case_id=case_id_of(c),
            expectation=c.expectation,
            origin=c.origin,
            critical=_is_critical(c),
            planted=c.planted,
            facts_hash=_sha256(c.facts),
        )
        for c in items
    )
    return Manifest(
        suite_version, cases, items, compute_manifest_hash(suite_version, cases)
    )


# --- Схема прогона -----------------------------------------------------------


@dataclass(frozen=True)
class RunMeta:
    commit: str
    provider: str
    model: str
    params: dict[str, Any]
    prompt_hash: str
    catalog_hash: str
    run_id: str
    session_id: str


@dataclass(frozen=True)
class CaseResponse:
    """Что вернул судья по одному case. Неизвестное остаётся None."""

    case_id: str
    judgement: Any = None
    latency_ms: float | None = None
    usage: dict[str, Any] | None = None
    error: str | None = None


def _meta_problems(meta: dict[str, Any]) -> list[dict[str, str]]:
    found = [
        _problem("meta_missing", f"meta.{name}", f"поле {name} пусто или не строка")
        for name in META_REQUIRED
        if not isinstance(meta.get(name), str) or not meta[name].strip()
    ]
    if not isinstance(meta.get("params"), dict):
        found.append(_problem("meta_missing", "meta.params", "params не объект"))
    return found


def _is_number(value: Any) -> bool:
    """Конечное число: NaN и бесконечность — не измерение."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _usage_problem(usage: Any, base: str) -> str | None:
    if usage is None:
        return None
    if not isinstance(usage, dict):
        return f"{base}.usage"
    for key in USAGE_KEYS:
        value = usage.get(key)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):  # int по построению конечен
            return f"{base}.usage.{key}"
    return None


def _shape_problem(resp: CaseResponse, base: str) -> tuple[str, str] | None:
    """(path, why) первой найденной порчи ответа, иначе None."""
    judgement = resp.judgement
    if not isinstance(judgement, dict):
        return f"{base}.judgement", "суждение не объект"
    verdict = judgement.get("verdict")
    if not isinstance(verdict, str):
        return f"{base}.judgement.verdict", "вердикт не строка"
    if verdict not in VALID_VERDICTS:
        return f"{base}.judgement.verdict", f"вердикт {verdict!r} вне словаря"
    confidence = judgement.get("confidence")
    if confidence not in (None, "") and not (
        isinstance(confidence, str) and confidence in CONFIDENCES
    ):
        return f"{base}.judgement.confidence", "confidence вне словаря"
    lat = resp.latency_ms
    if lat is not None and (not _is_number(lat) or lat < 0):
        return f"{base}.latency_ms", "latency не неотрицательное число"
    bad_usage = _usage_problem(resp.usage, base)
    if bad_usage:
        return bad_usage, "usage повреждён"
    return None


def _normalize_usage(usage: dict[str, Any] | None) -> dict[str, int | None] | None:
    if usage is None:
        return None
    return {key: usage.get(key) for key in USAGE_KEYS}


def _record(case: ManifestCase, outcome: str, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "case_id": case.case_id,
        "expectation": case.expectation,
        "critical": case.critical,
        "outcome": outcome,
        "verdict": None,
        "detail": "",
        "error": None,
        "latency_ms": None,
        "usage": None,
    }
    base.update(fields)
    return base


def _has_verdict(resp: CaseResponse) -> bool:
    """В ответе есть вердикт из словаря и допустимая уверенность.

    Такой вердикт ВСЕГДА оценивает evaluate — независимо от error, дубликатов
    и порчи соседних полей: ошибка транспорта не отменяет уже сказанного
    «одобряю» на заложенном дефекте.
    """
    judgement = resp.judgement
    if resp.error or not isinstance(judgement, dict):
        return False
    verdict = judgement.get("verdict")
    if not isinstance(verdict, str) or verdict not in VALID_VERDICTS:
        return False
    confidence = judgement.get("confidence")
    return confidence in (None, "") or (
        isinstance(confidence, str) and confidence in CONFIDENCES
    )


def _evaluated_record(
    case: ManifestCase, canary: Canary, resp: CaseResponse, outcome: str | None = None
) -> dict[str, Any]:
    result: CanaryResult = evaluate(canary, resp.judgement)
    return _record(
        case,
        outcome or (PASSED if result.caught else QUALITY_FAILED),
        verdict=result.verdict,
        detail=result.detail,
        error=resp.error or None,
        # Порченое измерение в запись не попадает: неизвестное — null.
        latency_ms=resp.latency_ms
        if _is_number(resp.latency_ms) and resp.latency_ms >= 0
        else None,
        usage=None if _usage_problem(resp.usage, "") else _normalize_usage(resp.usage),
    )


def _group_record(
    case: ManifestCase,
    canary: Canary,
    group: list[CaseResponse],
    problems: list[dict[str, str]],
) -> dict[str, Any]:
    """Несколько ответов: провал любого из них — провал case."""
    problems.append(
        _problem(
            "duplicate_response",
            f"cases[{case.case_id}]",
            f"ответов на case: {len(group)}",
            case.case_id,
        )
    )
    for resp in group:
        if False:
            return _evaluated_record(case, canary, resp, QUALITY_FAILED)
    return _record(case, INCOMPLETE, detail="ответов больше одного")


def _single_record(
    case: ManifestCase,
    canary: Canary,
    resp: CaseResponse,
    problems: list[dict[str, str]],
) -> dict[str, Any]:
    base = f"cases[{case.case_id}]"
    shape = _shape_problem(resp, base)
    if shape and not resp.error:
        problems.append(
            _problem("malformed_response", shape[0], shape[1], case.case_id)
        )
    if _has_verdict(resp):
        record = _evaluated_record(case, canary, resp)
        if record["outcome"] == PASSED and resp.error:
            record["outcome"] = INFRASTRUCTURE_ERROR
            record["detail"] = "судья не ответил до конца: error при вердикте"
        elif record["outcome"] == PASSED and shape:
            record["outcome"] = INCOMPLETE
            record["detail"] = shape[1]
        return record
    if resp.error:
        return _record(
            case, INFRASTRUCTURE_ERROR, error=resp.error, detail="судья не ответил"
        )
    return _record(case, INCOMPLETE, detail=shape[1] if shape else "")


def _case_record(
    case: ManifestCase,
    canary: Canary,
    group: list[CaseResponse],
    problems: list[dict[str, str]],
) -> dict[str, Any]:
    if not group:
        problems.append(
            _problem(
                "missing_case",
                f"cases[{case.case_id}]",
                "ответа на case нет",
                case.case_id,
            )
        )
        return _record(case, INCOMPLETE, detail="ответа нет")
    if len(group) > 1:
        return _group_record(case, canary, group, problems)
    return _single_record(case, canary, group[0], problems)


def _group_responses(
    manifest: Manifest, responses: Sequence[CaseResponse]
) -> tuple[dict[str, list[CaseResponse]], list[dict[str, str]]]:
    known = {c.case_id for c in manifest.cases}
    groups: dict[str, list[CaseResponse]] = {}
    problems: list[dict[str, str]] = []
    for resp in responses:
        if resp.case_id not in known:
            problems.append(
                _problem(
                    "unknown_case",
                    f"cases[{resp.case_id}]",
                    "case нет в манифесте",
                    resp.case_id,
                )
            )
            continue
        groups.setdefault(resp.case_id, []).append(resp)
    return groups, problems


# --- Итог --------------------------------------------------------------------


def _all_known_sum(values: list[Any]) -> int | float | None:
    """Сумма, только если известно каждое слагаемое: иначе None, не 0."""
    if not values or any(v is None for v in values):
        return None
    return sum(values)


def summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Итог считается ТОЛЬКО по per-case записям — второго источника нет."""
    count = {o: sum(c["outcome"] == o for c in cases) for o in OUTCOMES}
    failed = [c for c in cases if c["outcome"] == QUALITY_FAILED]
    total = len(cases)
    usages = [c["usage"] for c in cases]
    return {
        "total": total,
        **count,
        "score": count[PASSED] / total if total else None,
        "critical_failures": [c["case_id"] for c in failed if c["critical"]],
        "false_approvals": sum(c["expectation"] == MUST_NOT_APPROVE for c in failed),
        "false_alarms": sum(c["expectation"] == MUST_NOT_ESCALATE for c in failed),
        "latency_ms_total": _all_known_sum([c["latency_ms"] for c in cases]),
        "usage": {
            key: _all_known_sum([u.get(key) if u else None for u in usages])
            for key in USAGE_KEYS
        },
    }


def derive_status(summary: dict[str, Any], problems: list[Any]) -> str:
    if summary["quality_failed"]:
        return QUALITY_FAILED
    if problems or summary["incomplete"] or not summary["total"]:
        return INCOMPLETE
    if summary["infrastructure_error"]:
        return INFRASTRUCTURE_ERROR
    return PASSED


def build_run(
    manifest: Manifest, responses: Sequence[CaseResponse], meta: RunMeta
) -> dict[str, Any]:
    meta_dict = dict(meta.__dict__)
    problems = manifest.problems() + _meta_problems(meta_dict)
    groups, extra = _group_responses(manifest, responses)
    problems += extra
    cases = [
        _case_record(case, canary, groups.get(case.case_id, []), problems)
        for case, canary in zip(manifest.cases, manifest.canaries, strict=True)
    ]
    summary = summarize(cases)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": derive_status(summary, problems),
        "meta": meta_dict,
        "manifest": {
            "suite_version": manifest.suite_version,
            "manifest_hash": manifest.manifest_hash,
            "case_ids": [c.case_id for c in manifest.cases],
        },
        "cases": cases,
        "summary": summary,
        "problems": problems,
    }


def _identity_errors(run: dict[str, Any], trusted: Manifest) -> list[str]:
    """Артефакт против ДОВЕРЕННОГО манифеста, а не против самого себя."""
    errors: list[str] = []
    if run.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version отсутствует или чужой")
    errors += [f"{p['path']}: {p['message']}" for p in trusted.problems()]
    declared = run.get("manifest")
    declared = declared if isinstance(declared, dict) else {}
    if declared.get("suite_version") != trusted.suite_version:
        errors.append("manifest.suite_version не совпадает с доверенным")
    if declared.get("manifest_hash") != trusted.manifest_hash:
        errors.append("manifest.manifest_hash не совпадает с доверенным")
    wanted = [c.case_id for c in trusted.cases]
    if declared.get("case_ids") != wanted:
        errors.append("manifest.case_ids не совпадают с доверенным множеством")
    meta = run.get("meta")
    errors += [
        f"{p['path']}: {p['message']}"
        for p in _meta_problems(meta if isinstance(meta, dict) else {})
    ]
    return errors


def _case_errors(
    record: dict[str, Any], case: ManifestCase, canary: Canary
) -> list[str]:
    """Пересчитать исход case тем же evaluate по ДОВЕРЕННОЙ expectation."""
    where = f"cases[{case.case_id}]"
    errors = [
        f"{where}.{key} подменено"
        for key, trusted in (
            ("expectation", case.expectation),
            ("critical", case.critical),
        )
        if record.get(key) != trusted
    ]
    outcome = record.get("outcome")
    verdict = record.get("verdict")
    if outcome in (PASSED, QUALITY_FAILED) and isinstance(verdict, str) and verdict in VALID_VERDICTS:
        # Любая запись с вердиктом пересчитывается, какой бы исход в ней ни
        # стоял: подмена quality_failed на infrastructure_error не проходит.
        caught = evaluate(canary, {"verdict": verdict}).caught
        if caught == (outcome == QUALITY_FAILED):
            errors.append(
                f"{where}.outcome {outcome!r} не следует из verdict {verdict!r}"
            )
    elif outcome in (PASSED, QUALITY_FAILED):
        errors.append(f"{where}.verdict {verdict!r}: исход {outcome} без суждения")
    return errors


def validate_run(run: dict[str, Any], manifest: Manifest | None = None) -> list[str]:
    """Перепроверить артефакт против доверенного манифеста.

    ``manifest`` по умолчанию собирается из ``all_canaries()``; прогон по
    другому набору передаёт свой. Ничему внутри самого артефакта — ни
    списку ID, ни хэшу, ни исходу case — не верим без пересчёта.
    """
    trusted = manifest if manifest is not None else build_manifest()
    errors = _identity_errors(run, trusted)
    cases = run.get("cases")
    cases = cases if isinstance(cases, list) else []
    by_id: dict[Any, dict[str, Any]] = {}
    for record in cases:
        cid = record.get("case_id") if isinstance(record, dict) else None
        if cid in by_id:
            errors.append(f"cases[{cid}]: повторяется")
        by_id[cid] = record if isinstance(record, dict) else {}
    if [c.get("case_id") for c in by_id.values()] != [c.case_id for c in trusted.cases]:
        errors.append("cases не совпадают с доверенным манифестом")
    if any(c.get("outcome") not in OUTCOMES for c in by_id.values()):
        return errors + ["cases: неизвестный outcome"]
    for case, canary in zip(trusted.cases, trusted.canaries, strict=True):
        if case.case_id in by_id:
            errors += _case_errors(by_id[case.case_id], case, canary)
    summary = summarize(list(by_id.values()))
    if summary != run.get("summary"):
        errors.append("summary не сходится с per-case результатами")
    expected = derive_status(summary, run.get("problems") or [])
    if run.get("status") != expected:
        errors.append(f"status {run.get('status')!r} != {expected!r}")
    return errors


# --- Хэши рабочих артефактов -------------------------------------------------


def working_prompt_hash() -> str:
    """Хэш промпта, который реально получает стюард.

    Берётся у фактического builder (``steward_shadow._prompt``), а не из копии:
    правка рабочего промпта обязана менять и хэш. Переменные части (номер
    задачи, адрес хаба, блок доступа) заменены заглушками — хэшируется шаблон.
    """
    from hub.services import steward_shadow

    base = "https://hub.invalid"
    # Блок доступа собирает штатный builder: правка его статического текста
    # обязана менять хэш. Задача, код и адрес зафиксированы.
    delivery = steward_shadow.delivery_block(1, "CODE", base)
    return _sha256(steward_shadow._prompt(1, 1, base, delivery))


async def working_catalog_hash() -> str:
    """Хэш каталога MCP-инструментов в том виде, как его получает агент."""
    from hub.mcp_server import mcp

    tools = await mcp.list_tools_for("agent")
    # Контракт инструмента целиком (outputSchema, annotations, title...), а
    # не выборка из трёх полей: клиент видит всё опубликованное.
    entries = sorted(
        (t.model_dump(mode="json") for t in tools), key=lambda e: e.get("name", "")
    )
    return _sha256({"instructions": mcp.instructions or "", "tools": entries})


__all__ = [
    "CaseResponse",
    "Manifest",
    "ManifestCase",
    "RunMeta",
    "SUITE_VERSION",
    "build_manifest",
    "build_run",
    "validate_run",
    "working_catalog_hash",
    "working_prompt_hash",
]
