"""A blocked release is an alert, a slow one is routine (#1420).

On 25.09.2026 CI on develop was red from 10:40 to 12:20 and the auto-release
stood still. The hub wrote it down (activity_log #9084) — as «релиз стоит»,
the very phrase it had written five times that day for releases whose CI was
merely running longer than three poll cycles. The alarm read as routine, went
only to the feed, and was noticed by accident an hour and a half later.

This module holds the two halves the poller and the readers share:

- ``classify_release_reason`` splits the reasons ``merge_ready_release``
  returns into routine (CI still running, mergeability not computed yet), a
  probe that could not look (counted over several cycles, like a flicker),
  and an alert. A reason it does not recognise is an alert: guessing
  "routine" about an unknown state is how a real red goes quiet again.
- the alert's state lives in ``events`` (``release_blocked`` /
  ``release_unblocked``, per project), not in poller memory: it must survive
  a restart without writing the same alarm twice (#534), and the readers —
  ``prod_state`` for the steward, the digest for the owner — run outside the
  poller.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from hub import repository as repo
from hub.db import fetchall
from hub.integrations.protocols import CIProbeOutcome, MergeabilityOutcome

ROUTINE = "routine"
PROBE = "probe"
ALERT = "alert"

KIND_BLOCKED = "release_blocked"
KIND_UNBLOCKED = "release_unblocked"

_TS = "%Y-%m-%d %H:%M:%S"

# CI that is running or has not started yet, and a mergeability GitHub has not
# computed yet: the ordinary state of every release for its first minutes.
_ROUTINE_MARKERS = (
    f"ci_{CIProbeOutcome.pending.value} (",
    f"ci_{CIProbeOutcome.missing_run.value} (",
    f": {MergeabilityOutcome.unknown.value} (",
)

# The hub could not look: a forge error, an exception on the way. One such
# cycle is a network hiccup; the same one several cycles in a row is a
# release nobody can see, and then it is an alert too.
_PROBE_MARKERS = (
    f"ci_{CIProbeOutcome.unavailable.value} (",
    "не удалось провести",
    "не проведён",
    "выяснить не удалось",
    "не прочитан",
)

# merge_ready_release joins the release reason and the return-PR reason with
# "; " (#1426). Split only there: a CI reason may carry its own semicolons.
_PART_SPLIT = re.compile(r";\s+(?=возврат )")

_RANK = {ROUTINE: 0, PROBE: 1, ALERT: 2}


def _classify_part(part: str) -> str:
    if any(marker in part for marker in _ROUTINE_MARKERS):
        return ROUTINE
    if any(marker in part for marker in _PROBE_MARKERS):
        return PROBE
    return ALERT


def classify_release_reason(reason: str) -> str:
    """``routine`` | ``probe`` | ``alert`` — the worst of the reason's parts."""
    parts = [p.strip() for p in _PART_SPLIT.split(reason or "") if p.strip()]
    if not parts:
        return ROUTINE
    return max((_classify_part(p) for p in parts), key=_RANK.__getitem__)


def utc_stamp(moment: datetime | None = None) -> str:
    return (moment or datetime.now(UTC)).strftime(_TS)


def minutes_since(stamp: str, now: datetime | None = None) -> int:
    """Whole minutes from ``stamp`` (UTC, events format) to now; 0 if unreadable."""
    try:
        start = datetime.strptime(stamp, _TS).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return 0
    return max(0, int(((now or datetime.now(UTC)) - start).total_seconds() // 60))


async def active_release_block(db: Any, project_id: int) -> dict[str, Any] | None:
    """The open alert of this project, or None when its release is moving."""
    rows = await fetchall(
        db,
        "SELECT kind, payload, created_at FROM events "
        "WHERE project_id=? AND kind IN (?, ?) ORDER BY id DESC LIMIT 1",
        (project_id, KIND_BLOCKED, KIND_UNBLOCKED),
    )
    if not rows or rows[0]["kind"] != KIND_BLOCKED:
        return None
    try:
        payload = json.loads(rows[0]["payload"] or "{}")
    except ValueError:
        payload = {}
    since = str(payload.get("since") or rows[0]["created_at"] or "")
    return {
        "project": str(payload.get("project") or ""),
        "reason": str(payload.get("reason") or ""),
        "since": since,
        "minutes": minutes_since(since),
    }


async def active_release_blocks(db: Any) -> list[dict[str, Any]]:
    """Every open release alert on the hub — what the steward must see."""
    blocks = []
    for project in await repo.list_projects(db):
        row = dict(project)
        block = await active_release_block(db, int(row["id"]))
        if block is not None:
            block["project"] = block["project"] or str(row.get("slug") or "")
            blocks.append(block)
    return blocks


def release_block_lines(blocks: list[dict[str, Any]]) -> list[str]:
    """One line per open alert, shared by every reader that prints them."""
    return [
        f"Релиз заблокирован с {b.get('since', '?')} UTC "
        f"({b.get('project', '?')}, {b.get('minutes', 0)} мин): {b.get('reason', '')}"
        for b in blocks
    ]


async def release_block_history(
    db: Any, project_id: int, start: str, end: str
) -> list[dict[str, Any]]:
    """Alerts opened and cleared in [start, end) — the digest's block."""
    rows = await fetchall(
        db,
        "SELECT kind, payload, created_at FROM events "
        "WHERE project_id=? AND kind IN (?, ?) AND created_at >= ? AND created_at < ? "
        "ORDER BY id ASC",
        (project_id, KIND_BLOCKED, KIND_UNBLOCKED, start, end),
    )
    history = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            payload = {}
        history.append(
            {
                "kind": row["kind"],
                "at": row["created_at"],
                "reason": str(payload.get("reason") or ""),
                "minutes": payload.get("minutes"),
            }
        )
    return history
