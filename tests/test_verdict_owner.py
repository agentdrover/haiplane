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
        await auto_verdict._escalate(db, case.task_id, "красный CI", 1)
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

    assert await verdict_owner.hand_to_human(
        db, task_id, "daily_cap: потолок", generation=1
    )
    await db.commit()

    route = await verdict_route(db, task_id)
    assert (route.decider, route.final) == ("human", "human"), route
    assert route.decider != "policy"
    assert await _in_inbox(db, task_id)
    # повтор того же владельца ничего не пишет
    assert not await verdict_owner.hand_to_human(
        db, task_id, "daily_cap: потолок", generation=1
    )
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


# --- разбор ревью: временные передачи, живой исполнитель, поколение, гонка ----


async def _owner_state(db: aiosqlite.Connection, task_id: int) -> tuple[str, str]:
    task = await _owner(db, task_id)
    return task["verdict_owner"], task["verdict_owner_reason"]


@pytest.mark.parametrize("temporary", ["steward_daily_cap", "steward_advisor_skipped"])
async def test_a_terminal_reason_replaces_a_temporary_one_and_never_the_reverse(
    db: aiosqlite.Connection, monkeypatch, temporary: str
) -> None:
    from hub.services.verdict_owner import hand_to_human

    case = await _approved_case(db, monkeypatch, f"own-repl-{temporary}", concur=None)
    task_id = case.task_id
    assert await hand_to_human(db, task_id, f"{temporary}: до сброса", generation=1)
    assert not await hand_to_human(db, task_id, f"{temporary}: ещё раз", generation=1)

    assert await hand_to_human(db, task_id, "refused: заказа не будет", generation=1)
    assert await _owner_state(db, task_id) == ("human", "refused: заказа не будет")
    assert len(await _events(db, task_id)) == 2

    assert not await hand_to_human(db, task_id, f"{temporary}: поздно", generation=1)
    assert (await _owner_state(db, task_id))[1] == "refused: заказа не будет"
    assert len(await _events(db, task_id)) == 2
    # потолок кончился, а владелец остался терминальным: маршрут за человеком
    route = await verdict_route(db, task_id)
    assert (route.final, route.code) == ("human", "refused")


@pytest.mark.parametrize("contour", [1, 2])
async def test_the_watchdog_counts_only_the_approve_the_advisor_path_picks(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, contour: int
) -> None:
    from hub.services.verdict_owner import hand_over_stalled_reviews

    _steward_on(monkeypatch)
    task_id = await _ready(
        client, db, monkeypatch, f"own-contour-{contour}", {"verdict": "steward"}
    )
    await _age(db, task_id)
    await db.execute(
        "INSERT INTO steward_judgements (task_id, generation, kind, "
        "submitted_verdict, verdict, contour, advisor_outcome) "
        "VALUES (?, 1, 'verdict', 'approve', 'approve', ?, '')",
        (task_id, contour),
    )
    await db.commit()

    # contour=1 никто не подберёт: исполнителя нет; contour=2 подберёт применение
    assert await hand_over_stalled_reviews(db) == (1 if contour == 1 else 0)


@pytest.mark.parametrize("path", ["direct", "decide"])
async def test_an_escalation_is_written_only_on_the_generation_it_judged(
    db: aiosqlite.Connection, monkeypatch, path: str
) -> None:
    from hub.services import auto_verdict

    case = await _approved_case(db, monkeypatch, f"own-gen-{path}", concur=None)
    if path == "direct":
        await repo.update_task(db, case.task_id, submission_generation=2)
        await db.commit()
        await auto_verdict._escalate(db, case.task_id, "красный CI", 1)
    else:

        async def resubmitted_meanwhile(_db, task_id, **_k):
            await repo.update_task(_db, task_id, submission_generation=2)
            await _db.commit()
            return auto_verdict.AutoStance(
                auto_verdict.OUTCOME_ESCALATE, "x", "красный CI", generation=1
            )

        monkeypatch.setattr(auto_verdict, "autopilot_stance", resubmitted_meanwhile)
        assert await auto_verdict._decide(db, case.task_id) is False

    assert await _owner_state(db, case.task_id) == ("", "")
    assert await _events(db, case.task_id) == []


@pytest.mark.parametrize(
    ("reason", "reclaimed"),
    [
        ("steward_daily_cap: потолок", True),
        ("steward_advisor_skipped: пропуск", True),
        ("autopilot_escalated: красный CI", False),
        ("judge_escalate: precondition_failed", False),
    ],
)
async def test_a_new_order_reclaims_only_the_temporary_hand_offs(
    db: aiosqlite.Connection, monkeypatch, reason: str, reclaimed: bool
) -> None:
    from hub.services.verdict_owner import hand_to_human, reclaim_for_steward

    case = await _approved_case(db, monkeypatch, f"own-rec-{reclaimed}-{len(reason)}")
    await hand_to_human(db, case.task_id, reason, generation=1)

    assert await reclaim_for_steward(db, case.task_id, 1) is reclaimed
    expected = ("steward", "run_ordered") if reclaimed else ("human", reason)
    assert await _owner_state(db, case.task_id) == expected


async def test_a_live_actor_created_after_the_check_cancels_the_watchdog_write(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    from hub.services import verdict_route as route_module
    from hub.services.verdict_owner import hand_over_stalled_reviews

    _steward_on(monkeypatch)
    task_id = await _ready(client, db, monkeypatch, "own-race", {"verdict": "steward"})
    await _age(db, task_id)
    real = route_module.verdict_route

    async def route_then_a_dispatch(_db, tid, **kwargs):
        route = await real(_db, tid, **kwargs)
        # исполнитель появляется между выборкой кандидата и записью
        await _db.execute(
            "INSERT INTO review_dispatches (task_id, submission_generation, "
            "agent_id, model, status) VALUES (?, 1, 'a', 'm', 'active')",
            (tid,),
        )
        await _db.commit()
        return route

    monkeypatch.setattr(route_module, "verdict_route", route_then_a_dispatch)

    assert await hand_over_stalled_reviews(db) == 0
    assert await _owner_state(db, task_id) == ("", "")
    assert await _events(db, task_id) == []


async def test_an_escalation_carries_the_generation_the_stance_evaluated(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    from hub.services import auto_verdict
    from hub.services.verdict_owner import written_owner

    case = await _approved_case(db, monkeypatch, "own-stance-gen", concur=None)

    async def judged_the_new_submission(_db, task_id, **_k):
        # пересдача случилась до чтения сдачи стойкой: она судит поколение 2
        await repo.update_task(_db, task_id, submission_generation=2)
        await _db.commit()
        return auto_verdict.AutoStance(
            auto_verdict.OUTCOME_ESCALATE, "x", "красный CI", generation=2
        )

    monkeypatch.setattr(auto_verdict, "autopilot_stance", judged_the_new_submission)
    assert await auto_verdict._decide(db, case.task_id) is False

    owner, reason = written_owner(await _owner(db, case.task_id))
    assert owner == "human" and reason.startswith("autopilot_escalated")
    assert (await _events(db, case.task_id))[0]["generation"] == 2


@pytest.mark.parametrize(
    "interleaved", ["temporary_handoff", "fresh_owner_event", "status_just_entered"]
)
async def test_the_watchdog_write_rechecks_the_owner_and_the_grace(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, interleaved: str
) -> None:
    from hub.services import verdict_route as route_module
    from hub.services.verdict_owner import (
        OWNER_STEWARD,
        hand_over_stalled_reviews,
        hand_to_human,
        write_owner,
    )

    _steward_on(monkeypatch)
    task_id = await _ready(
        client, db, monkeypatch, f"own-race2-{interleaved}", {"verdict": "steward"}
    )
    await _age(db, task_id)
    real = route_module.verdict_route

    async def route_then_a_write(_db, tid, **kwargs):
        route = await real(_db, tid, **kwargs)
        if interleaved == "temporary_handoff":
            assert await hand_to_human(
                _db, tid, "steward_daily_cap: до сброса", generation=1
            )
            # событие давнее: остановить запись должен именно владелец
            await _db.execute(
                "UPDATE events SET created_at=datetime('now','-2 hours') "
                "WHERE task_id=? AND kind='verdict_owner_changed'",
                (tid,),
            )
        elif interleaved == "fresh_owner_event":
            assert await write_owner(
                _db, tid, OWNER_STEWARD, "run_ordered", generation=1
            )
        else:
            await _db.execute(
                "UPDATE tasks SET status_entered_at=datetime('now') WHERE id=?", (tid,)
            )
        await _db.commit()
        return route

    monkeypatch.setattr(route_module, "verdict_route", route_then_a_write)

    assert await hand_over_stalled_reviews(db) == 0
    owner, reason = await _owner_state(db, task_id)
    assert not reason.startswith("no_actor")
    if interleaved == "temporary_handoff":
        assert (owner, reason) == ("human", "steward_daily_cap: до сброса")


async def test_a_real_escalation_at_report_intake_writes_the_judged_generation(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    from hub import config
    from tests.test_auto_verdict import _post_review, _submitted_task

    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    task_id = await _submitted_task(client, db, "own-sec", {"verdict": "auto"})
    await _post_review(
        client,
        task_id,
        findings_rejected=[
            {"title": "possible auth bypass", "category": "security", "reason": "n/a"}
        ],
    )

    owner, reason = await _owner_state(db, task_id)
    assert owner == "human" and reason.startswith("autopilot_escalated")
    assert [e["generation"] for e in await _events(db, task_id)] == [1]
