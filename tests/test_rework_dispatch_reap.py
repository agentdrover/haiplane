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

import pytest

from hub import services
from hub.integrations import dispatch as dispatch_mod
from hub.integrations.registry import plugins
from hub.models import TaskDecide
from tests.test_decision_brief import _task_in
from tests.test_rework_dispatch_once import REWORK

#: Срок submit в тесте и потолок: время на закрытие transport и reap (5 с в kill).
TEST_TIMEOUT = 1.5
CEILING = 7.0

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


async def test_a_timed_out_submit_returns_on_a_full_pipe(flooding_child):
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await _within(
            dispatch_mod.DispatchIntegration().submit_task("m", task_id=1),
            TEST_TIMEOUT + CEILING,
        )
    assert time.monotonic() - started < TEST_TIMEOUT + CEILING
    await asyncio.sleep(0.2)
    assert not _alive(int(flooding_child.read_text())), "дочерний процесс жив"


async def test_a_full_pipe_timeout_frees_the_booking(db, monkeypatch, flooding_child):
    from hub.services import rework_launch

    monkeypatch.setattr(plugins, "dispatch", dispatch_mod.DispatchIntegration())
    task_id = await _task_in(db, "needs_decision", submission_generation=1)

    with pytest.raises(TimeoutError):
        await _within(
            services.decide_task(db, task_id, TaskDecide(**REWORK)),
            TEST_TIMEOUT + CEILING,
        )

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
