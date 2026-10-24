"""Владелец вердикта пишется исходом, а не пересчитывается при показе (#2052).

Четыре сцены: исход, которым кончается роль стюарда (AC-1); прогон без суждения
(AC-2); записанный владелец сильнее стойки автопилота (AC-3); сторож «забыли
записать» (AC-4). Сцены настоящие: исходы пишут те же функции, что в поллере.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub.db import fetchall
from hub.services import inbox_decisions
from hub.services.verdict_route import verdict_route
from tests.steward_double_clean_support import build_case, policy_of
from tests.test_steward_shadow import _advisor_run, _judge, _mark_started
from tests.test_verdict_route import _ready, _steward_on

EVENT = "verdict_owner_changed"


async def _owner(db: aiosqlite.Connection, task_id: int) -> dict:
    return dict(await repo.get_task(db, task_id))


async def _events(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    rows = await fetchall(
        db,
        "SELECT payload FROM events WHERE kind=? AND task_id=? ORDER BY id",
        (EVENT, task_id),
    )
    return [json.loads(dict(r)["payload"]) for r in rows]


async def _in_inbox(db: aiosqlite.Connection, task_id: int) -> bool:
    queue = (await inbox_decisions.collect(db, {}))["queue"]
    return any(e["task_id"] == task_id and e["action"] == "verdict" for e in queue)


async def _assert_handed_to_a_human(
    db: aiosqlite.Connection, task_id: int, code: str
) -> None:
    """Владелец, одно событие, маршрут и входящие говорят одно и то же."""
    task = await _owner(db, task_id)
    assert task["verdict_owner"] == "human", task["verdict_owner_reason"]
    assert task["verdict_owner_reason"].startswith(code), task["verdict_owner_reason"]
    assert task["verdict_owner_generation"] == task["submission_generation"]
    events = await _events(db, task_id)
    assert len(events) == 1, events
    assert events[0]["owner"] == "human" and events[0]["generation"] == 1
    route = await verdict_route(db, task_id)
    assert (route.decider, route.final) == ("human", "human"), route
    assert route.code == code
    assert await _in_inbox(db, task_id)


async def _after_resubmission_the_owner_is_empty(
    db: aiosqlite.Connection, task_id: int
) -> None:
    from hub.services.verdict_owner import written_owner

    await repo.update_task(db, task_id, submission_generation=2)
    await db.commit()
    assert written_owner(await _owner(db, task_id)) == ("", "")


async def _approved_case(db, monkeypatch, slug: str, concur: str | None = "concur"):
    return await build_case(
        db,
        monkeypatch,
        slug,
        policy=policy_of("r2", None, None),
        risk="R2",
        concur=concur,
    )


async def _apply_outcome(db: aiosqlite.Connection, case) -> str:
    from hub.services import steward_advisor

    assert await steward_advisor.apply_advisor_outcomes(db) == 1
    row = dict(await repo.get_steward_judgement(db, case.task_id, 1, "verdict"))
    return row["advisor_outcome"]


async def _make_outcome(db, monkeypatch, scene: str):
    """Подводит сцену к исходу ``scene``; возвращает ``(случай, ожидаемый код)``."""
    from hub.services import steward_apply, steward_applied

    if scene == "ladder_surface":
        # привратник применения возражает (ladder) - советник согласен
        case = await _approved_case(db, monkeypatch, "own-ladder")

        async def ladder(*_a, **_k):
            return [("ladder_surface", "дифф трогает поверхности гейтов")]

        monkeypatch.setattr(steward_apply, "apply_refusals", ladder)
        assert await _apply_outcome(db, case) == "escalated"
        return case, "ladder_surface"
    if scene == "not_converged":
        # восемь признаков не сошлись: ``_hand_to_the_human_because``
        case = await _approved_case(db, monkeypatch, "own-converged")

        async def missing(*_a, **_k):
            return steward_applied.SelfApproval(missing=(("ci_green", "CI красный"),))

        monkeypatch.setattr(steward_applied, "self_approval_for", missing)
        assert await _apply_outcome(db, case) == "escalated"
        return case, "self_approval_not_converged"
    if scene == "daily_cap":
        case = await build_case(
            db,
            monkeypatch,
            "own-cap",
            policy=policy_of("r2", None, None),
            risk="R2",
            order=False,
        )
        from hub import config
        from hub.services.steward_dispatch import order_run

        monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 0)
        assert await order_run(db, case.task_id, 1) is None
        return case, "steward_daily_cap"
    if scene == "advisor_skipped":
        from hub.services import steward_advisor_skip

        case = await _approved_case(db, monkeypatch, "own-skip", concur=None)
        assert await steward_advisor_skip.record_skip(
            db, case.task_id, 1, "ci_integrity", "целостность CI"
        )
        await db.commit()
        return case, "steward_advisor_skipped"
    if scene == "apply_interrupted":
        from hub.services import steward_advisor

        case = await _approved_case(db, monkeypatch, "own-interrupted")
        await db.execute(
            "UPDATE steward_judgements SET advisor_outcome='applying', "
            "advisor_claimed_at=datetime('now','-1 day') WHERE task_id=?",
            (case.task_id,),
        )
        await db.commit()
        assert await steward_advisor._escalate_interrupted(db) == 1
        return case, "advisor_apply_interrupted"
    if scene == "autopilot_escalated":
        from hub.services import auto_verdict

        case = await _approved_case(db, monkeypatch, "own-autopilot", concur=None)
        await auto_verdict._escalate(db, case.task_id, "красный CI")
        return case, "autopilot_escalated"
    if scene == "advisor_object":
        case = await _approved_case(db, monkeypatch, "own-object", concur="object")
        assert await _apply_outcome(db, case) == "escalated"
        return case, "advisor_not_concurred"
    if scene == "skipped":
        case = await _approved_case(db, monkeypatch, "own-skipped")

        async def nothing(*_a, **_k):
            return None

        monkeypatch.setattr(steward_applied, "apply_self_approval", nothing)
        assert await _apply_outcome(db, case) == "skipped"
        return case, "advisor_outcome_skipped"
    if scene == "failed":
        case = await _approved_case(db, monkeypatch, "own-failed")

        async def breaks(*_a, **_k):
            raise PermissionError("read of the policy")

        monkeypatch.setattr(steward_apply, "ceiling_refusals", breaks)
        assert await _apply_outcome(db, case) == "failed"
        return case, "steward_apply_failed"
    if scene == "advisor_timeout":
        case = await _approved_case(db, monkeypatch, "own-timeout", concur=None)
        run = await _advisor_run(db, case.task_id)
        await db.execute(
            "UPDATE steward_runs SET deadline_at=datetime('now','-1 minutes') "
            "WHERE id=?",
            (run["id"],),
        )
        await db.commit()
        from hub.services.steward_dispatch import close_finished_runs

        assert await close_finished_runs(db) == 1
        return case, "timeout"
    assert scene in ("judge_changes_requested", "judge_escalate")
    case = await build_case(
        db,
        monkeypatch,
        f"own-{scene}",
        policy=policy_of("r2", None, None),
        risk="R2",
        order=False,
    )
    from hub.services.steward_dispatch import order_run

    run = await order_run(db, case.task_id, 1)
    await _mark_started(db, run, model="gpt-5.3-codex")
    verdict = scene.removeprefix("judge_")
    await _judge(db, case.task_id, verdict=verdict)
    return case, scene


@pytest.mark.parametrize(
    "scene",
    [
        "ladder_surface",
        "not_converged",
        "daily_cap",
        "advisor_skipped",
        "apply_interrupted",
        "autopilot_escalated",
        "advisor_object",
        "advisor_timeout",
        "skipped",
        "failed",
        "judge_changes_requested",
        "judge_escalate",
    ],
)
async def test_an_outcome_that_ends_the_stewards_part_writes_a_human_owner(
    db: aiosqlite.Connection, monkeypatch, scene: str
) -> None:
    case, code = await _make_outcome(db, monkeypatch, scene)

    await _assert_handed_to_a_human(db, case.task_id, code)
    if scene == "daily_cap":
        await _a_new_order_takes_the_owner_back(db, monkeypatch, case.task_id)
    await _after_resubmission_the_owner_is_empty(db, case.task_id)


async def _a_new_order_takes_the_owner_back(
    db: aiosqlite.Connection, monkeypatch, task_id: int
) -> None:
    """Передача по потолку временная: заказ после сброса снимает её."""
    from hub import config
    from hub.services.steward_dispatch import order_run

    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 20)
    assert await order_run(db, task_id, 1) is not None
    task = await _owner(db, task_id)
    assert (task["verdict_owner"], task["verdict_owner_reason"]) == (
        "steward",
        "run_ordered",
    )
    assert (await verdict_route(db, task_id)).code != "steward_daily_cap"


@pytest.mark.parametrize("status", ["timeout", "never_started", "refused"])
async def test_a_judge_run_without_a_judgement_writes_a_human_owner(
    db: aiosqlite.Connection, monkeypatch, status: str
) -> None:
    from hub.services.steward_dispatch import (
        _close_generation_as_refused,
        close_finished_runs,
        order_run,
    )

    case = await build_case(
        db,
        monkeypatch,
        f"own-run-{status}",
        policy=policy_of("r2", None, None),
        risk="R2",
        order=False,
    )
    if status == "refused":
        await _close_generation_as_refused(db, case.task_id, 1, "нечего судить")
    else:
        run = await order_run(db, case.task_id, 1)
        if status == "timeout":
            await _mark_started(db, run, model="gpt-5.3-codex")
        await db.execute(
            "UPDATE steward_runs SET deadline_at=datetime('now','-1 minutes') "
            "WHERE id=?",
            (run["id"],),
        )
        await db.commit()
        assert await close_finished_runs(db) == 1

    await _assert_handed_to_a_human(db, case.task_id, status)


async def test_a_written_owner_wins_over_the_autopilot_stance(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    from hub.services import verdict_owner

    _steward_on(monkeypatch)
    task_id = await _ready(
        client, db, monkeypatch, "own-stance", {"verdict": "steward"}
    )
    before = await verdict_route(db, task_id)
    assert (before.decider, before.final) == ("policy", "policy"), before

    assert await verdict_owner.hand_to_human(db, task_id, "daily_cap: потолок")
    await db.commit()

    route = await verdict_route(db, task_id)
    assert (route.decider, route.final) == ("human", "human"), route
    assert route.decider != "policy"
    assert await _in_inbox(db, task_id)
    # повтор того же владельца ничего не пишет
    assert not await verdict_owner.hand_to_human(db, task_id, "daily_cap: потолок")
    assert len(await _events(db, task_id)) == 1


async def _age(db: aiosqlite.Connection, task_id: int) -> None:
    await db.execute(
        "UPDATE tasks SET status_entered_at=datetime('now','-2 hours') WHERE id=?",
        (task_id,),
    )
    await db.commit()


@pytest.mark.parametrize("actor", ["none", "open_run", "review_dispatch", "applying"])
async def test_the_watchdog_hands_a_review_without_an_actor_to_a_human(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, actor: str
) -> None:
    from hub.services.steward_dispatch import order_run, sweep_steward_runs
    from hub.services.verdict_owner import hand_over_stalled_reviews

    _steward_on(monkeypatch)
    task_id = await _ready(
        client, db, monkeypatch, f"own-dog-{actor}", {"verdict": "steward"}
    )
    await _age(db, task_id)
    if actor == "open_run":
        assert await order_run(db, task_id, 1) is not None
        await _age(db, task_id)
    elif actor == "review_dispatch":
        await db.execute(
            "INSERT INTO review_dispatches (task_id, submission_generation, "
            "agent_id, model, status) VALUES (?, 1, 'a', 'm', 'active')",
            (task_id,),
        )
        await db.commit()
    elif actor == "applying":
        await db.execute(
            "INSERT INTO steward_judgements (task_id, generation, kind, "
            "submitted_verdict, verdict, contour, advisor_outcome) "
            "VALUES (?, 1, 'verdict', 'approve', 'approve', 2, 'applying')",
            (task_id,),
        )
        await db.commit()

    if actor == "none":
        # боевой путь: свип заказов стюарда, а не прямой вызов сторожа; контур
        # выключен, чтобы свип не заказал прогон (заказ был бы живым исполнителем)
        monkeypatch.setattr(config, "STEWARD_MODE", "off")
        await sweep_steward_runs(db)
        handed = 1 if (await _owner(db, task_id))["verdict_owner"] else 0
    else:
        handed = await hand_over_stalled_reviews(db)

    if actor != "none":
        assert handed == 0
        assert (await _owner(db, task_id))["verdict_owner"] == ""
        return
    assert handed == 1
    task = await _owner(db, task_id)
    assert task["verdict_owner"] == "human"
    assert task["verdict_owner_reason"].startswith("no_actor")
    assert len(await _events(db, task_id)) == 1
    assert await hand_over_stalled_reviews(db) == 0  # повторный тик
    assert len(await _events(db, task_id)) == 1
    route = await verdict_route(db, task_id)
    assert (route.final, route.code) == ("human", "no_actor")
    assert await _in_inbox(db, task_id)
