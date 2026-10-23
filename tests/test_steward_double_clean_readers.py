"""Двойная чистота: выбор пары, пакет, маршрут, готовность, политика (#1986).

Решение (AC-1…AC-6) проверяют два основных файла. Здесь - читатели, которые
обязаны согласиться с решением: фиксация пары на заказе, хеш пакета, показ
маршрута и готовности act, отчёт эффективной политики.
"""

from __future__ import annotations

import dataclasses

from hub import config
from hub import repository as repo
from hub.services import steward_double_clean as dc
from hub.services.steward_dispatch import KIND_ADVISOR, order_run
from hub.services.steward_evidence import build_evidence_packet, packet_hash
from tests.steward_double_clean_support import (
    FINDING,
    LISTED,
    OTHER,
    THIRD,
    add_report,
    build_case,
    policy_of,
)


async def _pair(db, task_id: int):
    return await dc.frozen_pair(db, task_id, 1)


async def test_the_pair_is_the_latest_listed_report_and_the_latest_other_one(
    db, monkeypatch
):
    case = await build_case(db, monkeypatch, "dcr-latest", pair=False, order=False)
    old_other = await add_report(db, case.task_id, OTHER)
    old_listed = await add_report(db, case.task_id, LISTED)
    dirty_other = await add_report(db, case.task_id, OTHER, confirmed=(FINDING,))
    new_other = await add_report(db, case.task_id, THIRD)
    new_listed = await add_report(db, case.task_id, LISTED)
    unpinned = await add_report(db, case.task_id, OTHER, pinned=False)

    assert await order_run(db, case.task_id, 1) is not None

    assert await _pair(db, case.task_id) == (new_other, new_listed)
    assert len({old_other, old_listed, dirty_other, unpinned}) == 4


async def test_no_pair_is_frozen_without_the_exception_keys(db, monkeypatch):
    case = await build_case(
        db, monkeypatch, "dcr-no-keys", policy=policy_of(clean=None), order=False
    )

    assert await order_run(db, case.task_id, 1) is not None

    assert await _pair(db, case.task_id) is None


async def test_only_the_judge_order_freezes_a_pair(db, monkeypatch):
    case = await build_case(db, monkeypatch, "dcr-kinds", order=False)
    monkeypatch.setattr(config, "STEWARD_ADVISOR_WAIT_MAX", 60)

    advisor = await order_run(db, case.task_id, 1, KIND_ADVISOR, model="m")
    judge = await order_run(db, case.task_id, 1)

    assert advisor is not None and judge is not None
    rows = await db.execute_fetchall(
        "SELECT kind, double_clean_pair FROM steward_runs WHERE task_id=?",
        (case.task_id,),
    )
    by_kind = {r["kind"]: r["double_clean_pair"] for r in rows}
    assert by_kind["advisor"] == "" and by_kind["verdict"] != ""


def _legacy_hash(packet) -> str:
    """Хеш пакета по формуле ДО #1986: тело без поля пары."""
    import hashlib
    import json

    from hub.services import steward_evidence as ev

    facts = {}
    for source in ev.PACKET_HASH_SOURCES:
        fact = packet.facts.get(source)
        facts[source] = (
            None
            if fact is None
            else {
                "state": fact.state,
                "reason": fact.reason,
                "value": {
                    k: v
                    for k, v in (fact.value or {}).items()
                    if k not in ev._PACKET_HASH_VOLATILE_KEYS
                },
            }
        )
    brief = packet.brief.model_dump(mode="json") if packet.brief else None
    body = {
        "generation": packet.generation,
        "brief": (
            {k: brief.get(k) for k in ev.PACKET_HASH_BRIEF_FIELDS}
            if brief is not None
            else None
        ),
        "facts": facts,
        "quotes": [[q.source, q.author, q.text] for q in packet.quotes],
    }
    blob = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def test_the_pair_is_part_of_the_packet_hash(db, monkeypatch):
    case = await build_case(db, monkeypatch, "dcr-hash")
    packet = await build_evidence_packet(db, case.task_id, 1)
    assert packet is not None
    assert packet.double_clean == (case.main, case.extra)

    bare = dataclasses.replace(packet, double_clean=())
    swapped = dataclasses.replace(packet, double_clean=(case.extra, case.main))
    assert len({packet_hash(packet), packet_hash(bare), packet_hash(swapped)}) == 3
    # Без пары хеш равен хешу по формуле до задачи: прежние согласия не умирают.
    assert packet_hash(bare) == _legacy_hash(bare)


async def test_the_route_keeps_the_steward_for_a_class_the_exception_can_reach(
    db, monkeypatch
):
    from hub.services.steward_shadow import mode_report
    from hub.services.verdict_route import verdict_route

    async def act(_db):
        return {**await mode_report(_db), "effective": "act"}

    monkeypatch.setattr("hub.services.steward_shadow.mode_report", act)
    case = await build_case(
        db, monkeypatch, "dcr-route", policy=policy_of(clean="r5"), order=False
    )
    route = await verdict_route(db, case.task_id)
    assert route.final == "steward", route.reason
    assert "двойной чистоте" in route.condition

    await repo.update_project(
        db,
        case.project_id,
        gate_policy='{"verdict": "steward", "steward_approve_max_class": "r1"}',
    )
    await db.commit()
    route = await verdict_route(db, case.task_id)
    assert route.final == "human" and route.code == "class_above_policy_ceiling"


async def test_readiness_does_not_call_a_double_clean_candidate_ready_or_blocked(
    db, monkeypatch
):
    from hub.services.steward_act_readiness import UNKNOWN, candidates

    case = await build_case(db, monkeypatch, "dcr-readiness")

    found = [c for c in await candidates(db) if c["task_id"] == case.task_id]

    assert found and found[0]["state"] == UNKNOWN
    assert "двойная чистота" in found[0]["reasons"][0]["detail"]


async def test_may_apply_to_class_is_a_pure_read(db, monkeypatch):
    case = await build_case(db, monkeypatch, "dcr-may", order=False)
    assert await dc.may_apply_to_class(db, case.task_id, "R3")
    assert not await dc.may_apply_to_class(db, case.task_id, "R4")
    assert not await dc.may_apply_to_class(db, case.task_id, None)
    assert not await dc.may_apply_to_class(db, case.task_id, "R9")


async def test_effective_policy_names_both_keys(db, monkeypatch):
    from hub.services.effective_policy import key_rows

    case = await build_case(db, monkeypatch, "dcr-policy", order=False)
    project = await repo.get_project(db, case.project_id)

    rows = {row["key"]: row for row in key_rows(project)}

    clean = rows["steward_approve_max_class_double_clean"]
    assert clean["value"] == "r3" and clean["source"] == "project"
    assert rows["double_clean_reviewers"]["value"] == [LISTED]
