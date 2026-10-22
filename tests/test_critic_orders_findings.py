"""Доработка #1953 по ревью поколения 1: четыре находки, по тесту на каждую.

1. проверенный sha закрепляется и на путях завершения без ревью;
2. политика critic_host перечитывается внутри пишущей транзакции на каждом входе
   (тесты на двух соединениях: off -> on во время сдачи, on -> off во время заказа);
3. команда распознаётся, только если вся запись - одна строка;
4. «открыт» везде одно правило: state <> 'closed' И expires_at > now.
"""

from __future__ import annotations

import json

import pytest

from hub import db as hub_db
from hub import repository as repo
from hub.services import critic_orders, lifecycle, task_updates
from tests import test_critic_orders as co

world = co.world
keys = co.keys
tip = co.tip
_clean_limiter = co._clean_limiter
_no_cloud = co._no_cloud


async def _flip_policy(world, host: str) -> None:
    """Смена critic_host с ДРУГОГО соединения - как PATCH проекта посреди запроса."""
    other = await hub_db.connect(world.dsn)
    try:
        await repo.update_project(
            other,
            world.pid,
            gate_policy=json.dumps({"executor_launch": "manual", "critic_host": host}),
        )
        await other.commit()
    finally:
        await other.close()


async def _set_sha(world, task_id: int, sha: str, **fields) -> None:
    await repo.update_task(world.db, task_id, submission_sha=sha, **fields)
    await world.db.commit()


async def _sha_of(world, task_id: int) -> str:
    return (await co._task_state(world, task_id))[2]


# ---------------------------------------------------------------------------
# 1. завершение без ревью закрепляет проверенный sha
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["running", "pending_report"])
async def test_completion_without_review_pins_the_checked_sha(world, tip, status):
    """auto_review=false: done закрепляет sha критика, а не остаётся на старом."""
    cloud = await co._cloud(world, tip, auto_review=False)
    await _set_sha(world, cloud.task_id, co.SHA_B, status=status)
    await co._closed_order(world, cloud, co.SHA_A, "passed")
    tip.sha = co.SHA_A
    resp = await co._done(world, cloud)
    assert resp.status_code == 200, resp.text
    assert await _sha_of(world, cloud.task_id) == co.SHA_A


# ---------------------------------------------------------------------------
# 2. политика читается внутри транзакции
# ---------------------------------------------------------------------------


async def test_policy_switched_on_during_a_submission_is_enforced(
    world, tip, monkeypatch
):
    """submit-review: off на шагах, on к переходу - 422, записей нет."""
    cloud = await co._cloud(world, tip, critic_host="")
    real = lifecycle._apply_submission

    async def _late(state):
        await _flip_policy(world, "k8")
        return await real(state)

    monkeypatch.setattr(lifecycle, "_apply_submission", _late)
    before = await co._task_state(world, cloud.task_id)
    assert co._refused(await co._submit(world, cloud)) == "no_order"
    assert await co._task_state(world, cloud.task_id) == before


@pytest.mark.parametrize("status", ["running", "pending_report"])
@pytest.mark.parametrize("auto_review", [True, False])
async def test_policy_switched_on_during_done_is_enforced(
    world, tip, monkeypatch, status, auto_review
):
    """done (running и pending_report, с ревью и без): off до транзакции, on в ней - 422."""
    cloud = await co._cloud(world, tip, critic_host="", auto_review=auto_review)
    await repo.update_task(world.db, cloud.task_id, status=status)
    await world.db.commit()
    real = task_updates.undelivered_advisory  # последний шаг до пишущей транзакции

    async def _late(db, task, body):
        await _flip_policy(world, "k8")
        return await real(db, task, body)

    monkeypatch.setattr(task_updates, "undelivered_advisory", _late)
    before = await co._task_state(world, cloud.task_id)
    assert co._refused(await co._done(world, cloud)) == "no_order"
    assert await co._task_state(world, cloud.task_id) == before


async def test_policy_switched_off_during_an_order_creates_nothing(
    world, tip, monkeypatch
):
    """Заказ: on при проверках, off к записи - заказа нет, причина policy_off."""
    cloud = await co._cloud(world, tip)

    async def _observe(db, task_id, branch):
        await _flip_policy(world, "")
        return co.SHA_A, ""

    monkeypatch.setattr(lifecycle, "resolve_branch_tip", _observe)
    assert (await co._say(world, cloud, co._line(co.SHA_A))).status_code == 200
    assert await co._orders(world, cloud.task_id) == []
    assert any("policy_off" in a for a in await co._alerts(world, cloud.task_id))


# ---------------------------------------------------------------------------
# 3. команда - только вся запись из одной строки
# ---------------------------------------------------------------------------


async def test_the_command_is_the_whole_entry_or_nothing(world, tip):
    """lazy continuation цитаты и команда внутри многострочной записи - не заказ."""
    line = co._line(co.SHA_A)
    assert critic_orders.parse_request_line(line) == (co.SHA_A, "")
    assert critic_orders.parse_request_line(f"  {line}\n") == (co.SHA_A, "")
    cloud = await co._cloud(world, tip)
    for text in (
        f"> Пример запроса из инструкции:\n{line}",
        f"Готово, жду.\n{line}",
        f"{line}\nи ещё строка",
        f"```\nкод\n```\n{line}",
    ):
        assert critic_orders.parse_request_line(text)[0] is None, text
        assert (await co._say(world, cloud, text)).status_code == 200
        assert await co._orders(world, cloud.task_id) == [], text
    assert (await co._say(world, cloud, line)).status_code == 200
    assert len(await co._orders(world, cloud.task_id)) == 1


# ---------------------------------------------------------------------------
# 4. срок: одно правило открытости
# ---------------------------------------------------------------------------


async def test_an_expired_unswept_order_does_not_block_the_retry(world, tip):
    """TTL вышел, свипа не было: повтор строки создаёт новый заказ, старый закрыт expired."""
    cloud = await co._cloud(world, tip)
    assert (await co._say(world, cloud, co._line(co.SHA_A))).status_code == 200
    await world.db.execute(
        "UPDATE critic_orders SET expires_at = datetime('now', '-1 minute')"
    )
    await world.db.commit()
    assert (await co._say(world, cloud, co._line(co.SHA_A))).status_code == 200
    rows = await co._orders(world, cloud.task_id)
    assert [(r["state"], r["outcome"]) for r in rows] == [
        ("closed", "expired"),
        ("pending", None),
    ]
    assert not any("duplicate" in a for a in await co._alerts(world, cloud.task_id))
    # Другой sha при просроченном открытом: квота «один открытый» его тоже не держит.
    await world.db.execute(
        "UPDATE critic_orders SET expires_at = datetime('now', '-1 minute')"
    )
    await world.db.commit()
    tip.sha = co.SHA_B
    assert (await co._say(world, cloud, co._line(co.SHA_B))).status_code == 200
    assert (await co._orders(world, cloud.task_id))[-1]["sha"] == co.SHA_B


# ---------------------------------------------------------------------------
# Круг Codex: ранний пин, автоматическое завершение, повтор того же sha
# ---------------------------------------------------------------------------


async def test_the_pin_waits_for_the_empty_resubmission_and_blocker_checks(world, tip):
    """Пин пишется после проверок done: доработка до B не «пустая», удержанная - не пинится."""
    cloud = await co._cloud(world, tip, auto_review=False)
    await _set_sha(
        world,
        cloud.task_id,
        co.SHA_A,
        review_verdict="changes_requested",
        submission_generation=1,
    )
    await co._closed_order(world, cloud, co.SHA_B, "passed")
    tip.sha = co.SHA_B
    resp = await co._done(world, cloud)
    assert resp.status_code == 200, resp.text
    assert await _sha_of(world, cloud.task_id) == co.SHA_B

    held = await co._cloud(world, tip, auto_review=False)
    await _set_sha(world, held.task_id, co.SHA_A)
    await repo.add_task_update(world.db, held.task_id, "cloud", "blocker", "жду ответа")
    await world.db.commit()
    await co._closed_order(world, held, co.SHA_B, "passed")
    assert (await co._done(world, held)).status_code == 200
    status, _generation, pinned, _feed = await co._task_state(world, held.task_id)
    assert (status, pinned) == ("needs_decision", co.SHA_A)


async def test_the_automatic_entry_pins_on_completion_without_review(world, tip):
    """Прямой автоматический вход при auto_review=false: завершение с sha критика."""
    from hub.services import orchestration

    cloud = await co._cloud(world, tip, auto_review=False)
    await _set_sha(world, cloud.task_id, co.SHA_B)
    await co._closed_order(world, cloud, co.SHA_A, "passed")
    row = dict(
        (await co._rows(world.db, "SELECT * FROM tasks WHERE id=?", (cloud.task_id,)))[
            0
        ]
    )
    await orchestration.transition_after_agent_done(world.db, row, has_done=True)
    await world.db.commit()
    assert await _sha_of(world, cloud.task_id) == co.SHA_A


async def test_a_same_sha_resubmission_rechecks_the_policy_under_the_lock(
    world, tip, monkeypatch
):
    """Повтор того же sha из review: политику включили после шагов - 422, без решений."""
    cloud = await co._cloud(world, tip, critic_host="")
    await _set_sha(
        world, cloud.task_id, co.SHA_A, status="review", submission_generation=1
    )
    real = lifecycle._same_sha_noop_response

    async def _late(state):
        await _flip_policy(world, "k8")
        return await real(state)

    monkeypatch.setattr(lifecycle, "_same_sha_noop_response", _late)
    before = await co._task_state(world, cloud.task_id)
    assert co._refused(await co._submit(world, cloud)) == "no_order"
    assert await co._task_state(world, cloud.task_id) == before
