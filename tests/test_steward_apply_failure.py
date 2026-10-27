"""Сбой применения approve стюарда: событие, названная причина, маршрут к человеку (#2051).

Сценарий с маршрутом и путём ``/api/events`` - в ``test_steward_dispatch.py``
(тест критерия приёмки); здесь читатели потолка и граница «сбой проверки» /
«класс выше потолка».
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from hub import config
from hub import repository as repo
from hub.db import fetchall


async def _poll_with_a_ceiling_stage(db, monkeypatch, stage: str, replacement):
    """Поллер применяет approve, пока ``stage`` проверки потолка подменён ``replacement``."""
    from hub.services import steward_advisor, steward_apply
    from tests.steward_double_clean_support import build_case, policy_of

    case = await build_case(
        db,
        monkeypatch,
        f"apply-failed-{stage}",
        policy=policy_of("r2", None, None),
        risk="R2",
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(steward_apply, stage, replacement)
        assert await steward_advisor.apply_advisor_outcomes(db) == 1
    row = dict(await repo.get_steward_judgement(db, case.task_id, 1, "verdict"))
    events = await fetchall(
        db,
        "SELECT payload FROM events WHERE kind='steward_apply_failed' AND task_id=?",
        (case.task_id,),
    )
    return row, [json.loads(dict(e)["payload"]) for e in events]


async def test_the_early_ceiling_check_failure_is_named_like_the_late_one(
    db: aiosqlite.Connection, monkeypatch
):
    """#2051: исключение ранней проверки потолка - тот же код ``ceiling_check_failed``."""

    async def breaks(*_args, **_kwargs):
        raise PermissionError("early read of the policy")

    row, events = await _poll_with_a_ceiling_stage(
        db, monkeypatch, "ceiling_refusals", breaks
    )

    assert row["advisor_outcome"] == "failed"
    assert row["advisor_outcome_detail"].startswith("ceiling_check_failed: ")
    assert [e["exception_type"] for e in events] == ["PermissionError"]
    assert "early read of the policy" in events[0]["exception_detail"]


async def test_a_class_above_the_ceiling_keeps_its_own_code_in_the_failure(
    db: aiosqlite.Connection, monkeypatch
):
    """#2051: отказ по классу выше потолка в транзакции записи - свой код, не сбой проверки."""

    async def refuses(*_args, **_kwargs):
        return (
            "class_above_policy_ceiling",
            "steward_approve_max_class=r1: выше потолка",
        )

    row, events = await _poll_with_a_ceiling_stage(
        db, monkeypatch, "ceiling_gate", refuses
    )

    assert row["advisor_outcome"] == "failed"
    assert row["advisor_outcome_detail"].startswith("class_above_policy_ceiling: ")
    assert "ceiling_check_failed" not in row["advisor_outcome_detail"]
    assert [e["code"] for e in events] == ["class_above_policy_ceiling"]
    assert events[0]["exception_type"] == "HTTPException"


async def test_a_gate_refusal_inside_the_ceiling_check_is_not_a_check_failure(
    db: aiosqlite.Connection, monkeypatch
):
    """#2051: отказ ворот, брошенный внутри проверки потолка, не превращается в сбой проверки."""
    from fastapi import HTTPException

    async def refuses(*_args, **_kwargs):
        raise HTTPException(409, "generation_moved: поколение уже другое")

    row, events = await _poll_with_a_ceiling_stage(
        db, monkeypatch, "ceiling_gate", refuses
    )

    assert row["advisor_outcome_detail"].startswith("generation_moved: ")
    # #2054, B-2: гонка 409 - не сбой применения: исход skipped, события отказа нет.
    assert row["advisor_outcome"] == "skipped"
    assert events == []


async def _current_ceiling_breaks(monkeypatch) -> None:
    from hub.services import steward_apply

    async def breaks(*_args, **_kwargs):
        raise FileNotFoundError("/srv/clone-gone/policy.json")

    monkeypatch.setattr(steward_apply, "current_ceiling", breaks)


async def test_a_ceiling_read_failure_is_named_by_every_reader(
    db: aiosqlite.Connection, monkeypatch
):
    """#2051: сам ``current_ceiling`` бросает - применение, событие и маршрут называют ``ceiling_check_failed``."""
    from hub.services.steward_shadow import mode_report
    from hub.services.verdict_route import verdict_route
    from tests.steward_double_clean_support import build_case, policy_of

    async def act(_db):
        return {**await mode_report(_db), "effective": "act"}

    monkeypatch.setattr("hub.services.steward_shadow.mode_report", act)
    case = await build_case(
        db,
        monkeypatch,
        "ceiling-read-fails",
        policy=policy_of("r2", None, None),
        risk="R2",
    )
    await _current_ceiling_breaks(monkeypatch)

    route = await verdict_route(db, case.task_id)
    assert (route.decider, route.final) == ("human", "human"), route
    assert route.code == "ceiling_check_failed"
    assert "clone-gone" not in route.reason

    from hub.services import steward_advisor

    assert await steward_advisor.apply_advisor_outcomes(db) == 1
    row = dict(await repo.get_steward_judgement(db, case.task_id, 1, "verdict"))
    assert row["advisor_outcome"] == "failed"
    assert row["advisor_outcome_detail"].startswith("ceiling_check_failed: ")
    events = await fetchall(
        db,
        "SELECT payload FROM events WHERE kind='steward_apply_failed' AND task_id=?",
        (case.task_id,),
    )
    payload = json.loads(dict(events[0])["payload"])
    assert payload["code"] == "ceiling_check_failed"
    assert payload["exception_type"] == "FileNotFoundError"


async def test_the_route_with_the_autopilot_on_does_not_raise_when_the_ceiling_read_fails(
    db: aiosqlite.Connection, monkeypatch
):
    """#2051: автопилот читает свой потолок тем же читателем - маршрут возвращает человека, не падает."""
    from hub.services.verdict_route import verdict_route
    from tests.steward_double_clean_support import build_case

    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    case = await build_case(
        db,
        monkeypatch,
        "autopilot-ceiling-read-fails",
        policy={"verdict": "auto", "auto_approve_max_class": "r1"},
        risk="R1",
        order=False,
    )
    await _current_ceiling_breaks(monkeypatch)

    route = await verdict_route(db, case.task_id)

    assert (route.decider, route.final) == ("human", "human"), route
    assert route.code == "ceiling_check_failed", route


async def test_the_transaction_and_the_advisor_skip_read_the_ceiling_through_the_wrapper(
    db: aiosqlite.Connection, monkeypatch
):
    """#2051: каждый читатель потолка получает ``CeilingUnavailable``, а не сырое исключение."""
    from hub.services import steward_advisor_skip, steward_apply
    from tests.steward_double_clean_support import build_case, policy_of

    case = await build_case(
        db,
        monkeypatch,
        "ceiling-readers",
        policy=policy_of("r2", None, None),
        risk="R2",
    )
    await _current_ceiling_breaks(monkeypatch)
    task = dict(await repo.get_task(db, case.task_id))

    with pytest.raises(steward_apply.CeilingUnavailable) as late:
        await steward_apply.ceiling_gate(db, case.task_id)
    with pytest.raises(steward_apply.CeilingUnavailable) as skip:
        await steward_advisor_skip._ceiling(db, task)

    for caught in (late, skip):
        assert caught.value.code == "ceiling_check_failed"
        assert isinstance(caught.value.__cause__, FileNotFoundError)
        assert "clone-gone" not in caught.value.reason
