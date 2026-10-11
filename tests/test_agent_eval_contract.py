"""Контракт артефакта eval: версионированный набор канареек и итог прогона (#1221).

Оценщик один — ``steward_canaries.evaluate`` (#1108); здесь проверяется то,
что вокруг него: манифест с устойчивыми ID и хэшем, схема прогона и итог,
который не даёт неполным данным выглядеть зелёными. Судья везде заглушка —
результаты настоящей модели эти тесты не доказывают.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agent_eval import contract as ct  # noqa: E402
from hub.services.steward_canaries import (  # noqa: E402
    MUST_NOT_APPROVE,
    MUST_NOT_ESCALATE,
    all_canaries,
)

META = ct.RunMeta(
    commit="a" * 40,
    provider="stub",
    model="stub-judge-1",
    params={"temperature": 0},
    prompt_hash="p" * 64,
    catalog_hash="c" * 64,
    run_id="run-1",
    session_id="sess-1",
)


def _attentive_verdict(canary) -> dict:
    """Судья-заглушка: чистый пакет одобряет, всё остальное возвращает."""
    if canary.expectation == MUST_NOT_ESCALATE:
        return {"verdict": "approve", "confidence": "high"}
    return {"verdict": "changes_requested", "confidence": "high"}


def _responses(manifest, judge=_attentive_verdict, **extra):
    return [
        ct.CaseResponse(
            case_id=case.case_id,
            judgement=judge(canary),
            latency_ms=12.5,
            usage={"input_tokens": 100, "output_tokens": 20},
            **extra,
        )
        for case, canary in zip(manifest.cases, manifest.canaries, strict=True)
    ]


def _problem_kinds(run) -> set[str]:
    return {p["kind"] for p in run["problems"]}


def test_complete_manifest_result():
    """AC-1: полный набор — результат на каждый case, версии известны, итог сходится."""
    manifest = ct.build_manifest()
    run = ct.build_run(manifest, _responses(manifest), META)

    ids = [c["case_id"] for c in run["cases"]]
    assert ids == [c.case_id for c in manifest.cases]
    assert len(ids) == len(set(ids)) == len(all_canaries()) > 0
    assert run["status"] == "passed"
    assert run["problems"] == []
    # версии набора, модели и промпта
    assert run["manifest"]["suite_version"] == ct.SUITE_VERSION
    assert run["manifest"]["manifest_hash"] == manifest.manifest_hash
    assert len(manifest.manifest_hash) == 64
    assert run["meta"]["model"] == "stub-judge-1"
    assert run["meta"]["prompt_hash"] == "p" * 64
    assert run["meta"]["catalog_hash"] == "c" * 64
    assert run["meta"]["run_id"] == "run-1" and run["meta"]["session_id"] == "sess-1"
    # summary == пересчёт по per-case
    summary = run["summary"]
    assert summary["total"] == len(run["cases"])
    assert summary["passed"] == sum(c["outcome"] == "passed" for c in run["cases"])
    assert summary["passed"] == summary["total"]
    assert summary["usage"]["input_tokens"] == 100 * summary["total"]
    assert ct.validate_run(run) == []
    json.dumps(run)  # артефакт сериализуется как есть
    # критичность и ID устойчивы между сборками
    again = ct.build_manifest()
    assert again.manifest_hash == manifest.manifest_hash
    crit = {c.case_id: c.critical for c in manifest.cases}
    assert all(
        crit[c.case_id] == (c.expectation == MUST_NOT_APPROVE) for c in manifest.cases
    )


def test_manifest_hash_tracks_dataset_change():
    """Хэш манифеста меняется, когда меняется факт канарейки или её версия."""
    base = ct.build_manifest()
    assert ct.build_manifest(suite_version="999").manifest_hash != base.manifest_hash
    changed = list(all_canaries())
    first = changed[0]
    facts = copy.deepcopy(first.facts)
    facts["branch_tip"] = {"state": "present", "value": {"moved": True, "x": 1}}
    changed[0] = type(first)(
        name=first.name,
        expectation=first.expectation,
        facts=facts,
        planted=first.planted,
        origin=first.origin,
    )
    assert ct.build_manifest(changed).manifest_hash != base.manifest_hash


def test_incomplete_run_is_not_passed():
    """AC-2: пустой набор, пропавший/дублированный case, битый ответ — не passed."""
    # пустой набор
    empty = ct.build_manifest([])
    run = ct.build_run(empty, [], META)
    assert run["status"] == "incomplete"
    assert "manifest_empty" in _problem_kinds(run)

    manifest = ct.build_manifest()
    good = _responses(manifest)
    victim = manifest.cases[0].case_id

    # пропавший case
    run = ct.build_run(manifest, good[1:], META)
    assert run["status"] == "incomplete"
    missing = [p for p in run["problems"] if p["kind"] == "missing_case"]
    assert [p["case_id"] for p in missing] == [victim]
    record = next(c for c in run["cases"] if c["case_id"] == victim)
    assert record["outcome"] == "incomplete"  # case отражён, а не выпал

    # дублированный case
    run = ct.build_run(manifest, good + [good[0]], META)
    assert run["status"] == "incomplete"
    dup = [p for p in run["problems"] if p["kind"] == "duplicate_response"]
    assert [p["case_id"] for p in dup] == [victim]

    # case вне манифеста
    stray = ct.CaseResponse(case_id="stored:nope", judgement={"verdict": "approve"})
    run = ct.build_run(manifest, good + [stray], META)
    assert run["status"] == "incomplete"
    assert "unknown_case" in _problem_kinds(run)

    # битый ответ: слово вне словаря, не-словарь
    for bad in ({"verdict": "banana"}, {}, "approve", None):
        broken = [ct.CaseResponse(case_id=victim, judgement=bad)] + good[1:]
        run = ct.build_run(manifest, broken, META)
        assert run["status"] == "incomplete", bad
        probs = [p for p in run["problems"] if p["kind"] == "malformed_response"]
        assert probs and probs[0]["case_id"] == victim
        assert victim in probs[0]["path"] and "judgement" in probs[0]["path"]

    # неизвестный usage остаётся null, а не 0
    no_usage = [ct.CaseResponse(case_id=r.case_id, judgement=r.judgement) for r in good]
    run = ct.build_run(manifest, no_usage, META)
    assert run["status"] == "passed"
    assert all(c["usage"] is None and c["latency_ms"] is None for c in run["cases"])
    assert run["summary"]["usage"]["input_tokens"] is None
    assert run["summary"]["latency_ms_total"] is None

    # частично известный usage: итог не притворяется суммой
    partial = list(good)
    partial[0] = ct.CaseResponse(case_id=victim, judgement=good[0].judgement)
    run = ct.build_run(manifest, partial, META)
    assert run["summary"]["usage"]["input_tokens"] is None

    # инфраструктурная ошибка — не качество и не passed
    infra = [ct.CaseResponse(case_id=victim, judgement=None, error="timeout")]
    run = ct.build_run(manifest, infra + good[1:], META)
    assert run["status"] == "infrastructure_error"
    rec = next(c for c in run["cases"] if c["case_id"] == victim)
    assert rec["outcome"] == "infrastructure_error" and rec["error"] == "timeout"


def test_critical_false_approve_fails_run():
    """AC-3: одобренный критический дефект — quality_failed при любом среднем score."""
    manifest = ct.build_manifest()
    target = next(c for c in manifest.cases if c.critical)

    def judge(canary):
        if canary.expectation == MUST_NOT_APPROVE and (
            f"{canary.origin}:{canary.name}" == target.case_id
        ):
            return {"verdict": "approve", "confidence": "high"}
        return _attentive_verdict(canary)

    run = ct.build_run(manifest, _responses(manifest, judge), META)
    assert run["status"] == "quality_failed"
    assert run["summary"]["score"] > 0.8  # средний score высок, итог всё равно красный
    assert run["summary"]["critical_failures"] == [target.case_id]
    assert run["summary"]["false_approvals"] == 1
    assert run["summary"]["false_alarms"] == 0
    assert ct.validate_run(run) == []

    # всегда-одобряющий судья тоже красный
    run = ct.build_run(
        manifest,
        _responses(manifest, lambda _c: {"verdict": "approve", "confidence": "high"}),
        META,
    )
    assert run["status"] == "quality_failed"

    # чистый кейс отдельно ловит ложный отказ
    run = ct.build_run(
        manifest,
        _responses(
            manifest,
            lambda _c: {"verdict": "changes_requested", "confidence": "high"},
        ),
        META,
    )
    assert run["status"] == "quality_failed"
    assert run["summary"]["false_alarms"] >= 1
    assert run["summary"]["false_approvals"] == 0
    assert run["summary"]["critical_failures"] == []

    # известный провал не прячется за неполнотой данных
    partial = _responses(manifest, lambda _c: {"verdict": "approve"})[:-1]
    run = ct.build_run(manifest, partial, META)
    assert run["status"] == "quality_failed"
    assert "missing_case" in _problem_kinds(run)


def test_validate_run_detects_tampered_summary():
    manifest = ct.build_manifest()
    run = ct.build_run(manifest, _responses(manifest), META)
    forged = copy.deepcopy(run)
    forged["cases"][0]["outcome"] = "quality_failed"
    assert ct.validate_run(forged)  # итог passed при проваленном case
    dropped = copy.deepcopy(run)
    dropped["cases"].pop()
    assert ct.validate_run(dropped)


def test_meta_requires_identity_fields():
    manifest = ct.build_manifest()
    blank = ct.RunMeta(**{**META.__dict__, "commit": "", "model": ""})
    run = ct.build_run(manifest, _responses(manifest), blank)
    assert run["status"] == "incomplete"
    paths = {p["path"] for p in run["problems"] if p["kind"] == "meta_missing"}
    assert paths == {"meta.commit", "meta.model"}


def test_working_prompt_hash_follows_real_builder(monkeypatch):
    """Хэш промпта берётся у фактического builder стюарда, а не у копии."""
    from hub.services import steward_shadow

    before = ct.working_prompt_hash()
    assert len(before) == 64
    assert ct.working_prompt_hash() == before
    original = steward_shadow._prompt
    monkeypatch.setattr(
        steward_shadow, "_prompt", lambda *a, **k: original(*a, **k) + " !"
    )
    assert ct.working_prompt_hash() != before


def test_working_catalog_hash_is_stable():
    first = asyncio.run(ct.working_catalog_hash())
    assert len(first) == 64
    assert asyncio.run(ct.working_catalog_hash()) == first


def test_unknown_expectation_is_rejected_loudly():
    from hub.services.steward_canaries import Canary

    odd = Canary(name="odd", expectation="whatever", facts={}, planted="x")
    with pytest.raises(ValueError):
        ct.build_manifest([odd])


# --- Обходы, найденные ревью (Codex на ea7dcac) ---------------------------------


def _passed_run():
    manifest = ct.build_manifest()
    return manifest, ct.build_run(manifest, _responses(manifest), META)


def test_validate_rejects_forged_verdict_with_passed_outcome():
    """P1-1: исход пересчитывается evaluate по доверенной expectation."""
    manifest, run = _passed_run()
    for forged_verdict in ("approve", None):
        forged = copy.deepcopy(run)
        rec = next(c for c in forged["cases"] if c["critical"])
        rec["verdict"] = forged_verdict
        rec["outcome"] = "passed"
        assert ct.validate_run(forged, manifest), forged_verdict
    # подмена expectation/critical у записи тоже видна
    forged = copy.deepcopy(run)
    next(c for c in forged["cases"] if c["critical"])["critical"] = False
    assert ct.validate_run(forged, manifest)
    assert ct.validate_run(run, manifest) == []


def test_validate_rejects_consistent_case_removal():
    """P1-2: согласованное удаление case и пересчёт summary не проходят."""
    manifest, run = _passed_run()
    forged = copy.deepcopy(run)
    gone = forged["cases"].pop(0)["case_id"]
    forged["manifest"]["case_ids"].remove(gone)
    forged["summary"] = ct.summarize(forged["cases"])
    assert forged["status"] == "passed"
    errors = ct.validate_run(forged, manifest)
    assert errors and any("case_ids" in e or "cases" in e for e in errors)
    # хэш и версия сверяются с доверенными
    for key, value in (("manifest_hash", "0" * 64), ("suite_version", "other")):
        bad = copy.deepcopy(run)
        bad["manifest"][key] = value
        assert any(key in e for e in ct.validate_run(bad, manifest))
    # без явного манифеста — доверенный собирается из all_canaries()
    assert ct.validate_run(forged)


def test_required_identity_fields_in_build_and_validate():
    """P2-3: версия, params, meta, schema_version обязательны и в build_run, и в validate_run."""
    manifest, run = _passed_run()
    no_params = ct.RunMeta(**{**META.__dict__, "params": None})
    built = ct.build_run(manifest, _responses(manifest), no_params)
    assert built["status"] == "incomplete"
    assert "meta.params" in {p["path"] for p in built["problems"]}

    blank = ct.build_manifest(suite_version=" ")
    built = ct.build_run(blank, _responses(blank), META)
    assert built["status"] == "incomplete"
    assert "manifest_invalid" in _problem_kinds(built)

    for mutate in (
        lambda r: r.pop("meta"),
        lambda r: r["meta"].pop("model"),
        lambda r: r["meta"].__setitem__("params", None),
        lambda r: r.pop("schema_version"),
    ):
        forged = copy.deepcopy(run)
        mutate(forged)
        assert ct.validate_run(forged, manifest)


@pytest.mark.parametrize(
    "bad, field",
    [
        ({"verdict": "approve", "confidence": "maybe"}, "confidence"),
        ({"verdict": "approve", "confidence": ["high"]}, "confidence"),
        ({"verdict": []}, "verdict"),
        ({"verdict": {"a": 1}}, "verdict"),
    ],
)
def test_broken_judgement_is_a_problem_not_an_exception(bad, field):
    """P2-4: битые данные называют case и путь, исключения нет."""
    manifest = ct.build_manifest()
    victim = manifest.cases[0].case_id
    good = _responses(manifest)
    broken = [ct.CaseResponse(case_id=victim, judgement=bad)] + good[1:]
    run = ct.build_run(manifest, broken, META)
    assert run["status"] == "incomplete"
    prob = next(p for p in run["problems"] if p["kind"] == "malformed_response")
    assert prob["case_id"] == victim and field in prob["path"]


@pytest.mark.parametrize(
    "extra",
    [
        {"latency_ms": float("nan")},
        {"latency_ms": float("inf")},
        {"latency_ms": -1.0},
        {"usage": {"input_tokens": float("inf")}},
        {"usage": {"input_tokens": float("nan")}},
        {"usage": {"input_tokens": -3}},
    ],
)
def test_non_finite_numbers_are_problems(extra):
    manifest = ct.build_manifest()
    victim = manifest.cases[0].case_id
    good = _responses(manifest)
    bad = ct.CaseResponse(case_id=victim, judgement=good[0].judgement, **extra)
    run = ct.build_run(manifest, [bad] + good[1:], META)
    assert run["status"] == "incomplete"
    prob = next(p for p in run["problems"] if p["kind"] == "malformed_response")
    assert prob["case_id"] == victim
    json.dumps(run, allow_nan=False)


def test_prompt_hash_covers_delivery_block(monkeypatch):
    """P2-5: статический текст блока доступа входит в хэш промпта."""
    from hub.services import steward_shadow

    before = ct.working_prompt_hash()
    original = steward_shadow.delivery_block
    monkeypatch.setattr(
        steward_shadow,
        "delivery_block",
        lambda *a, **k: original(*a, **k) + "\nновое правило",
    )
    assert ct.working_prompt_hash() != before


def test_catalog_hash_covers_whole_tool_contract(monkeypatch):
    """P2-6: хэш каталога чувствителен к outputSchema и описанию, не к константе."""
    from hub import mcp_server
    from mcp import types

    def tool(**extra):
        return types.Tool(
            name="t", description="d", inputSchema={"type": "object"}, **extra
        )

    async def hash_for(*tools):
        async def listing(_view):
            return list(tools)

        monkeypatch.setattr(mcp_server.mcp, "list_tools_for", listing)
        return await ct.working_catalog_hash()

    base = asyncio.run(hash_for(tool()))
    with_out = asyncio.run(hash_for(tool(outputSchema={"type": "object"})))
    other_out = asyncio.run(
        hash_for(tool(outputSchema={"type": "object", "required": ["a"]}))
    )
    described = asyncio.run(
        hash_for(types.Tool(name="t", description="e", inputSchema={"type": "object"}))
    )
    assert len({base, with_out, other_out, described}) == 4


def test_manifest_hash_ignores_input_order():
    """P2-7: порядок канареек не меняет хэш."""
    forward = ct.build_manifest(all_canaries())
    backward = ct.build_manifest(list(reversed(all_canaries())))
    assert forward.manifest_hash == backward.manifest_hash
    assert [c.case_id for c in forward.cases] == [c.case_id for c in backward.cases]


def test_manifest_is_a_snapshot_of_facts():
    """P2-8: правка facts после сборки не меняет манифест; порча снимка видна."""
    source = list(all_canaries())
    manifest = ct.build_manifest(source)
    source[0].facts["injected"] = {"state": "present", "value": {}}
    assert ct.build_manifest(all_canaries()).manifest_hash == manifest.manifest_hash
    assert ct.build_run(manifest, _responses(manifest), META)["status"] == "passed"
    # порча самого снимка ловится сверкой facts_hash в build_run
    manifest.canaries[0].facts["injected"] = 1
    run = ct.build_run(manifest, _responses(manifest), META)
    assert run["status"] == "incomplete"
    assert "manifest_tampered" in _problem_kinds(run)


# --- Вердикт оценивается всегда (lite-ревью, отчёт 878) --------------------------


def _critical_case(manifest):
    return next(c.case_id for c in manifest.cases if c.critical)


def test_critical_approve_with_error_is_quality_failed():
    """error рядом с вердиктом не отменяет вердикт: approve на дефекте — провал."""
    manifest = ct.build_manifest()
    target = _critical_case(manifest)
    good = [r for r in _responses(manifest) if r.case_id != target]
    bad = ct.CaseResponse(
        case_id=target, judgement={"verdict": "approve"}, error="timeout"
    )
    run = ct.build_run(manifest, good + [bad], META)
    assert run["status"] == "quality_failed"
    assert run["summary"]["critical_failures"] == [target]
    rec = next(c for c in run["cases"] if c["case_id"] == target)
    assert rec["outcome"] == "quality_failed"
    assert rec["verdict"] == "approve" and rec["error"] == "timeout"
    assert ct.validate_run(run, manifest) == []
    # безобидный вердикт с error остаётся инфраструктурной ошибкой
    ok = ct.CaseResponse(
        case_id=target, judgement={"verdict": "escalate"}, error="timeout"
    )
    run = ct.build_run(manifest, good + [ok], META)
    assert run["status"] == "infrastructure_error"
    assert ct.validate_run(run, manifest) == []


def test_validate_rejects_outcome_downgrade_to_infrastructure():
    """Подмена quality_failed на infrastructure_error/incomplete не проходит."""
    manifest = ct.build_manifest()
    target = _critical_case(manifest)
    run = ct.build_run(
        manifest,
        _responses(manifest, lambda _c: {"verdict": "approve", "confidence": "high"}),
        META,
    )
    assert run["status"] == "quality_failed"
    for fake in ("infrastructure_error", "incomplete", "passed"):
        forged = copy.deepcopy(run)
        next(c for c in forged["cases"] if c["case_id"] == target)["outcome"] = fake
        forged["summary"] = ct.summarize(forged["cases"])
        assert ct.validate_run(forged, manifest), fake


def test_duplicate_with_critical_approve_is_quality_failed():
    manifest = ct.build_manifest()
    target = _critical_case(manifest)
    base = _responses(manifest)
    approve = ct.CaseResponse(case_id=target, judgement={"verdict": "approve"})
    run = ct.build_run(manifest, base + [approve], META)
    assert run["status"] == "quality_failed"
    assert run["summary"]["critical_failures"] == [target]
    assert "duplicate_response" in _problem_kinds(run)
    assert ct.validate_run(run, manifest) == []
