"""Бронь доработки #1991: сверка по границе процесса, а не по таймеру.

Время не доказывает, что прежний отправитель остановился. Бронь закрывает
сверка только после перезапуска хаба (другой boot id), а исход ``needs_human``
снимается найденным заданием с собственной меткой.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from hub import repository as repo
from hub import services
from hub.integrations.registry import plugins
from hub.models import TaskDecide
from tests.test_decision_brief import _task_in
from tests.test_rework_dispatch_once import LONG_AGO, REWORK, _crash_on_submit


async def _restart(db, task_id: int) -> None:
    """Хаб перезапущен: бронь принадлежит прежнему процессу (и давно)."""
    cols = {
        r[1] for r in await db.execute_fetchall("PRAGMA table_info(rework_launches)")
    }
    if "boot_id" in cols:
        await db.execute(
            "UPDATE rework_launches SET boot_id='previous-boot' WHERE task_id=?",
            (task_id,),
        )
    await db.execute(
        "UPDATE rework_launches SET reserved_at=? WHERE task_id=?", (LONG_AGO, task_id)
    )
    await db.commit()


async def _states(db, task_id: int) -> list[str]:
    rows = await db.execute_fetchall(
        "SELECT state FROM rework_launches WHERE task_id=? ORDER BY id", (task_id,)
    )
    return [r[0] for r in rows]


async def test_a_delayed_sender_in_a_live_process_is_never_swept(
    client, db, monkeypatch
):
    """Отправитель завис в сети дольше любого таймера: бронь держится, заказ один."""
    from hub.services import rework_launch

    in_net, release, sent = asyncio.Event(), asyncio.Event(), []

    async def submit(message: str, **_kwargs: Any) -> dict[str, Any]:
        sent.append(message)
        in_net.set()
        await asyncio.wait_for(release.wait(), 10)
        return {"job_id": "job-1"}

    monkeypatch.setattr(plugins.dispatch, "submit_task", submit)
    monkeypatch.setattr(plugins.dispatch, "list_jobs", lambda limit=50: [])
    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    attempt = asyncio.create_task(
        client.post(f"/api/tasks/{task_id}/decide", json=REWORK)
    )
    await asyncio.wait_for(in_net.wait(), 10)
    await db.execute(
        "UPDATE rework_launches SET reserved_at=? WHERE task_id=?", (LONG_AGO, task_id)
    )
    await db.commit()

    await rework_launch.sweep(db)
    again = await client.post(
        f"/api/tasks/{task_id}/decide", json={**REWORK, "confirm_no_job": True}
    )

    assert await _states(db, task_id) == ["reserved"]
    assert again.status_code == 409
    release.set()
    assert (await asyncio.wait_for(attempt, 10)).status_code == 200
    assert len(sent) == 1
    assert (await repo.get_task(db, task_id))["job_id"] == "job-1"


async def test_sweep_reconciles_a_booking_of_a_previous_process(db, monkeypatch):
    from hub.services import rework_launch

    task_id, jobs = await _crash_on_submit(db, monkeypatch, accepted=True)
    await db.execute(
        "UPDATE rework_launches SET boot_id=? WHERE task_id=?",
        (rework_launch.boot_id(), task_id),
    )
    await db.commit()
    await rework_launch.sweep(db)
    assert await _states(db, task_id) == ["reserved"], "тот же процесс: не трогать"

    await _restart(db, task_id)
    await rework_launch.sweep(db)

    assert await _states(db, task_id) == ["launched"]
    assert (await repo.get_task(db, task_id))["job_id"] == "job-77"


async def test_a_found_job_lifts_an_unproven_outcome(db, monkeypatch):
    """needs_human, затем у диспетчера появилось задание с точной меткой."""
    from hub.services import rework_launch

    task_id, jobs = await _crash_on_submit(db, monkeypatch, accepted=False)
    await _restart(db, task_id)
    await rework_launch.sweep(db)
    assert await _states(db, task_id) == ["needs_human"]
    jobs["job-late"] = {"id": "job-late", "message": jobs["calls"][0]}

    await rework_launch.sweep(db)

    assert await _states(db, task_id) == ["launched"]
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "fix_requested" and task["job_id"] == "job-late"
    assert len(jobs["calls"]) == 1, "нового submit нет"


async def test_accept_closes_an_unproven_outcome(db):
    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    await db.execute(
        "INSERT INTO rework_launches (task_id, state) VALUES (?, 'needs_human')",
        (task_id,),
    )
    await db.commit()

    await services.decide_task(db, task_id, TaskDecide(action="accept"))

    assert await _states(db, task_id) == ["closed"]


async def test_force_complete_closes_an_unproven_outcome(client, db):
    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    await db.execute(
        "INSERT INTO rework_launches (task_id, state) VALUES (?, 'needs_human')",
        (task_id,),
    )
    await db.commit()

    forced = await client.post(
        f"/api/tasks/{task_id}/force-complete", json={"comment": "закрыть"}
    )

    assert forced.status_code == 200, forced.text
    assert await _states(db, task_id) == ["closed"]


async def test_archiving_closes_an_unproven_outcome_but_not_a_live_booking(db):
    done = await _task_in(db, "needs_decision", submission_generation=1)
    live = await _task_in(db, "needs_decision", submission_generation=1)
    await db.execute(
        "INSERT INTO rework_launches (task_id, state) VALUES (?, 'needs_human')",
        (done,),
    )
    await db.execute("INSERT INTO rework_launches (task_id) VALUES (?)", (live,))
    await db.commit()

    for task_id in (done, live):
        await services.archive_task(db, task_id, reason="уборка")

    assert await _states(db, done) == ["closed"]
    assert await _states(db, live) == ["reserved"]


async def test_a_finished_sender_is_reconciled_without_a_restart(db, monkeypatch):
    """Submit вернул ошибку: отправителя больше нет, ждать перезапуска незачем."""
    from hub.services import rework_launch

    task_id, jobs = await _crash_on_submit(db, monkeypatch, accepted=True)

    await rework_launch.sweep(db)

    assert await _states(db, task_id) == ["launched"]
    assert (await repo.get_task(db, task_id))["job_id"] == "job-77"
    assert len(jobs["calls"]) == 1


async def test_a_stale_sweep_does_not_link_a_job_to_a_superseded_booking(
    db, monkeypatch
):
    """Пока сверка искала задание, человек повторил решение: старый исход не нужен."""
    from hub.services import rework_launch

    task_id, jobs = await _crash_on_submit(db, monkeypatch, accepted=False)
    await _restart(db, task_id)
    await rework_launch.sweep(db)
    old = (await db.execute_fetchall("SELECT * FROM rework_launches"))[-1]
    held = dict(old)
    jobs["job-old"] = {"id": "job-old", "message": jobs["calls"][0]}

    real_find = rework_launch._find_job

    async def find_then_supersede(*args: Any) -> Any:
        found = await real_find(*args)
        await db.execute(
            "INSERT INTO rework_launches (task_id, state) VALUES (?, 'needs_human')",
            (task_id,),
        )
        await db.commit()
        return found

    monkeypatch.setattr(rework_launch, "_find_job", find_then_supersede)

    result = await rework_launch._lift_unproven(db, held)

    assert result == "needs_human"
    assert (await repo.get_task(db, task_id))["job_id"] in (None, "")


async def test_a_failed_result_write_frees_the_booking_for_reconciliation(
    db, monkeypatch
):
    """Диспетчер принял задание, а запись результата сорвалась: бронь не вечна."""
    from hub.services import rework_launch

    jobs: dict[str, Any] = {}

    async def accepts(message: str, **_kwargs: Any) -> dict[str, Any]:
        jobs["job-5"] = {"id": "job-5", "message": message}
        return {"job_id": "job-5"}

    async def broken(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(plugins.dispatch, "submit_task", accepts)
    monkeypatch.setattr(
        plugins.dispatch, "list_jobs", lambda limit=50: list(jobs.values())
    )
    monkeypatch.setattr(rework_launch, "settle_launched", broken)
    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    with pytest.raises(RuntimeError):
        await services.decide_task(db, task_id, TaskDecide(**REWORK))
    monkeypatch.undo()
    monkeypatch.setattr(
        plugins.dispatch, "list_jobs", lambda limit=50: list(jobs.values())
    )

    await rework_launch.sweep(db)

    assert await _states(db, task_id) == ["launched"]
    assert (await repo.get_task(db, task_id))["job_id"] == "job-5"


async def test_a_failure_after_the_decision_commit_frees_the_booking(db, monkeypatch):
    """Решение записано, а обработка упала до диспетчера: бронь не вечна."""
    from hub.services import rework_launch, state_task
    from tests.state_support import make_state_task

    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("упало после коммита решения")

    monkeypatch.setattr(state_task, "rework_without_dispatch", boom)
    task_id = await make_state_task(db)
    await db.execute(
        "UPDATE tasks SET status='needs_decision', submission_generation=1 WHERE id=?",
        (task_id,),
    )
    await db.commit()
    with pytest.raises(RuntimeError):
        await services.decide_task(db, task_id, TaskDecide(**REWORK))

    await rework_launch.sweep(db)

    assert await _states(db, task_id) == ["needs_human"]
