"""Побудка стюарда: заказ размещает хаб, и ровно один раз (#1073).

Проверяется не «умеет ли диспетчер заказать», а четыре предохранителя, ради
которых он вообще выделен в отдельный контракт: выключатель, идемпотентность
заказа, суточный потолок и дедлайн слота. Каждый из них при срабатывании
оставляет сегодняшний человеческий маршрут работать — отказ здесь никогда не
означает «проверено и чисто».
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from hub import config
from hub import repository as repo
from hub.db import fetchall
from hub.services.steward_dispatch import (
    EVENT_ORDERED,
    EVENT_REFUSED,
    REFUSED_ALREADY_ORDERED,
    REFUSED_DAILY_CAP,
    REFUSED_MODE_OFF,
    RUN_OPEN,
    RUN_SUPERSEDED,
    RUN_TIMEOUT,
    close_finished_runs,
    open_run,
    order_due_runs,
    order_run,
)


@pytest.fixture(autouse=True)
def shadow_mode(monkeypatch):
    """Контур включён в тень: заказы размещаются, ничего не решают."""
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 20)
    monkeypatch.setattr(config, "STEWARD_RUN_DEADLINE_MIN", 30)


async def _project(db: aiosqlite.Connection, slug: str, *, steward: bool) -> int:
    project_id = await repo.create_project(
        db, slug=slug, name=slug, workspace_path="", status="active"
    )
    policy = {"verdict": "steward"} if steward else {}
    await db.execute(
        "UPDATE projects SET gate_policy=? WHERE id=?",
        (json.dumps(policy), project_id),
    )
    await db.commit()
    return project_id


async def _submitted_task(
    db: aiosqlite.Connection, project_id: int, *, generation: int = 1
) -> int:
    task_id = await repo.create_task(
        db,
        title="сдача на суд",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="pda_claude",
        rationale="",
        status="review",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(
        db,
        task_id,
        project_id=project_id,
        submission_generation=generation,
        submission_sha="a" * 40,
    )
    await db.commit()
    return task_id


async def _events(db: aiosqlite.Connection, kind: str) -> list[dict]:
    rows = await fetchall(db, "SELECT * FROM events WHERE kind=?", (kind,))
    return [dict(r) for r in rows]


async def test_review_entry_orders_one_run(db: aiosqlite.Connection):
    """#1073 AC-1: тик поллера заказывает ровно один прогон и говорит об этом.

    Заказ — единственный способ, которым прогон вообще начинается: у самого
    стюарда такой операции нет (#1021).
    """
    project_id = await _project(db, "steward-one", steward=True)
    task_id = await _submitted_task(db, project_id)

    ordered = await order_due_runs(db)

    assert ordered == 1
    run = await open_run(db, task_id, 1)
    assert run is not None
    assert run["status"] == RUN_OPEN
    assert run["model"] == config.STEWARD_MODEL
    events = await _events(db, EVENT_ORDERED)
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["generation"] == 1
    assert payload["kind"] == "verdict"
    assert payload["model"] == config.STEWARD_MODEL


async def test_order_is_at_most_once_per_generation(db: aiosqlite.Connection):
    """#1073 AC-2: второй заказ на ту же генерацию не создаётся.

    Два тика, идущих подряд по одной сдаче, — обычный случай, а не редкий:
    поллер тикает каждые тридцать секунд, пока задача стоит в review. Дубль
    стоил бы второго оплаченного прогона и второго суждения на один код.
    """
    project_id = await _project(db, "steward-once", steward=True)
    task_id = await _submitted_task(db, project_id)

    first = await order_run(db, task_id, 1)
    second = await order_run(db, task_id, 1)
    await order_due_runs(db)

    assert first is not None
    assert second is None
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE task_id=?", (task_id,))
    assert len(rows) == 1
    refusals = await _events(db, EVENT_REFUSED)
    assert any(
        json.loads(e["payload"])["reason"] == REFUSED_ALREADY_ORDERED for e in refusals
    )


async def test_kill_switch_closes_dispatcher(db: aiosqlite.Connection, monkeypatch):
    """#1073 AC-3: off и нераспознанное значение одинаково закрывают контур.

    Опечатка в drop-in не должна ВКЛЮЧАТЬ проверку, которой никто не
    заказывал, — это правило #835 про потолок класса, здесь оно же.
    """
    project_id = await _project(db, "steward-off", steward=True)
    task_id = await _submitted_task(db, project_id)

    for mode in ("off", "shadwo", ""):
        monkeypatch.setattr(config, "STEWARD_MODE", mode)
        assert await order_due_runs(db) == 0
        assert await order_run(db, task_id, 1) is None

    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE task_id=?", (task_id,))
    assert rows == []
    refusals = await _events(db, EVENT_REFUSED)
    assert refusals, "закрытый контур обязан сказать об этом в фиде"
    assert all(json.loads(e["payload"])["reason"] == REFUSED_MODE_OFF for e in refusals)


async def test_daily_cap_falls_back_to_human(db: aiosqlite.Connection, monkeypatch):
    """#1073 AC-4: исчерпанный потолок — человеческий маршрут, а не тишина.

    «Упёрлись в потолок» и «проверено, чисто» обязаны быть различимы: второе
    прочтение первого — это ровно то, как пустой отчёт однажды прошёл за
    чистый (#750).
    """
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 2)
    project_id = await _project(db, "steward-cap", steward=True)
    first = await _submitted_task(db, project_id)
    second = await _submitted_task(db, project_id)
    third = await _submitted_task(db, project_id)

    assert await order_run(db, first, 1) is not None
    assert await order_run(db, second, 1) is not None
    over_cap = await order_run(db, third, 1)

    assert over_cap is None
    assert await open_run(db, third, 1) is None
    refusals = await _events(db, EVENT_REFUSED)
    assert any(
        json.loads(e["payload"])["reason"] == REFUSED_DAILY_CAP for e in refusals
    )


async def test_hung_run_closes_on_timeout(db: aiosqlite.Connection):
    """#1073 AC-5: просроченный слот закрывается, статус задачи не трогается.

    review:client — человеческий слот без дедлайна, поэтому зависший прогон
    иначе не эскалирует никогда: он просто стоит и выглядит заказанным.
    """
    project_id = await _project(db, "steward-timeout", steward=True)
    task_id = await _submitted_task(db, project_id)
    run = await order_run(db, task_id, 1)
    assert run is not None
    await db.execute(
        "UPDATE steward_runs SET deadline_at = datetime('now', '-1 minute') WHERE id=?",
        (run["id"],),
    )
    await db.commit()

    closed = await close_finished_runs(db)

    assert closed == 1
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run["id"],))
    assert dict(rows[0])["status"] == RUN_TIMEOUT
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "review", "диспетчер не двигает задачу — это F4"


async def test_human_verdict_closes_slot(db: aiosqlite.Connection):
    """#1073 AC-6: человеческий вердикт на эту генерацию закрывает слот.

    Человек всегда старше: суждение, пришедшее после него, получает 409 по
    контракту F1, а слот, ради которого его ждали, больше ничего не ждёт.
    """
    project_id = await _project(db, "steward-human", steward=True)
    task_id = await _submitted_task(db, project_id)
    run = await order_run(db, task_id, 1)
    assert run is not None
    await repo.update_task(
        db,
        task_id,
        review_verdict="approved",
        review_verdict_generation=1,
    )
    await db.commit()

    closed = await close_finished_runs(db)

    assert closed == 1
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run["id"],))
    assert dict(rows[0])["status"] == RUN_SUPERSEDED
    assert await open_run(db, task_id, 1) is None


async def test_project_without_the_policy_is_left_alone(db: aiosqlite.Connection):
    """Проект, не просивший стюарда, не получает заказов.

    Не отдельный AC, а граница всех шести: политику ставит человек (#743), и
    диспетчер не расширяет её молча на соседние проекты.
    """
    plain = await _project(db, "steward-none", steward=False)
    task_id = await _submitted_task(db, plain)

    assert await order_due_runs(db) == 0
    assert await open_run(db, task_id, 1) is None
