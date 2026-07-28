"""Kill a spawned child and everything it spawned (#544).

``proc.kill()`` signals only the pid the hub spawned, and that pid is rarely the
process doing the work: ``create_subprocess_shell`` gives us ``/bin/sh``, and
``uv run pytest`` gives us the launcher. Whether the real payload dies with it
depends on whether that process ``exec``s — bash-as-sh on macOS does for a
simple command, dash on Ubuntu does not. So #509 shipped a kill that passed on
the author's macOS and left the command running on the Linux production host.

Spawn with ``start_new_session=True`` and signal the whole process group; the
outcome then does not depend on a shell optimisation.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
from typing import Any

log = logging.getLogger("hub")


async def kill_process_group(proc: Any) -> None:
    """SIGKILL the child's process group and reap it. Never raises.

    Falls back to a single-pid kill when the group cannot be signalled, so the
    outcome is never worse than plain ``proc.kill()``.
    """
    if proc is None or proc.returncode is not None:
        return

    pgid = None
    with contextlib.suppress(ProcessLookupError, OSError):
        pgid = os.getpgid(proc.pid)

    # Never signal our own group. Without start_new_session the child shares the
    # hub's process group, and killpg would SIGKILL the hub itself — a far worse
    # outcome than the leak this function exists to prevent.
    if pgid is not None and pgid != os.getpgid(0):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, signal.SIGKILL)
    else:
        log.warning(
            "child %s shares the hub's process group; killing the pid only",
            proc.pid,
        )

    # Always follow up on the pid itself: killpg may have been skipped or denied,
    # and signalling an already-dead process is a no-op.
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()
    with contextlib.suppress(ProcessLookupError, OSError):
        await proc.wait()
