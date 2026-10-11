"""Tests for killing a child and its descendants (#544)."""

from __future__ import annotations

import asyncio
import os

from hub import process_kill
from hub.process_kill import kill_process_group


async def test_kills_grandchild_the_shell_did_not_exec(tmp_path):
    # `sh -c "<payload>; :"` cannot be exec-optimised away, so the payload runs
    # as a grandchild. Killing only the shell's pid — the pre-#544 behaviour —
    # leaves it running; the group kill must reach it.
    marker = tmp_path / "still_alive"
    payload = (
        f'python3 -c "import time,pathlib;'
        f"time.sleep(1.5);pathlib.Path(r'{marker}').write_text('x')\""
    )
    proc = await asyncio.create_subprocess_shell(
        f"{payload}; :",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    await kill_process_group(proc)
    await asyncio.sleep(2.0)
    assert not marker.exists(), "grandchild survived the group kill"


async def test_never_signals_the_hubs_own_process_group(monkeypatch):
    # Without start_new_session the child shares OUR process group. Signalling
    # it would SIGKILL the hub itself — strictly worse than the leak this
    # function prevents. The guard must skip the group and kill the pid only.
    aimed_at: list[int] = []
    monkeypatch.setattr(
        process_kill.os, "killpg", lambda pgid, sig: aimed_at.append(pgid)
    )
    proc = await asyncio.create_subprocess_shell(
        "sleep 5",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert os.getpgid(proc.pid) == os.getpgid(0), "test premise: shared group"

    await kill_process_group(proc)

    assert aimed_at == [], "killpg was aimed at the hub's own process group"
    assert proc.returncode is not None, "child was not killed and reaped"


async def test_already_finished_child_is_a_noop():
    proc = await asyncio.create_subprocess_shell(
        ":", stdout=asyncio.subprocess.DEVNULL, start_new_session=True
    )
    await proc.wait()
    await kill_process_group(proc)  # must not raise


async def test_run_kills_process_group_on_cancel(tmp_path):
    # AC-4 (#1603): отмена корутины не оставляет дочерние процессы, а
    # CancelledError идёт дальше. Реальные процессы, без моков.
    from hub.integrations import proc

    pidfile = tmp_path / "child.pid"
    task = asyncio.create_task(
        proc.run("sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait", timeout=60)
    )
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.05)
    pid = int(pidfile.read_text().strip())
    try:
        task.cancel()
        raised = False
        try:
            await task
        except asyncio.CancelledError:
            raised = True
        assert raised, "CancelledError was swallowed"
        alive = True
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
                break
            await asyncio.sleep(0.05)
        assert not alive, "the child survived the cancellation"
    finally:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
