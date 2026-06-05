"""Vast.ai instance management from Hub.

Provides active shutdown (vast-job down), status queries, and DB checks
for active Vast tasks.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiosqlite

from hub.config import VAST_JOB_BIN

log = logging.getLogger(__name__)

ACTIVE_STATUSES = ("running", "review", "fix_requested", "ci_check")


async def _run_vast_job(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            VAST_JOB_BIN,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        rc = proc.returncode or 0
        return (
            rc,
            stdout.decode(errors="replace").strip(),
            stderr.decode(errors="replace").strip(),
        )
    except FileNotFoundError:
        log.warning("vast-job binary not found at %s", VAST_JOB_BIN)
        return 1, "", f"binary not found: {VAST_JOB_BIN}"
    except asyncio.TimeoutError:
        log.warning(
            "vast-job %s timed out after %ds", args[0] if args else "?", timeout
        )
        return 1, "", "timeout"


class VastIntegration:
    """Concrete Vast.ai plugin backed by the vast-openclaw CLI."""

    async def vast_up(self) -> dict[str, Any]:
        rc, out, err = await _run_vast_job("up", timeout=1200)
        if rc != 0:
            log.warning("vast_up failed (rc=%d): %s", rc, err)
        try:
            return json.loads(out) if out else {"error": err or "no output"}
        except json.JSONDecodeError:
            return {"error": err, "raw": out}

    async def vast_down(self) -> dict[str, Any]:
        rc, out, err = await _run_vast_job("down", timeout=60)
        if rc == 0:
            log.info("vast_down: instance destroyed")
        else:
            log.warning("vast_down failed (rc=%d): %s", rc, err)
        try:
            return json.loads(out) if out else {"destroyed": rc == 0, "error": err}
        except json.JSONDecodeError:
            return {"destroyed": rc == 0, "raw": out, "error": err}

    async def vast_status(self) -> dict[str, Any]:
        rc, out, err = await _run_vast_job("status", timeout=15)
        try:
            return json.loads(out) if out else {"status": "unknown", "error": err}
        except json.JSONDecodeError:
            return {"status": "unknown", "raw": out}

    async def has_active_vast_tasks(self, db: aiosqlite.Connection) -> bool:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        cursor = await db.execute(
            f"SELECT count(*) FROM tasks WHERE archived=0 "  # nosec B608
            f"AND runtime IN ('vast','auto') AND status IN ({placeholders})",
            ACTIVE_STATUSES,
        )
        row = await cursor.fetchone()
        return bool(row and row[0] > 0)
