"""SQL of the CI integrity check (#1688).

Beside ``hub.repository`` and not in it: that file is at its size ceiling
(docs/agent-context/file-size-budget.json), and a new topic is the case the
budget asks to split out rather than to grow the waived file.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from hub.db import fetchall


async def get_ci_integrity_scan(
    db: aiosqlite.Connection, task_id: int, head_sha: str
) -> dict[str, Any] | None:
    """The stored scan of one commit, or None when it was never scanned (#1688)."""
    rows = await fetchall(
        db,
        "SELECT state, signs, reason FROM ci_integrity_scans "
        "WHERE task_id=? AND head_sha=?",
        (task_id, head_sha),
    )
    return dict(rows[0]) if rows else None


async def save_ci_integrity_scan(
    db: aiosqlite.Connection,
    task_id: int,
    head_sha: str,
    state: str,
    signs_json: str,
    reason: str,
) -> None:
    await db.execute(
        "INSERT INTO ci_integrity_scans (task_id, head_sha, state, signs, reason) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(task_id, head_sha) DO UPDATE SET state=excluded.state, "
        "signs=excluded.signs, reason=excluded.reason, "
        "scanned_at=datetime('now')",
        (task_id, head_sha, state, signs_json, reason),
    )


async def get_ci_integrity_answer(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> dict[str, Any] | None:
    """The newest reviewer answer to the integrity question for a generation."""
    rows = await fetchall(
        db,
        "SELECT ci_integrity_verdict, ci_integrity_explanation FROM machine_reviews "
        "WHERE task_id=? AND submission_generation=? AND ci_integrity_verdict != '' "
        "ORDER BY id DESC LIMIT 1",
        (task_id, generation),
    )
    return dict(rows[0]) if rows else None
