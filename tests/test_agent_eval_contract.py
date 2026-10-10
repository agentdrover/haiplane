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
