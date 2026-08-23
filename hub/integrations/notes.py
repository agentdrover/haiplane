"""notesforllm integration via n4l CLI bridge."""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from typing import Any

from hub.config import N4L_BIN, N4L_SPACE_ID

log = logging.getLogger(__name__)


async def _n4l(
    tool: str, payload: dict[str, Any] | None = None, timeout: float = 15
) -> Any:
    if payload is None:
        payload = {}
    input_json = json.dumps(payload)
    try:
        proc = await asyncio.create_subprocess_exec(
            N4L_BIN,
            tool,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError):
        log.debug("n4l binary not found at %s", N4L_BIN)
        return None
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_json.encode()),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("n4l %s timed out", tool)
        return None
    if proc.returncode != 0:
        log.warning("n4l %s failed: %s", tool, stderr.decode(errors="replace"))
        return None
    raw = stdout.decode(errors="replace").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


class NotesIntegration:
    """Concrete notes plugin backed by the n4l CLI."""

    async def availability(self) -> dict[str, str]:
        """Diagnose the notes link (#251): available | no_binary | no_space | error.

        Silent empty lists made "no decisions" indistinguishable from "the
        integration is broken"; this gives callers an explicit reason.
        """
        import shutil

        if not shutil.which(N4L_BIN) and not pathlib.Path(N4L_BIN).exists():
            return {
                "status": "no_binary",
                "detail": f"n4l binary not found at {N4L_BIN}",
            }
        if N4L_SPACE_ID:
            return {"status": "available", "detail": f"space={N4L_SPACE_ID}"}
        result = await _n4l("spaces_list")
        if result is None:
            return {
                "status": "error",
                "detail": "n4l spaces_list failed (see hub logs)",
            }
        spaces = result if isinstance(result, list) else []
        if not spaces:
            return {
                "status": "no_space",
                "detail": "HAIPLANE_N4L_SPACE (or legacy OPENCLAW_N4L_SPACE) "
                "is not set and no spaces exist",
            }
        return {
            "status": "available",
            "detail": f"default space={spaces[0].get('id', '?')}",
        }

    async def recent_decisions(
        self, space_id: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        sid = space_id or N4L_SPACE_ID
        if not sid:
            result = await _n4l("spaces_list")
            spaces = result if isinstance(result, list) else []
            if spaces:
                sid = spaces[0].get("id", "")
        if not sid:
            return []
        result = await _n4l(
            "notes_query",
            {
                "space_id": sid,
                "type": "decision",
                "limit": limit,
                "sort": "newest",
            },
        )
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "pages" in result:
            return result["pages"]
        return []

    async def save_decision(
        self,
        task_id: int,
        action: str,
        summary: str,
        context: str = "",
    ) -> dict[str, Any] | None:
        """Persist a human decision record through n4l notes_decision_save."""
        sid = N4L_SPACE_ID
        if not sid:
            result = await _n4l("spaces_list")
            spaces = result if isinstance(result, list) else []
            if spaces:
                sid = spaces[0].get("id", "")
        if not sid:
            log.debug("save_decision skipped: no space_id available")
            return None

        title = f"Task #{task_id}: decision={action}"
        payload: dict[str, Any] = {
            "space_id": sid,
            "title": title,
            "decision": f"{action}: {summary}",
            "why": context or summary,
            "task_id": f"task-{task_id}",
            "tags": ["hub-decision", f"task-{task_id}"],
        }
        result = await _n4l("notes_decision_save", payload)
        if result is None:
            log.warning("save_decision: n4l call returned None for task #%s", task_id)
        return result if isinstance(result, dict) else None
