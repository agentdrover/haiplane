"""#1332: CancelledError must kill the host-profile child, not leave it running."""

from __future__ import annotations

import asyncio
import sys

import pytest

from hub.services import validation_run


async def test_cancelled_profile_exec_kills_the_child(tmp_path) -> None:
    pidfile = tmp_path / "child.pid"
    marker = tmp_path / "still_alive"
    script = (
        "import os, pathlib, time\n"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(2.0)\n"
        f"pathlib.Path({str(marker)!r}).write_text('x')\n"
    )
    running = asyncio.create_task(
        validation_run._profile_exec([sys.executable, "-c", script], str(tmp_path))
    )
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip().isdigit():
            break
        if running.done():
            await running
            pytest.fail("host-profile child exited before it could be cancelled")
        await asyncio.sleep(0.05)
    else:
        running.cancel()
        pytest.fail("host-profile child never started")

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    await asyncio.sleep(2.5)
    assert not marker.exists(), (
        "CancelledError left the host-profile child running"
    )
