"""Таймаут submit с НАСТОЯЩИМ дочерним процессом на полной трубе (#1991).

Двойник, чей wait() возвращается сразу, полную трубу не видит: на CPython wait()
просыпается, лишь когда закрыт transport, а читатель остановился. Здесь дочерний
процесс непрерывно пишет в stdout и stderr.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import stat
import sys
import time
from typing import Any

import pytest

from hub import services
from hub.integrations import dispatch as dispatch_mod
from hub.integrations.registry import plugins
from hub.models import TaskDecide
from tests.test_decision_brief import _task_in
from tests.test_rework_dispatch_once import REWORK

#: Срок submit в тесте и потолок: время на закрытие transport и reap (5 с в kill).
#: Потолок - граница зависания, а не утверждение о скорости: настоящий зависший
#: reap не возвращается никогда, поэтому запас на загруженной машине (5 с сверх
#: границы kill) чувствительность не снижает.
TEST_TIMEOUT = 1.5
CEILING = 10.0
#: Общий срок ожидания наблюдаемого условия (запуск, смерть группы). Условие
#: на свободной машине наступает за миллисекунды; на перегруженной запуск
#: дочернего python занимает секунды (замер: >10 с при 24 busy-loop и 12 pytest
#: на 8 ядрах), поэтому срок щедрый. Он лишь не даёт тесту висеть вечно.
WAIT = 30.0

SCRIPT = """#!{python}
import os, sys
open(os.environ["REAP_PIDFILE"], "w").write(str(os.getpid()))
while True:
    sys.stdout.write("x" * 65536); sys.stdout.flush()
    sys.stderr.write("y" * 65536); sys.stderr.flush()
"""


@pytest.fixture
def flooding_child(tmp_path, monkeypatch):
    script = tmp_path / "flood.py"
    script.write_text(SCRIPT.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("REAP_PIDFILE", str(pidfile))
    monkeypatch.setattr(dispatch_mod, "DISPATCH_BIN", str(script))
    monkeypatch.setattr(dispatch_mod, "SUBMIT_TIMEOUT", TEST_TIMEOUT)
    yield pidfile
    with contextlib.suppress(Exception):
        os.kill(int(pidfile.read_text()), signal.SIGKILL)


class Spawn:
    """Следит за create_subprocess_exec: процесс, pid и удержание spawn.

    Тест ждёт события (``attached``, полная труба, смерть pid), а не отрезка
    времени. ``hold`` оставляет spawn «подключающим трубы» до ``release``.
    """

    def __init__(self, hold: bool = False) -> None:
        self.hold = hold
        self.procs: list[Any] = []
        self.attached = asyncio.Event()
        self.release = asyncio.Event()
        self.reaping = asyncio.Event()

    @property
    def pid(self) -> int:
        assert self.procs, "процесс не был запущен"
        return self.procs[0].pid

    def pipe_full(self) -> bool:
        """Читатель хотя бы одной трубы остановлен: буфер полон, ребёнок заблокирован.

        Какая из двух труб остановится первой, зависит от планировщика; для
        зависания reap достаточно любой: остановленная труба не доходит до EOF.
        """
        if not self.procs:
            return False
        transport = self.procs[0]._transport
        pipes = (transport.get_pipe_transport(fd) for fd in (1, 2))
        return any(p is not None and not p.is_reading() for p in pipes)


@pytest.fixture
async def spawn_log(monkeypatch):
    spawn = Spawn()
    real = asyncio.create_subprocess_exec

    async def watched(*args, **kwargs):
        proc = await real(*args, **kwargs)
        spawn.procs.append(proc)
        spawn.attached.set()
        if spawn.hold:
            await spawn.release.wait()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", watched)

    real_reap = dispatch_mod._reap_spawned

    async def observed_reap(spawned):
        spawn.reaping.set()
        await real_reap(spawned)

    monkeypatch.setattr(dispatch_mod, "_reap_spawned", observed_reap)
    yield spawn
    spawn.release.set()
    for proc in spawn.procs:
        with contextlib.suppress(Exception):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc._transport.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=WAIT)


async def _within(coro, seconds: float):
    """Результат корутины или провал теста, если она не вернулась в срок.

    ``wait_for`` здесь не годится: отмена зависшего reap сама зависает.
    """
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=seconds)
    assert done, "вызов завис: ожидание без потолка"
    return task.result()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_for(condition, what: str, seconds: float = WAIT) -> None:
    """Ждёт условие опросом; по истечении срока - провал с названием условия."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while not condition():
        assert loop.time() < end, f"не дождались: {what} (за {seconds} с)"
        await asyncio.sleep(0.01)


async def _hold_until_pipe_full(spawn: Spawn) -> None:
    """Держит spawn, пока ребёнок не заполнит трубу, затем отпускает его.

    Срок submit (SUBMIT_TIMEOUT) начинается только после этого: на медленном
    старте ребёнок иначе убит до заполнения трубы, и дефектный reap не виснет.
    """
    await _wait_for(spawn.attached.is_set, "процесс запущен, spawn удержан")
    await _wait_for(spawn.pipe_full, "труба заполнена, читатель остановлен")
    spawn.release.set()


async def test_a_timed_out_submit_returns_on_a_full_pipe(flooding_child, spawn_log):
    spawn_log.hold = True
    task = asyncio.ensure_future(
        dispatch_mod.DispatchIntegration().submit_task("m", task_id=1)
    )
    await _hold_until_pipe_full(spawn_log)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await _within(task, TEST_TIMEOUT + CEILING)
    assert time.monotonic() - started < TEST_TIMEOUT + CEILING
    await _wait_for(lambda: not _alive(spawn_log.pid), "смерть дочернего процесса")


async def test_a_full_pipe_timeout_frees_the_booking(
    db, monkeypatch, flooding_child, spawn_log
):
    from hub.services import rework_launch

    monkeypatch.setattr(plugins, "dispatch", dispatch_mod.DispatchIntegration())
    task_id = await _task_in(db, "needs_decision", submission_generation=1)

    spawn_log.hold = True
    decide = asyncio.ensure_future(
        services.decide_task(db, task_id, TaskDecide(**REWORK))
    )
    await _hold_until_pipe_full(spawn_log)
    with pytest.raises(TimeoutError):
        await _within(decide, TEST_TIMEOUT + CEILING)

    rows = await db.execute_fetchall(
        "SELECT state, boot_id FROM rework_launches WHERE task_id=?", (task_id,)
    )
    assert [tuple(r) for r in rows] == [("reserved", "")]
    monkeypatch.setattr(plugins.dispatch, "list_jobs", lambda limit=50: [])
    await rework_launch.sweep(db)
    rows = await db.execute_fetchall(
        "SELECT state FROM rework_launches WHERE task_id=?", (task_id,)
    )
    assert [r[0] for r in rows] == ["needs_human"]


async def test_a_cancel_during_spawn_still_reaps_the_group(flooding_child, spawn_log):
    """Отмена, пока spawn подключает трубы: дочерний процесс не остаётся сиротой."""
    spawn_log.hold = True
    task = asyncio.ensure_future(
        dispatch_mod.DispatchIntegration().submit_task("m", task_id=1)
    )
    await _wait_for(spawn_log.attached.is_set, "процесс запущен, spawn удержан")
    await _wait_for(spawn_log.pipe_full, "труба заполнена, читатель остановлен")
    task.cancel()
    spawn_log.release.set()

    with pytest.raises(asyncio.CancelledError):
        await _within(task, CEILING)
    await _wait_for(lambda: not _alive(spawn_log.pid), "смерть дочернего процесса")


async def test_a_second_cancel_does_not_skip_the_group_reap(flooding_child, spawn_log):
    """Повторная отмена во время очистки: kill группы всё равно выполняется."""
    spawn_log.hold = True
    task = asyncio.ensure_future(
        dispatch_mod.DispatchIntegration().submit_task("m", task_id=1)
    )
    await _wait_for(spawn_log.attached.is_set, "процесс запущен, spawn удержан")
    await _wait_for(spawn_log.pipe_full, "труба заполнена, читатель остановлен")
    task.cancel()
    # Вторая отмена должна прийти, пока очистка (_finish) ждёт удержанный spawn.
    await _wait_for(spawn_log.reaping.is_set, "очистка началась")
    assert not task.done(), "очистка должна ещё ждать удержанный spawn"
    task.cancel()
    spawn_log.release.set()

    with pytest.raises(asyncio.CancelledError):
        await _within(task, CEILING)
    await _wait_for(lambda: not _alive(spawn_log.pid), "смерть дочернего процесса")


class ContractProc:
    """Двойник процесса с контрактом CPython: wait() ждёт закрытия transport.

    Читатель трубы остановлен, EOF не придёт, поэтому wait() возвращается лишь
    после ``transport.close()``. Расписание и реальные трубы не участвуют:
    голые ``kill(); await wait()`` зависают по построению.
    """

    pid = 4242424

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False
        self.closed = asyncio.Event()
        self._transport = self

    def close(self) -> None:
        self.closed.set()

    def kill(self) -> None:
        self.killed = True

    async def communicate(self):
        await asyncio.Event().wait()

    async def wait(self) -> int:
        await self.closed.wait()
        self.returncode = -9
        return self.returncode


async def test_the_cleanup_contract_closes_the_transport_before_waiting(monkeypatch):
    """Очистка таймаута submit и _reap_spawned закрывает transport до ожидания.

    Зависание ловится ограниченным сроком, а не расписанием (#2087).
    """
    from hub import process_kill

    killed_groups: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_kill.os, "killpg", lambda g, s: killed_groups.append((g, s))
    )
    monkeypatch.setattr(dispatch_mod, "SUBMIT_TIMEOUT", 0.05)
    procs: list[ContractProc] = []

    async def fake_exec(*args, **kwargs):
        procs.append(ContractProc())
        return procs[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    pending: list[asyncio.Future] = []

    async def bounded(coro, what: str):
        task = asyncio.ensure_future(coro)
        pending.append(task)
        done, _ = await asyncio.wait({task}, timeout=2.0)
        assert done, (
            f"{what}: очистка не вернулась: wait() ждёт закрытия transport, "
            "его никто не закрыл (голые kill+wait)"
        )
        return task

    try:
        # Путь 1: таймаут communicate() в submit_task.
        task = await bounded(
            dispatch_mod.DispatchIntegration().submit_task("m", task_id=1),
            "таймаут submit",
        )
        assert isinstance(task.exception(), TimeoutError)
        assert procs[0].killed and procs[0].closed.is_set()
        assert killed_groups == [(ContractProc.pid, signal.SIGKILL)]

        # Путь 2: _reap_spawned после отмены во время spawn.
        spawn = asyncio.ensure_future(fake_exec())
        task = await bounded(dispatch_mod._reap_spawned(spawn), "_reap_spawned")
        assert task.exception() is None
        assert procs[1].killed and procs[1].closed.is_set()
        assert killed_groups[1:] == [(ContractProc.pid, signal.SIGKILL)]
    finally:
        for proc in procs:
            proc.close()
        for t in pending:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(t, timeout=WAIT)
