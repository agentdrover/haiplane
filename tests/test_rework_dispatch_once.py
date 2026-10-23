"""Решение «на доработку» потребляет вход needs_decision один раз (#1991).

Гонки идут на разных соединениях: ``client`` открывает соединение на запрос, а
сеть диспетчера подменена заглушкой, которая первого вызывающего держит «в
сети», пока второй вход пробует то же решение. Бронь запуска лежит в базе, а не
в памяти процесса, поэтому гарантия не зависит от замка листа (#1813).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import HTTPException

from hub import repository as repo
from hub import services
from hub.integrations.registry import plugins
from hub.models import TaskDecide
from tests.card_sheet_support import SHEET_HEADERS, sheet_form
from tests.test_decision_brief import _task_in

REWORK = {"action": "rework", "instructions": "починить"}
#: Достаточно, чтобы пройти срок ожидания сверки; сама граница — в модуле.
LONG_AGO = "2000-01-01 00:00:00"


class Network:
    """Заглушка submit_task: первый вызов ждёт ``release``, остальные не ждут."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.messages: list[str] = []

    async def submit(self, message: str, **_kwargs: Any) -> dict[str, Any]:
        self.messages.append(message)
        n = len(self.messages)
        if n == 1:
            self.entered.set()
            await asyncio.wait_for(self.release.wait(), 10)
        return {"job_id": f"job-{n}"}


@pytest.fixture
def net(monkeypatch) -> Network:
    fake = Network()
    monkeypatch.setattr(plugins.dispatch, "submit_task", fake.submit)
    return fake


async def _count(db, sql: str, task_id: int) -> int:
    rows = await db.execute_fetchall(sql, (task_id,))
    return int(rows[0][0])


async def _decisions(db, task_id: int) -> int:
    return await _count(
        db,
        "SELECT COUNT(*) FROM task_updates WHERE task_id=? AND kind='decision'",
        task_id,
    )


async def _decided_events(db, task_id: int) -> int:
    return await _count(
        db,
        "SELECT COUNT(*) FROM events WHERE task_id=? AND kind='task_decided'",
        task_id,
    )


async def _winner_in_network(client, net: Network, task_id: int):
    """Первое решение rework ушло в сеть и стоит там; вернуть его задачу."""
    first = asyncio.create_task(
        client.post(f"/api/tasks/{task_id}/decide", json=REWORK)
    )
    await asyncio.wait_for(net.entered.wait(), 10)
    return first


async def _finish(net: Network, first) -> Any:
    net.release.set()
    return await asyncio.wait_for(first, 10)


async def _assert_launched_once(db, net: Network, task_id: int) -> None:
    task = dict(await repo.get_task(db, task_id))
    assert len(net.messages) == 1, "задание ушло дважды"
    assert task["status"] == "fix_requested" and task["job_id"] == "job-1"
    assert await _decisions(db, task_id) == 1
    assert await _decided_events(db, task_id) == 1


async def test_two_concurrent_reworks_launch_once(client, db, net):
    """AC-1, REST + REST на двух соединениях."""
    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    first = await _winner_in_network(client, net, task_id)

    loser = await client.post(f"/api/tasks/{task_id}/decide", json=REWORK)

    assert loser.status_code == 409, loser.text
    assert "уже" in loser.text
    done = await _finish(net, first)
    assert done.status_code == 200, done.text
    await _assert_launched_once(db, net, task_id)


async def test_rest_then_plain_web_form_launch_once(client, db, net):
    """AC-1, REST + веб-форма без expected_* (форма «Подробностей»)."""
    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    first = await _winner_in_network(client, net, task_id)

    loser = await client.post(
        f"/tasks/{task_id}/web-decide", data=REWORK, follow_redirects=False
    )

    assert loser.status_code == 409, loser.text
    assert (await _finish(net, first)).status_code == 200
    await _assert_launched_once(db, net, task_id)


async def test_web_form_then_rest_launch_once(client, db, net):
    """AC-1, в обратную сторону: победитель - веб-форма, проигравший - REST."""
    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    first = asyncio.create_task(
        client.post(f"/tasks/{task_id}/web-decide", data=REWORK, follow_redirects=False)
    )
    await asyncio.wait_for(net.entered.wait(), 10)

    loser = await client.post(f"/api/tasks/{task_id}/decide", json=REWORK)

    assert loser.status_code == 409, loser.text
    assert (await _finish(net, first)).status_code in (200, 303)
    await _assert_launched_once(db, net, task_id)


async def test_rest_then_card_sheet_launch_once(client, db, net):
    """AC-1, REST + лист карточки (MC-4) с expected_*: замок процесса листу не помогает."""
    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    page = (await client.get(f"/tasks/{task_id}")).text
    form, data = sheet_form(page, "sh-rework")
    first = await _winner_in_network(client, net, task_id)

    loser = await client.post(
        form.attrs["action"], data={**data, **REWORK}, headers=SHEET_HEADERS
    )

    assert loser.status_code == 409, loser.text
    assert 'class="c2-sheet-note"' in loser.text and "data-c2-done" not in loser.text
    assert (await _finish(net, first)).status_code == 200
    await _assert_launched_once(db, net, task_id)


async def test_card_sheet_then_rest_launch_once(client, db, net):
    """AC-1: победитель - лист, проигравший - REST."""
    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    page = (await client.get(f"/tasks/{task_id}")).text
    form, data = sheet_form(page, "sh-rework")
    first = asyncio.create_task(
        client.post(
            form.attrs["action"], data={**data, **REWORK}, headers=SHEET_HEADERS
        )
    )
    await asyncio.wait_for(net.entered.wait(), 10)

    loser = await client.post(f"/api/tasks/{task_id}/decide", json=REWORK)

    assert loser.status_code == 409, loser.text
    assert (await _finish(net, first)).status_code == 200
    await _assert_launched_once(db, net, task_id)


async def test_accept_does_not_overwrite_a_rework_in_flight(client, db, net):
    """Принять решением, пока rework в сети, нельзя: completed не затирается."""
    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    first = await _winner_in_network(client, net, task_id)

    loser = await client.post(f"/api/tasks/{task_id}/decide", json={"action": "accept"})

    assert loser.status_code == 409, loser.text
    assert (await _finish(net, first)).status_code == 200
    await _assert_launched_once(db, net, task_id)


async def test_rework_does_not_overwrite_an_accept_that_won(client, db, net):
    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    won = await client.post(f"/api/tasks/{task_id}/decide", json={"action": "accept"})
    assert won.status_code == 200, won.text

    late = await client.post(f"/api/tasks/{task_id}/decide", json=REWORK)

    assert late.status_code in (400, 409), late.text
    assert net.messages == []
    assert (await repo.get_task(db, task_id))["status"] == "completed"


async def test_a_declined_launch_frees_the_next_decision(db, monkeypatch):
    """Диспетчер ничего не принял: задача open, бронь снята, повтор не заперт."""
    calls: list[str] = []

    async def refused(message: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(message)
        return {"error": "нет бинарника"}

    monkeypatch.setattr(plugins.dispatch, "submit_task", refused)
    task_id = await _task_in(db, "needs_decision", submission_generation=1)

    view = await services.decide_task(db, task_id, TaskDecide(**REWORK))

    assert view.status.value == "open" and not view.job_id
    rows = await db.execute_fetchall(
        "SELECT state FROM rework_launches WHERE task_id=?", (task_id,)
    )
    assert [r[0] for r in rows] == ["declined"]


async def _crash_on_submit(db, monkeypatch, *, accepted: bool) -> tuple[int, dict]:
    """Решение rework, при котором процесс «упал» вокруг submit_task.

    ``accepted=False`` - до принятия задания диспетчером; ``True`` - после:
    диспетчер уже завёл job (с текстом заказа), а hub не успел записать job_id.
    """
    jobs: dict[str, Any] = {}

    async def dies(message: str, **_kwargs: Any) -> dict[str, Any]:
        if accepted:
            jobs["job-77"] = {"id": "job-77", "status": "running", "message": message}
        jobs.setdefault("calls", []).append(message)
        raise RuntimeError("процесс умер вокруг submit")

    monkeypatch.setattr(plugins.dispatch, "submit_task", dies)
    monkeypatch.setattr(
        plugins.dispatch,
        "list_jobs",
        lambda limit=50: [v for k, v in jobs.items() if k != "calls"],
    )
    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    with pytest.raises(RuntimeError):
        await services.decide_task(db, task_id, TaskDecide(**REWORK))
    return task_id, jobs


async def _age_reservations(db, task_id: int) -> None:
    await db.execute(
        "UPDATE rework_launches SET reserved_at=? WHERE task_id=?", (LONG_AGO, task_id)
    )
    await db.commit()


async def test_a_crash_around_submit_does_not_launch_twice(db, monkeypatch):
    """AC-2: до принятия задания - к человеку с причиной; после - связь с job."""
    from hub.services import rework_launch

    task_id, jobs = await _crash_on_submit(db, monkeypatch, accepted=False)

    # Рестарт до сверки: повторное решение заперто бронью, а не уходит в сеть.
    with pytest.raises(HTTPException) as again:
        await services.decide_task(db, task_id, TaskDecide(**REWORK))
    assert again.value.status_code == 409
    assert len(jobs["calls"]) == 1

    await _age_reservations(db, task_id)
    await rework_launch.sweep(db)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision" and not task["job_id"]
    assert len(jobs["calls"]) == 1, "сверка запустила задание заново"
    feed = " ".join(
        str(dict(u)["content"]) for u in await repo.get_task_updates(db, task_id)
    )
    assert "не удалось установить" in feed
    rows = await db.execute_fetchall(
        "SELECT state FROM rework_launches WHERE task_id=?", (task_id,)
    )
    assert [r[0] for r in rows] == ["needs_human"]


async def test_a_crash_after_acceptance_links_the_existing_job(db, monkeypatch):
    from hub.services import rework_launch

    task_id, jobs = await _crash_on_submit(db, monkeypatch, accepted=True)
    await _age_reservations(db, task_id)

    await rework_launch.sweep(db)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "fix_requested" and task["job_id"] == "job-77"
    assert len(jobs["calls"]) == 1, "сверка запустила задание заново"
    rows = await db.execute_fetchall(
        "SELECT state, job_id FROM rework_launches WHERE task_id=?", (task_id,)
    )
    assert [tuple(r) for r in rows] == [("launched", "job-77")]


async def test_a_fresh_reservation_is_left_alone_by_the_sweep(db, monkeypatch):
    """Заказ мог ещё идти в сети: свежую бронь сверка не трогает."""
    from hub.services import rework_launch

    task_id, jobs = await _crash_on_submit(db, monkeypatch, accepted=True)

    await rework_launch.sweep(db)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision" and not task["job_id"]
    rows = await db.execute_fetchall(
        "SELECT state FROM rework_launches WHERE task_id=?", (task_id,)
    )
    assert [r[0] for r in rows] == ["reserved"]
