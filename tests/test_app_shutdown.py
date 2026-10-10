"""Остановка хаба ограничена по времени и называет виновного (#1667).

10.10.2026 дочерний процесс хаба после ``with TestClient(app)`` дважды не
завершился на Linux-раннере, и CI висел по часу. Причина не доказана; остановка
поэтому закрывает все кандидаты разом: у каждой фоновой задачи есть handle, все
отменяются и ожидаются под ОБЩИМ пределом, незавершённая названа в логе, а
соединение, открывавшееся в момент отмены, всё равно закрывается (рабочий поток
aiosqlite не демон: незакрытое соединение держит выход интерпретатора).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

_STUCK_CHILD = """
import asyncio, logging, sys
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
from hub import poller

async def stuck(app):
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        print("swallowed-first-cancel", flush=True)
    # игнорирует отмену остановки хаба; вторая отмена (закрытие цикла) её снимет
    await asyncio.sleep(25)

poller._drift_watch = stuck
from fastapi.testclient import TestClient
from hub.app import app
with TestClient(app) as client:
    assert client.get("/api/tasks").status_code == 200
print("exited-with-block", flush=True)
"""


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    import os

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("HAIPLANE_", ("open" + "claw").upper() + "_"))
    }
    env["PYTHONPATH"] = str(_REPO_ROOT)
    env["HAIPLANE_HUB_DB"] = str(tmp_path / "hub.db")
    env.update(extra)
    return env


def test_shutdown_is_bounded_and_names_the_stuck_task(tmp_path: Path) -> None:
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-u", "-c", _STUCK_CHILD],
        capture_output=True,
        text=True,
        env=_env(tmp_path, HAIPLANE_STOP_TIMEOUT_SECONDS="2"),
        cwd=str(_REPO_ROOT),
        timeout=30,
    )
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "swallowed-first-cancel" in proc.stdout, proc.stdout + proc.stderr
    assert elapsed < 30
    # the log names the task that did not stop and the limit that was applied
    assert "hub-drift-watch" in proc.stderr, proc.stderr
    assert "did not stop within 2s" in proc.stderr, proc.stderr


async def test_stop_background_returns_at_the_limit_and_names_every_laggard(caplog):
    from hub import app as hub_app

    async def stubborn():
        # ignores cancel for a long while: the stop must not wait for it
        for _ in range(100):
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                continue

    async def obedient():
        await asyncio.sleep(60)

    tasks = {
        "hub-stubborn-a": asyncio.create_task(stubborn(), name="hub-stubborn-a"),
        "hub-stubborn-b": asyncio.create_task(stubborn(), name="hub-stubborn-b"),
        "hub-obedient": asyncio.create_task(obedient(), name="hub-obedient"),
    }
    app = SimpleNamespace(state=SimpleNamespace(background_tasks=dict(tasks)))
    poll = asyncio.create_task(obedient(), name="hub-poller")
    await asyncio.sleep(0)
    with (
        patch.object(hub_app.config, "STOP_TIMEOUT_SECONDS", 1),
        caplog.at_level(logging.WARNING, logger="hub"),
    ):
        started = time.monotonic()
        await hub_app._stop_background(app, poll)
        elapsed = time.monotonic() - started
    assert 0.9 <= elapsed < 3, elapsed  # one shared limit, not one per task
    text = caplog.text
    assert "hub-stubborn-a" in text and "hub-stubborn-b" in text
    assert "hub-obedient" not in text and "hub-poller" not in text
    assert tasks["hub-obedient"].cancelled() and poll.cancelled()
    for t in tasks.values():
        t.cancel()
    await asyncio.gather(*tasks.values(), return_exceptions=True)


async def test_a_hanging_stop_step_is_named_and_does_not_hold_the_stop(caplog):
    from hub import app as hub_app

    async def hang():
        await asyncio.sleep(60)

    app = SimpleNamespace(state=SimpleNamespace(background_tasks={}))
    poll = asyncio.create_task(hang(), name="hub-poller")
    step = None
    with (
        patch.object(hub_app.config, "STOP_TIMEOUT_SECONDS", 1),
        caplog.at_level(logging.WARNING, logger="hub"),
    ):
        started = time.monotonic()
        coro = hang()
        await hub_app._stop_background(app, poll, steps={"local review runs": coro})
        assert time.monotonic() - started < 3
    assert "local review runs" in caplog.text
    # the step is NOT cancelled by the limit: a second cancel is what cut the
    # withdrawal of a published job
    step = [t for t in asyncio.all_tasks() if t.get_coro() is coro][0]
    await asyncio.sleep(0.05)  # let a (wrong) cancel be delivered
    assert not step.cancelled() and not step.done()
    step.cancel()
    await asyncio.gather(step, return_exceptions=True)


async def test_local_job_is_withdrawn_even_when_the_poller_uses_the_whole_budget():
    """P1 (#1667): снятие локальных прогонов не ждёт поллер и не урезается им.

    Поллер не отпускает до конца бюджета. Прогон, ждущий отзыва задания, всё
    равно успевает его отозвать и закрыть строку.
    """
    from hub import app as hub_app
    from hub.services import review_dispatch as rd

    events: list[str] = []

    async def run():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            # как local_reviewer._run_via_runner: ждёт поток публикации и
            # отзывает задание; повторная отмена здесь оборвала бы отзыв
            await asyncio.sleep(0.3)
            events.append("withdrawn:job-1")
            raise

    async def stubborn_poller():
        # не отпускает до конца бюджета (1 с)
        for _ in range(30):
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                continue

    run_task = asyncio.create_task(run())
    poll = asyncio.create_task(stubborn_poller(), name="hub-poller")
    handle = rd._LocalRunHandle(
        task=run_task, db_path="", dispatch_id=77001, task_id=1, generation=1
    )
    closed: list[int] = []

    async def fake_close(h):
        closed.append(h.dispatch_id)

    rd._LOCAL_RUNS[77001] = handle
    await asyncio.sleep(0)
    app = SimpleNamespace(state=SimpleNamespace(background_tasks={}))
    try:
        with (
            patch.object(hub_app.config, "STOP_TIMEOUT_SECONDS", 1),
            patch.object(rd, "_close_cancelled_run", fake_close),
        ):
            await hub_app._stop_background(
                app, poll, steps={"local review runs": rd.cancel_local_runs()}
            )
    finally:
        rd._LOCAL_RUNS.pop(77001, None)
        poll.cancel()
    assert events == ["withdrawn:job-1"]
    assert closed == [77001]


async def test_local_withdrawal_survives_a_second_cancel_of_the_step():
    from hub.services import review_dispatch as rd

    events: list[str] = []

    async def run():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(0.3)
            events.append("withdrawn")
            raise

    run_task = asyncio.create_task(run())
    rd._LOCAL_RUNS[77002] = rd._LocalRunHandle(
        task=run_task, db_path="", dispatch_id=77002, task_id=1, generation=1
    )
    await asyncio.sleep(0)
    step = asyncio.ensure_future(rd.cancel_local_runs())
    await asyncio.sleep(0.05)
    step.cancel()  # the second cancel
    await asyncio.gather(step, return_exceptions=True)
    await asyncio.sleep(0.5)
    rd._LOCAL_RUNS.pop(77002, None)
    assert events == ["withdrawn"]


async def test_advisor_withdrawal_survives_a_second_cancel_of_the_step():
    from hub.services import steward_advisor_local as sal

    events: list[str] = []

    async def run():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(0.3)
            events.append("withdrawn")
            raise

    closed: list[int] = []

    async def fake_close(h):
        closed.append(h.run_id)

    handle = sal._Handle(
        run_id=77003, task_id=1, generation=1, model="m", db_path="", live_db=None
    )
    handle.task = asyncio.create_task(run())
    sal._HANDLES[77003] = handle
    await asyncio.sleep(0)
    try:
        with patch.object(sal, "_close_stopped", fake_close):
            step = asyncio.ensure_future(sal.cancel_local_advisors())
            await asyncio.sleep(0.05)
            step.cancel()  # the second cancel
            await asyncio.gather(step, return_exceptions=True)
            await asyncio.sleep(0.5)
    finally:
        sal._HANDLES.pop(77003, None)
    assert events == ["withdrawn"]


async def test_a_failing_stop_step_is_logged_by_name_and_the_stop_goes_on(caplog):
    import warnings

    from hub import app as hub_app

    async def boom():
        raise RuntimeError("step exploded")

    async def fine():
        await asyncio.sleep(0.05)

    app = SimpleNamespace(state=SimpleNamespace(background_tasks={}))
    poll = asyncio.create_task(fine(), name="hub-poller")
    handler_calls = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _l, ctx: handler_calls.append(ctx))
    try:
        with caplog.at_level(logging.ERROR, logger="hub"), warnings.catch_warnings():
            warnings.simplefilter("error")
            await hub_app._stop_background(
                app, poll, steps={"exploding step": boom(), "fine step": fine()}
            )
            import gc

            gc.collect()
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)
    assert "exploding step" in caplog.text
    assert "step exploded" in caplog.text
    assert "fine step" not in caplog.text
    assert handler_calls == []  # no "Task exception was never retrieved"
    assert poll.done()


def _open_connection_count(conn) -> bool:
    try:
        conn._conn.execute("select 1")  # noqa: SLF001 - probing liveness
    except (sqlite3.ProgrammingError, ValueError, AttributeError):
        return False
    return True


async def test_own_connection_is_closed_when_cancelled_while_opening(tmp_path):
    from hub import db as hub_db
    from hub import poller

    opened = []
    release = asyncio.Event()

    async def slow_connect(dsn):
        await release.wait()  # still "opening" when the cancel arrives
        conn = await hub_db.connect(dsn)
        opened.append(conn)
        return conn

    app = SimpleNamespace(state=SimpleNamespace(dsn=str(tmp_path / "x.db")))

    async def user():
        async with poller._own_connection(app):
            await asyncio.sleep(60)

    with patch.object(poller, "db_connect", slow_connect):
        task = asyncio.create_task(user())
        await asyncio.sleep(0.05)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            if opened and not opened[0]._thread.is_alive():  # noqa: SLF001
                break
            await asyncio.sleep(0.05)
    assert opened, "the connection was never opened"
    assert not opened[0]._thread.is_alive(), "worker thread of a leaked connection"  # noqa: SLF001


async def test_own_connection_is_closed_when_cancelled_while_in_use(tmp_path):
    """Регрессионный тест, не RED: на develop он тоже проходит (finally там был)."""
    from hub import poller

    seen = []
    entered = asyncio.Event()
    app = SimpleNamespace(state=SimpleNamespace(dsn=str(tmp_path / "y.db")))

    async def user():
        async with poller._own_connection(app) as conn:
            seen.append(conn)
            entered.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(user())
    await asyncio.wait_for(entered.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    seen[0]._thread.join(timeout=10)  # noqa: SLF001
    assert not seen[0]._thread.is_alive()  # noqa: SLF001


async def test_start_poller_keeps_a_handle_of_every_background_task():
    from hub import poller

    async def idle(*_a, **_k):
        await asyncio.sleep(60)

    app = SimpleNamespace(state=SimpleNamespace(db=None))
    with (
        patch.object(poller, "_poll_running_tasks", idle),
        patch.object(poller, "_session_reaper", idle),
        patch.object(poller, "_drift_watch", idle),
        patch.object(poller, "_red_base_watch", idle),
        patch.object(poller, "arm_workspace_hooks", idle),
        patch("hub.services.egress_watch.run_loop", idle),
    ):
        main = poller.start_poller(app)
        handles = app.state.background_tasks
        try:
            assert main in handles.values()
            assert app.state.egress_task in handles.values()
            assert {
                "hub-poller",
                "hub-egress-watch",
                "hub-session-reaper",
                "hub-drift-watch",
                "hub-red-base-watch",
                "hub-arm-workspace-hooks",
            } == set(handles)
            assert all(t.get_name() == n for n, t in handles.items())
        finally:
            for t in handles.values():
                t.cancel()
            await asyncio.gather(*handles.values(), return_exceptions=True)
