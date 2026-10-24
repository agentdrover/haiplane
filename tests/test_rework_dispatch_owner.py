"""Бронь доработки #1991: один процесс на базу и отмена на COMMIT брони.

Восстановление по boot_id верно, пока на одной базе живёт один процесс хаба.
Второй экземпляр не должен ни сверять брони, ни заказывать доработку.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
from typing import Any

import pytest
from fastapi import HTTPException

from hub import config
from hub import repository as repo
from hub import services
from hub.integrations.registry import plugins
from hub.models import TaskDecide
from tests.test_decision_brief import _task_in
from tests.test_rework_dispatch_once import REWORK, _crash_on_submit


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    path = tmp_path / "hub.db"
    monkeypatch.setattr(config, "HUB_DB_PATH", path)
    return path


def _other_process_holds(path):
    """Другой экземпляр хаба: свой open того же файла блокировки."""
    fd = open(f"{path}.owner.lock", "a+")  # noqa: SIM115
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


async def test_a_second_instance_neither_sweeps_nor_orders(db, monkeypatch, lock_path):
    from hub.services import rework_launch

    rework_launch.claim_process(lock_path, "test")  # приложение на этой базе
    task_id, jobs = await _crash_on_submit(db, monkeypatch, accepted=True)
    await db.execute(
        "UPDATE rework_launches SET boot_id='previous-boot' WHERE task_id=?",
        (task_id,),
    )
    await db.commit()
    rework_launch.release_all()  # другой процесс держит блокировку, не мы
    holder = _other_process_holds(lock_path)
    try:
        assert rework_launch.claim_process(lock_path) is False

        await rework_launch.sweep(db)
        rows = await db.execute_fetchall(
            "SELECT state FROM rework_launches WHERE task_id=?", (task_id,)
        )
        assert [r[0] for r in rows] == ["reserved"], "второй экземпляр сверил бронь"

        other = await _task_in(db, "needs_decision", submission_generation=1)
        with pytest.raises(HTTPException) as refused:
            await services.decide_task(db, other, TaskDecide(**REWORK))
        assert refused.value.status_code == 503
        assert len(jobs["calls"]) == 1
    finally:
        holder.close()

    # Первый экземпляр ушёл: право берётся при следующей попытке.
    await rework_launch.sweep(db)
    rows = await db.execute_fetchall(
        "SELECT state FROM rework_launches WHERE task_id=?", (task_id,)
    )
    assert [r[0] for r in rows] == ["launched"]
    rework_launch.release_process(lock_path)


async def test_the_process_lock_is_taken_once_per_process(lock_path):
    from hub.services import rework_launch

    assert rework_launch.claim_process(lock_path) is True
    assert rework_launch.claim_process(lock_path) is True  # второе приложение
    rework_launch.release_process(lock_path)
    rework_launch.release_process(lock_path)
    holder = _other_process_holds(lock_path)
    try:
        assert rework_launch.claim_process(lock_path) is False
    finally:
        holder.close()
    rework_launch.release_process(lock_path)


async def test_a_cancel_on_the_booking_commit_frees_the_booking(db, monkeypatch):
    """Отмена на COMMIT брони: решение записано, до release_sender раньше не доходило."""
    from hub.services import rework_launch

    sent: list[str] = []

    async def submit(message: str, **_kwargs: Any) -> dict[str, Any]:
        sent.append(message)
        return {"job_id": f"job-{len(sent)}"}

    monkeypatch.setattr(plugins.dispatch, "submit_task", submit)
    monkeypatch.setattr(plugins.dispatch, "list_jobs", lambda limit=50: [])
    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    loop = asyncio.get_running_loop()
    seen = {"booked": False, "task": None}

    def trace(statement: str) -> None:
        if statement.startswith("INSERT INTO rework_launches"):
            seen["booked"] = True
        elif statement.startswith("COMMIT") and seen["booked"] and seen["task"]:
            seen["booked"] = False
            loop.call_soon_threadsafe(seen["task"].cancel)

    await db.set_trace_callback(trace)
    attempt = asyncio.create_task(
        services.decide_task(db, task_id, TaskDecide(**REWORK))
    )
    seen["task"] = attempt
    with pytest.raises(asyncio.CancelledError):
        await attempt
    await db.set_trace_callback(None)

    assert sent == []
    await rework_launch.sweep(db)
    rows = await db.execute_fetchall(
        "SELECT state FROM rework_launches WHERE task_id=?", (task_id,)
    )
    assert [r[0] for r in rows] == ["needs_human"], "бронь осталась занятой"

    view = await services.decide_task(
        db, task_id, TaskDecide(**REWORK, confirm_no_job=True)
    )
    assert view.status.value == "fix_requested" and len(sent) == 1
    assert dict(await repo.get_task(db, task_id))["job_id"] == "job-1"


async def test_the_lock_creates_its_directory_and_never_grants_ownership_on_error(
    tmp_path,
):
    from hub.services import rework_launch

    fresh = tmp_path / "not-yet" / "hub.db"
    assert rework_launch.claim_process(fresh) is True
    rework_launch.release_process(fresh)

    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert rework_launch.claim_process(blocker / "hub.db") is False
    rework_launch.release_process(blocker / "hub.db")


async def test_an_app_that_never_held_the_lock_cannot_grant_ownership(lock_path):
    """Два приложения без блокировки; уход одного не делает оставшееся владельцем."""
    from hub.services import rework_launch

    holder = _other_process_holds(lock_path)
    try:
        first, second = object(), object()
        assert rework_launch.claim_process(lock_path, first) is False
        assert rework_launch.claim_process(lock_path, second) is False
        rework_launch.release_process(lock_path, first)

        assert rework_launch.owns_db() is False
    finally:
        holder.close()
    rework_launch.release_process(lock_path, second)


async def test_a_forked_child_inherits_neither_ownership_nor_boot_id(
    lock_path, monkeypatch
):
    from hub.services import rework_launch

    assert rework_launch.claim_process(lock_path) is True
    parent_boot = rework_launch.boot_id()
    # Родитель держит своё открытое описание файла (после fork их два).
    parent_copy = os.dup(rework_launch._OWNERS[str(lock_path)].handle.fileno())
    child_pid = os.getpid() + 100000
    monkeypatch.setattr(rework_launch.os, "getpid", lambda: child_pid)

    assert rework_launch.owns_db() is False, "потомок унаследовал владение"
    assert rework_launch.boot_id() != parent_boot, "потомок унаследовал boot id"
    monkeypatch.undo()
    os.close(parent_copy)
    rework_launch.release_all()


async def test_a_lazily_taken_lock_is_released_with_its_last_app(lock_path):
    """A ушёл после двух отказов B1 и B2; lock взят лениво и закрывается с B2."""
    from hub.services import rework_launch

    holder = _other_process_holds(lock_path)
    first, second = object(), object()
    assert rework_launch.claim_process(lock_path, first) is False
    assert rework_launch.claim_process(lock_path, second) is False
    rework_launch.release_process(lock_path, first)
    holder.close()  # A освободил блокировку

    assert rework_launch.owns_db() is True, "второе приложение вправе взять lock"
    rework_launch.release_process(lock_path, second)

    probe = _other_process_holds(lock_path)  # BlockingIOError, если дескриптор жив
    probe.close()


async def test_owns_db_takes_no_lock_without_a_registered_app(lock_path):
    from hub.services import rework_launch

    rework_launch.release_all()

    assert rework_launch.owns_db() is False
    probe = _other_process_holds(lock_path)
    probe.close()


def test_the_database_path_fixture_survives_a_monkeypatch_undo(monkeypatch):
    before = config.HUB_DB_PATH
    assert "hubdb" in str(before)

    monkeypatch.setattr(config, "HUB_DB_PATH", before.parent / "other.db")
    monkeypatch.undo()

    assert config.HUB_DB_PATH == before, "undo() вернул реальный путь базы"


async def test_a_symlinked_database_shares_one_lock(tmp_path, monkeypatch):
    from hub.services import rework_launch

    real = tmp_path / "real.db"
    link = tmp_path / "link.db"
    link.symlink_to(real)
    assert rework_launch.claim_process(real, "a") is True

    assert rework_launch.claim_process(link, "b") is True  # тот же процесс
    probe_path = f"{real}.owner.lock"
    with pytest.raises(BlockingIOError):
        fd = open(probe_path, "a+")  # noqa: SIM115
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            fd.close()
    assert not (tmp_path / "link.db.owner.lock").exists(), "второй lock-файл"
    rework_launch.release_all()


async def test_a_failed_startup_does_not_leak_its_registration(monkeypatch):
    from hub import app as app_mod
    from hub.services import rework_launch

    async def broken():
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(app_mod, "get_db", broken)
    registered = {k: list(v) for k, v in rework_launch._APPS.items()}

    with pytest.raises(RuntimeError, match="база недоступна"):
        async with app_mod.lifespan(app_mod.app):
            pass

    assert rework_launch._APPS == registered, "заявка приложения осталась"
