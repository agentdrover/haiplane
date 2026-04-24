"""GitHub integration via gh CLI."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from hub.config import GH_BIN, REPO_NAME

log = logging.getLogger(__name__)


async def _gh(*args: str, timeout: float = 30) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            GH_BIN,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError):
        log.debug("gh binary not found at %s", GH_BIN)
        return ""
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("gh command timed out: %s", args)
        return ""
    if proc.returncode != 0:
        log.warning("gh %s failed: %s", args, stderr.decode(errors="replace"))
        return ""
    return stdout.decode(errors="replace")


class GitHubIntegration:
    """Concrete GitHub plugin backed by the gh CLI."""

    async def recent_commits(self, limit: int = 10) -> list[dict[str, Any]]:
        raw = await _gh(
            "api",
            f"repos/{REPO_NAME}/commits",
            "--jq",
            f".[:{limit}] | [.[] | {{sha: .sha[:7], message: .commit.message, author: .commit.author.name, date: .commit.author.date}}]",
        )
        if not raw.strip():
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []

    async def open_prs(self) -> list[dict[str, Any]]:
        raw = await _gh(
            "pr",
            "list",
            "--repo",
            REPO_NAME,
            "--state",
            "open",
            "--json",
            "number,title,headRefName,author,createdAt,url",
        )
        if not raw.strip():
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []
