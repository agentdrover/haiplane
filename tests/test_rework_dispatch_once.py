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
    booked = await db.execute_fetchall(
        "SELECT state, job_id FROM rework_launches WHERE task_id=?", (task_id,)
    )
    assert [tuple(r) for r in booked] == [("launched", "job-1")]


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


async def _feed(db, task_id: int) -> str:
    updates = await repo.get_task_updates(db, task_id)
    return " ".join(str(dict(u)["content"]) for u in updates)


async def _age_reservations(db, task_id: int) -> None:
    """Хаб перезапущен: бронь принадлежит прежнему процессу."""
    await db.execute(
        "UPDATE rework_launches SET boot_id='previous-boot' WHERE task_id=?",
        (task_id,),
    )
    await db.commit()


async def test_a_crash_around_submit_does_not_launch_twice(db, monkeypatch):
    """AC-2: до принятия задания - к человеку с причиной; после - связь с job."""
    from hub.services import rework_launch

    task_id, jobs = await _crash_on_submit(db, monkeypatch, accepted=False)

    # Рестарт до сверки: повторное решение заперто бронью, а не уходит в сеть.
    feed = await _feed(db, task_id)
    assert "Заказ доработки оборвался" in feed and "RuntimeError" in feed
    with pytest.raises(HTTPException) as again:
        await services.decide_task(db, task_id, TaskDecide(**REWORK))
    assert again.value.status_code == 409
    assert len(jobs["calls"]) == 1

    await _age_reservations(db, task_id)
    await rework_launch.sweep(db)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision" and not task["job_id"]
    assert len(jobs["calls"]) == 1, "сверка запустила задание заново"
    assert "Повторного запуска нет" in await _feed(db, task_id)
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


async def test_a_merge_run_is_refused_while_a_rework_is_reserved(db, monkeypatch):
    """Прогон «слей базу» (#1445) и доработка по решению не идут на одну задачу."""
    from hub.services import executor_launch as el
    from tests.test_executor_dispatch import (
        _CREATED,
        _creator,
        _human,
        _launch_config,
        _task_in_base_conflict,
    )

    _launch_config(monkeypatch)
    calls = _creator(monkeypatch, [_CREATED])
    _, task_id = await _task_in_base_conflict(db, slug="exec-merge-vs-rework")
    await db.execute("INSERT INTO rework_launches (task_id) VALUES (?)", (task_id,))
    await db.commit()

    result = await el.merge_executor(
        db, task_id, issuer_principal_id=await _human(db), issuer="owner"
    )

    assert not result.launched and el.REASON_ALREADY_RUNNING in result.reason
    assert calls == []


async def test_a_rework_is_refused_while_a_merge_run_is_live(client, db, monkeypatch):
    from hub.services import executor_launch as el
    from tests.test_executor_dispatch import (
        _CREATED,
        _creator,
        _human,
        _launch_config,
        _task_in_base_conflict,
    )

    _launch_config(monkeypatch)
    _creator(monkeypatch, [_CREATED])
    _, task_id = await _task_in_base_conflict(db, slug="exec-rework-vs-merge")
    made = await el.merge_executor(
        db, task_id, issuer_principal_id=await _human(db), issuer="owner"
    )
    assert made.launched, made

    refused = await client.post(f"/api/tasks/{task_id}/decide", json=REWORK)

    assert refused.status_code == 409, refused.text
    assert await _decisions(db, task_id) == 0


async def test_a_state_task_rework_closes_its_booking(client, db, net):
    """state-задача заданием не запускается: бронь не должна остаться reserved."""
    from tests.state_support import make_state_task

    task_id = await make_state_task(db)
    await db.execute(
        "UPDATE tasks SET status='needs_decision', submission_generation=1 WHERE id=?",
        (task_id,),
    )
    await db.commit()

    done = await client.post(f"/api/tasks/{task_id}/decide", json=REWORK)

    assert done.status_code == 200, done.text
    assert net.messages == []
    rows = await db.execute_fetchall(
        "SELECT state FROM rework_launches WHERE task_id=?", (task_id,)
    )
    assert [r[0] for r in rows] == ["closed"]


async def test_reserve_refuses_a_task_that_left_needs_decision(db):
    """Под write-локом статус перечитывается: ушедшая из входа задача брони не получает."""
    from hub.services import rework_launch

    task_id = await _task_in(db, "running")

    with pytest.raises(HTTPException) as lost:
        await rework_launch.reserve(db, task_id)

    assert lost.value.status_code == 409
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM rework_launches WHERE task_id=?", (task_id,)
    )
    assert rows[0][0] == 0


async def test_the_sweep_closes_a_booking_of_a_task_that_moved_on(db, monkeypatch):
    """Задача ушла из needs_decision другим путём: бронь закрывается без заказа."""
    from hub.services import rework_launch

    task_id, jobs = await _crash_on_submit(db, monkeypatch, accepted=False)
    await repo.update_task(db, task_id, status="open")
    await db.commit()
    await _age_reservations(db, task_id)

    await rework_launch.sweep(db)

    rows = await db.execute_fetchall(
        "SELECT state FROM rework_launches WHERE task_id=?", (task_id,)
    )
    assert [r[0] for r in rows] == ["closed"]
    assert len(jobs["calls"]) == 1


async def test_a_repeat_decision_after_an_unproven_outcome_needs_confirmation(
    db, monkeypatch
):
    """Сверка не нашла задание - это не доказательство, что его нет."""
    from hub.services import rework_launch

    task_id, jobs = await _crash_on_submit(db, monkeypatch, accepted=False)
    await _age_reservations(db, task_id)
    await rework_launch.sweep(db)

    with pytest.raises(HTTPException) as again:
        await services.decide_task(db, task_id, TaskDecide(**REWORK))
    assert again.value.status_code == 409 and "confirm_no_job" in again.value.detail
    assert len(jobs["calls"]) == 1

    async def accepts(message: str, **_kwargs: Any) -> dict[str, Any]:
        jobs["calls"].append(message)
        return {"job_id": "job-2"}

    monkeypatch.setattr(plugins.dispatch, "submit_task", accepts)
    view = await services.decide_task(
        db, task_id, TaskDecide(**REWORK, confirm_no_job=True)
    )
    assert view.status.value == "fix_requested" and len(jobs["calls"]) == 2


async def test_an_unparsed_dispatcher_answer_is_not_a_refusal(db, monkeypatch):
    """{raw, exit_code: 0}: задание могло быть принято - бронь остаётся до сверки."""

    async def unparsed(message: str, **_kwargs: Any) -> dict[str, Any]:
        return {"raw": "не json", "exit_code": 0}

    monkeypatch.setattr(plugins.dispatch, "submit_task", unparsed)
    task_id = await _task_in(db, "needs_decision", submission_generation=1)

    await services.decide_task(db, task_id, TaskDecide(**REWORK))

    assert (await repo.get_task(db, task_id))["status"] == "needs_decision"
    rows = await db.execute_fetchall(
        "SELECT state FROM rework_launches WHERE task_id=?", (task_id,)
    )
    assert [r[0] for r in rows] == ["reserved"]


async def test_the_booking_marker_matches_the_whole_line(db, monkeypatch):
    from hub.services import rework_launch

    monkeypatch.setattr(
        plugins.dispatch,
        "list_jobs",
        lambda limit=50: [
            {"id": "other", "message": rework_launch.marker_line(10, 5, "ab")},
            {"id": "wrong-task", "message": rework_launch.marker_line(1, 6, "ab")},
        ],
    )

    assert await rework_launch._find_job(1, 5, "ab") == (None, False)
    assert await rework_launch._find_job(10, 5, "zz") == (None, False)
    found, ambiguous = await rework_launch._find_job(10, 5, "ab")
    assert found["id"] == "other" and not ambiguous


async def test_force_complete_and_resubmission_wait_for_a_booking(client, db, net):
    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    first = await _winner_in_network(client, net, task_id)

    forced = await client.post(
        f"/api/tasks/{task_id}/force-complete", json={"comment": "закрыть"}
    )

    assert forced.status_code == 409, forced.text
    assert (await _finish(net, first)).status_code == 200
    assert (await repo.get_task(db, task_id))["status"] == "fix_requested"


async def test_a_late_answer_does_not_touch_a_booking_the_sweep_closed(db):
    from hub.services import rework_launch

    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    await db.execute(
        "INSERT INTO rework_launches (task_id, state) VALUES (?, 'needs_human')",
        (task_id,),
    )
    await db.commit()
    rid = (await db.execute_fetchall("SELECT MAX(id) FROM rework_launches"))[0][0]

    linked = await rework_launch.settle_launched(db, rid, task_id, "job-late")

    assert linked is False
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision" and not task["job_id"]
    assert "job-late" in await _feed(db, task_id)


async def test_a_merge_run_waits_for_the_human_after_an_unproven_outcome(
    db, monkeypatch
):
    from hub.services import executor_launch as el
    from tests.test_executor_dispatch import (
        _CREATED,
        _creator,
        _human,
        _launch_config,
        _task_in_base_conflict,
    )

    _launch_config(monkeypatch)
    calls = _creator(monkeypatch, [_CREATED])
    _, task_id = await _task_in_base_conflict(db, slug="exec-merge-unproven")
    await db.execute(
        "INSERT INTO rework_launches (task_id, state) VALUES (?, 'needs_human')",
        (task_id,),
    )
    await db.commit()

    result = await el.merge_executor(
        db, task_id, issuer_principal_id=await _human(db), issuer="owner"
    )

    assert not result.launched and "confirm_no_job" in result.reason
    assert calls == []


async def test_force_complete_rereads_the_task_under_the_lock(client, db, monkeypatch):
    """Снимок до лока устарел: между чтением и записью rework записал job_id."""
    from hub.services import rework_launch

    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    real = rework_launch.refuse_if_reserved

    async def job_lands_first(conn, tid):
        await real(conn, tid)
        await repo.update_task(conn, tid, status="fix_requested", job_id="job-9")

    monkeypatch.setattr(rework_launch, "refuse_if_reserved", job_lands_first)

    forced = await client.post(
        f"/api/tasks/{task_id}/force-complete", json={"comment": "закрыть"}
    )

    assert forced.status_code == 409, forced.text
    assert (await repo.get_task(db, task_id))["status"] != "completed"


async def test_the_web_forms_can_confirm_that_no_job_exists(client, db, net):
    """Без поля в форме человек после needs_human упёрся бы в вечный 409."""
    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    await db.execute(
        "INSERT INTO rework_launches (task_id, state) VALUES (?, 'needs_human')",
        (task_id,),
    )
    await db.commit()
    page = (await client.get(f"/tasks/{task_id}")).text
    assert page.count('name="confirm_no_job"') >= 2  # лист и форма «Подробностей»

    refused = await client.post(
        f"/tasks/{task_id}/web-decide", data=REWORK, follow_redirects=False
    )
    assert refused.status_code == 409
    net.release.set()
    done = await client.post(
        f"/tasks/{task_id}/web-decide",
        data={**REWORK, "confirm_no_job": "on"},
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert len(net.messages) == 1
