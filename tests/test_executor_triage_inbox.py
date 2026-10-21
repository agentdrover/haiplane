"""Разбор прогона k8 во Входящих и в дайджесте (#1943), поверх рекомендаций #1942.

AC-1: пункт во Входящих, действие владельца «разбор принят / отклонён» (только человек).
AC-2: строка «разобрано N» в дайджесте, окна created_at / followup_at.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from hub import config, services
from hub.config import TokenIdentity
from hub.services import dashboard, digest
from hub.services.gate_events import HUMAN_GATE_EVENT_KINDS
from tests import test_executor_orders as _eo
from tests.test_executor_orders import _task
from tests.test_executor_triage import (
    CLASS0,
    CLASS1,
    CLASS2,
    CLASS3,
    CLASS4,
    _fail,
    _pin_old_activation,
    _recs,
    _relaunch,
    _rows,
    _set_task,
)

world = _eo.world
keys = _eo.keys
_clean_limiter = _eo._clean_limiter
_no_cloud = _eo._no_cloud

STEWARD_TOKEN = "steward-token-1943"  # pragma: allowlist secret
DAY = "2026-10-20"
NOW = datetime(2026, 10, 21, 1, 0, tzinfo=UTC)

#: Ключи дайджеста до #1943: их набор и значения не меняются.
OLD_KEYS = {
    "date",
    "project",
    "release_blocks",
    "category_debt",
    "human_queue",
    "findings_queue",
    "auto_approvals",
    "auto_verdicts",
    "escalations",
    "steward_judgements",
    "steward_self_approvals",
    "deliveries",
    "audit_sample",
    "audit_results",
}


@pytest.fixture
async def owner(world, monkeypatch):
    """Мир #1942 с агентским и стюардовым ключами и закреплённой границей включения."""
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            _eo.AGENT_TOKEN: TokenIdentity("bot", "agent"),
            STEWARD_TOKEN: TokenIdentity("steward-bot", "steward"),
        },
    )
    await _pin_old_activation(world)
    world.steward_auth = {"Authorization": f"Bearer {STEWARD_TOKEN}"}
    return world


def _rec_id(recs: list[dict], order_id: int) -> int:
    return int(next(r["id"] for r in recs if r["order_id"] == order_id))


async def _triage_items(world, **filters) -> dict[int, dict]:
    await world.db.commit()
    inbox = await services.get_inbox_data(world.db, **filters)
    return {int(i["task_id"]): i for i in inbox["triage_items"]}


async def _verdict(world, rec_id: int, verdict: str, auth: dict):
    return await world.client.post(
        f"/api/executor-triage/{rec_id}/verdict",
        json={"verdict": verdict},
        headers=auth,
    )


async def _row(world, rec_id: int) -> dict:
    await world.db.commit()
    sql = "SELECT * FROM executor_triage_recommendations WHERE id = ?"
    return (await _rows(world.db, sql, (rec_id,)))[0]


# ---------------------------------------------------------------------------
# AC-1
# ---------------------------------------------------------------------------


async def test_triage_item_appears_and_resolves(owner):
    """AC-1: пункт с классом и рекомендацией, счётчик, действие только человека."""
    world = owner
    accept = await _task(world, "принять")
    reject = await _task(world, "отклонить")
    take = await _task(world, "взять")
    agent_try = await _task(world, "агент")
    o_acc = await _fail(world, accept, CLASS1)
    o_rej = await _fail(world, reject, CLASS3)
    await _fail(world, take, CLASS2)
    o_ag = await _fail(world, agent_try, CLASS4)
    recs = await _recs(world)

    items = await _triage_items(world)
    assert {accept, reject, take, agent_try} <= set(items)
    one = items[accept]
    assert (one["class"], one["class_name"]) == (1, "infra")
    assert one["action"] == "retry_after_status"
    assert "повторить запуск" in one["recommendation"]
    assert one["recommendation_id"] == _rec_id(recs, o_acc)

    inbox = await services.get_inbox_data(world.db)
    quiet = {**inbox, "triage_items": []}
    assert inbox["inbox_attention_total"] == dashboard.inbox_attention_total(
        quiet
    ) + len(inbox["triage_items"])
    page = await world.client.get("/partials/inbox", headers=world.human_auth)
    assert page.status_code == 200
    assert f"#{accept}" in page.text and "Разбор принят" in page.text
    assert 'name="csrf_token"' in page.text

    # Агент, стюард и ключ хоста не разбирают.
    rid = _rec_id(recs, o_acc)
    for auth in (world.agent_auth, world.steward_auth, world.k8_auth):
        resp = await _verdict(world, rid, "accepted", auth)
        assert resp.status_code in (401, 403), resp.text
    assert (await _row(world, rid))["owner_verdict"] is None
    assert accept in await _triage_items(world)

    # Человек: принято. Это только owner_verdict: followup и статус не трогаются.
    ok = await _verdict(world, rid, "accepted", world.human_auth)
    assert ok.status_code == 200, ok.text
    after = await _row(world, rid)
    assert after["owner_verdict"] == "accepted" and after["owner_verdict_at"]
    assert after["followup"] is None
    assert accept not in await _triage_items(world)
    status = await _rows(world.db, "SELECT status FROM tasks WHERE id = ?", (accept,))
    assert status[0]["status"] == "open"

    # Повторное действие не перезаписывает решение.
    again = await _verdict(world, rid, "rejected", world.human_auth)
    assert again.status_code == 409
    assert (await _row(world, rid))["owner_verdict"] == "accepted"

    # Отклонено - через веб-форму; пункт исчезает.
    rej_id = _rec_id(recs, o_rej)
    web = await world.client.post(
        f"/triage/{rej_id}/web-verdict",
        data={"verdict": "rejected"},
        headers=world.human_auth,
    )
    assert web.status_code == 200, web.text
    assert (await _row(world, rej_id))["owner_verdict"] == "rejected"
    assert reject not in await _triage_items(world)
    bad = await world.client.post(
        f"/triage/{_rec_id(recs, o_ag)}/web-verdict",
        data={"verdict": "accepted"},
        headers=world.agent_auth,
    )
    assert bad.status_code == 403
    nonsense = await _verdict(world, _rec_id(recs, o_ag), "maybe", world.human_auth)
    assert nonsense.status_code == 422

    # Задачу взяли: пункт исчезает сам, решение не записывается.
    await _set_task(world, take, status="claimed", claimed_by="someone")
    assert take not in await _triage_items(world)
    take_rec = next(r for r in await _recs(world) if r["task_id"] == take)
    assert take_rec["owner_verdict"] is None

    # Событие действия не гейт владельца и не копит его минуты.
    events = await _rows(
        world.db,
        "SELECT actor FROM events WHERE kind = 'executor_triage_verdict'",
    )
    assert len(events) == 2 and "executor_triage_verdict" not in HUMAN_GATE_EVENT_KINDS
    assert not any(
        row["status"] == "needs_decision"
        for row in await _rows(world.db, "SELECT status FROM tasks")
    )


async def test_triage_item_only_for_the_last_order_and_scoped(owner):
    """Рекомендация по не последнему заказу и чужой проект/владелец не показываются."""
    world = owner
    stale = await _task(world, "новый заказ после неудачи")
    fresh = await _task(world, "свежая")
    await _fail(world, stale, CLASS1)
    await _relaunch(world, stale)
    await _fail(world, fresh, CLASS1)
    await _set_task(world, fresh, human_owner="alice")

    items = await _triage_items(world)
    assert stale not in items and fresh in items
    assert fresh in await _triage_items(world, project=world.slug)
    assert fresh not in await _triage_items(world, project="default")
    assert fresh in await _triage_items(world, human_owner="alice")
    assert fresh not in await _triage_items(world, human_owner="bob")


async def test_triage_unreadable_is_not_empty(owner, monkeypatch):
    """Сбой чтения рекомендаций - названная причина, а не «разбор пуст»."""
    world = owner
    await _fail(world, await _task(world, "x"), CLASS1)
    from hub.services import triage_inbox

    monkeypatch.setattr(triage_inbox, "_ITEMS_SQL", "SELECT * FROM no_such_table")
    inbox = await services.get_inbox_data(world.db)
    assert inbox["triage_items"] == []
    assert inbox["triage_unavailable"]
    page = await world.client.get("/partials/inbox", headers=world.human_auth)
    assert "разбор не прочитан" in page.text


# ---------------------------------------------------------------------------
# AC-2
# ---------------------------------------------------------------------------


async def _stamp(world, order_id: int, created: str, followup=None, at=None) -> None:
    await world.db.execute(
        "UPDATE executor_triage_recommendations SET created_at = ?, followup = ?, "
        "followup_at = ? WHERE order_id = ?",
        (created, followup, at, order_id),
    )
    await world.db.commit()


async def _day_of_triage(world) -> dict[str, int]:
    """Пять рекомендаций дня DAY и шестая, созданная накануне, решённая в DAY."""
    t = [await _task(world, f"разбор {i}") for i in range(6)]
    orders = [
        await _fail(world, t[0], CLASS1),  # retry_after_status
        await _fail(world, t[1], CLASS2),  # refine_hints_and_retry
        await _fail(world, t[2], CLASS3),  # retry_once
        await _fail(world, t[3], CLASS4),  # to_owner
        await _fail(world, t[4], CLASS0),  # to_owner
        await _fail(world, t[5], CLASS1),  # создана накануне
    ]
    at = f"{DAY} 12:00:00"
    created = f"{DAY} 10:00:00"
    await _stamp(world, orders[0], created, "new_order", at)  # совпало
    await _stamp(world, orders[1], created, "statement_changed", at)  # не совпало
    await _stamp(world, orders[2], created, "submitted", at)  # не измеряется
    await _stamp(world, orders[3], created, "statement_changed", at)  # совпало
    await _stamp(world, orders[4], created, "none_72h", at)  # не измеряется
    await _stamp(world, orders[5], "2026-10-19 10:00:00", "new_order", at)  # совпало
    return {"n": 5, "k": 3, "m": 2, "y": 4, "x": 3, "u": 2}


async def _digests(world, now=NOW) -> list[dict]:
    created = await digest.generate_due_digests(world.db, now=now)
    await world.db.commit()
    rows = await _rows(
        world.db,
        "SELECT d.*, p.slug AS slug FROM autopilot_digests d "
        "JOIN projects p ON p.id = d.project_id ORDER BY d.id",
    )
    assert created == len(rows)
    return [{**r, "data": json.loads(r["payload"])} for r in rows]


async def test_digest_counts_the_triage(owner):
    """AC-2: день только с разборами создаёт дайджест; числа и окна верны."""
    world = owner
    want = await _day_of_triage(world)

    (only,) = [d for d in await _digests(world) if d["slug"] == world.slug]
    mine = only["data"]
    assert OLD_KEYS <= set(mine)
    assert mine["auto_approvals"] == [] and mine["escalations"] == []
    triage = mine["triage"]
    assert triage["state"] == "measured"
    assert (triage["created"], triage["retry"], triage["owner"]) == (
        want["n"],
        want["k"],
        want["m"],
    )
    assert (triage["matched"], triage["resolved"], triage["unmeasured"]) == (
        want["x"],
        want["y"],
        want["u"],
    )
    page = await world.client.get("/digests", headers=world.human_auth)
    assert (
        "разобрано 5, рекомендован повтор 3, к владельцу 2; "
        "совпало с вашим решением 3 из 4" in page.text
    )
    # Готовый дайджест не пересчитывается.
    again = await digest.generate_due_digests(world.db, now=NOW)
    assert again == 0


async def test_digest_day_with_only_decisions_and_quiet_day(owner):
    """День только с решениями создаёт дайджест; день без разбора - нет."""
    world = owner
    o = await _fail(world, await _task(world, "решение пришло позже"), CLASS1)
    await _stamp(world, o, "2026-10-17 10:00:00", "new_order", "2026-10-18 08:00:00")
    got = await _digests(world, now=datetime(2026, 10, 19, 1, 0, tzinfo=UTC))
    (only,) = [d for d in got if d["slug"] == world.slug]
    assert only["digest_date"] == "2026-10-18"
    assert (only["data"]["triage"]["created"], only["data"]["triage"]["resolved"]) == (
        0,
        1,
    )
    # Тихий день (ничего не создано и не решено) дайджеста не даёт.
    quiet = await digest.generate_due_digests(
        world.db, now=datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
    )
    assert quiet == 0


async def test_digest_old_payload_without_triage_is_unmeasured(owner):
    """Дайджест старше раздела: «не измерялось», а не ноль."""
    assert digest.triage_state({"date": DAY}) == "unmeasured"
    assert digest.triage_state({"triage": {"state": "measured"}}) == "measured"
    assert digest.triage_state({"triage": {"state": "unavailable"}}) == "unavailable"


async def test_digest_delegating_project_gets_the_line_too(owner):
    """Проект с делегированием: строка разбора рядом с прежними секциями."""
    from hub import repository as repo

    world = owner
    await repo.update_project(
        world.db,
        world.pid,
        gate_policy=json.dumps({"executor_launch": "manual", "verdict": "steward"}),
    )
    await world.db.commit()
    o = await _fail(world, await _task(world, "делегирующий"), CLASS1)
    await _stamp(world, o, f"{DAY} 09:00:00")
    (only,) = [d for d in await _digests(world) if d["slug"] == world.slug]
    assert OLD_KEYS <= set(only["data"])
    assert only["data"]["triage"]["created"] == 1
