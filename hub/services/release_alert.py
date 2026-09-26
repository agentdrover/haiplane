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


def minutes_since(stamp: str, now: datetime | None = None) -> int | None:
    """Whole minutes from ``stamp`` (UTC, events format) to now.

    None when the stamp cannot be read: «0 мин» would be a measurement of a
    time nobody knows (#1420 review, baac9fd4eb694be8).
    """
    try:
        start = datetime.strptime(stamp, _TS).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return max(0, int(((now or datetime.now(UTC)) - start).total_seconds() // 60))


def _payload_of(row: Any) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _block_of(row: Any, payload: dict[str, Any]) -> dict[str, Any]:
    since = str(payload.get("since") or row["created_at"] or "")
    return {
        "project": str(payload.get("project") or ""),
        "reason": str(payload.get("reason") or ""),
        "since": since,
        "minutes": minutes_since(since),
        "ci": payload.get("ci"),
    }


async def active_release_block(db: Any, project_id: int) -> dict[str, Any] | None:
    """The open alert of this project, or None when its release is moving."""
    rows = await fetchall(
        db,
        "SELECT kind, payload, created_at FROM events "
        "WHERE kind IN (?, ?) AND project_id=? ORDER BY id DESC LIMIT 1",
        (KIND_BLOCKED, KIND_UNBLOCKED, project_id),
    )
    if not rows or rows[0]["kind"] != KIND_BLOCKED:
        return None
    return _block_of(rows[0], _payload_of(rows[0]))


# One query for the whole hub, answered from idx_events_kind_project: the
# newest release event of each project, kept only if it opened a block. It is
# read by every general hub_my_context, so it must not scan the event feed
# (#1420 review, ca52883c1a7ac3a7).
OPEN_BLOCKS_SQL = (
    "SELECT e.kind, e.payload, e.created_at, p.slug FROM events e "
    "LEFT JOIN projects p ON p.id = e.project_id "
    "WHERE e.id IN (SELECT MAX(id) FROM events WHERE kind IN (?, ?) "
    "GROUP BY project_id) AND e.kind = ? ORDER BY e.id"
)


def open_blocks_args() -> tuple[str, str, str]:
    return (KIND_BLOCKED, KIND_UNBLOCKED, KIND_BLOCKED)


async def active_release_blocks(db: Any) -> list[dict[str, Any]]:
    """Every open release alert on the hub — what the steward must see."""
    blocks = []
    for row in await fetchall(db, OPEN_BLOCKS_SQL, open_blocks_args()):
        block = _block_of(row, _payload_of(row))
        block["project"] = block["project"] or str(row["slug"] or "")
        blocks.append(block)
    return blocks


def ci_evidence_text(ci: Any) -> str:
    """The CI run and failed checks of a red-CI alert — or that there were none.

    Empty for an alert that is not about CI (a conflict has no run). For a red
    CI the forge's silence is said out loud: a line without the run reads as
    "the hub did not look" (#1420 review, 7c1d20f701b32445).
    """
    if not isinstance(ci, dict):
        return ""
    url = str(ci.get("run_url") or "")
    checks = [str(c) for c in ci.get("failed_checks") or [] if c]
    return "; ".join(
        (
            f"прогон: {url}" if url else "прогон CI форж не назвал",
            f"упали: {', '.join(checks)}"
            if checks
            else "упавшие проверки форж не назвал",
        )
    )


def _held(minutes: Any) -> str:
    return "время не прочитано" if minutes is None else f"{minutes} мин"


def release_block_lines(blocks: list[dict[str, Any]]) -> list[str]:
    """One line per open alert, shared by every reader that prints them."""
    lines = []
    for b in blocks:
        evidence = ci_evidence_text(b.get("ci"))
        lines.append(
            f"Релиз заблокирован с {b.get('since') or '?'} UTC "
            f"({b.get('project') or '?'}, {_held(b.get('minutes'))}): "
            f"{b.get('reason', '')}" + (f" · {evidence}" if evidence else "")
        )
    return lines


async def release_ci_evidence(project_row: Any, reason: str) -> dict[str, Any] | None:
    """Run URL and failed checks for a red-CI alert; None if CI is not the cause.

    The same forge call the task-CI fix path reads (``get_ci_failure_logs``).
    Asked once per alert, never per cycle. A forge that answers nothing, or
    fails, leaves empty fields — which ``ci_evidence_text`` names.
    """
    from hub.integrations.registry import plugins
    from hub.services.project_policy import base_branch_of, forge_of, release_base_of

    marker = f"ci_{CIProbeOutcome.failed.value} ("
    part = next((p for p in _PART_SPLIT.split(reason or "") if marker in p), "")
    if not part:
        return None
    evidence: dict[str, Any] = {"run_url": "", "failed_checks": []}
    number = re.search(r"PR #(\d+)", part)
    if number is None:
        return evidence
    project = dict(project_row)
    # The release PR runs on the integration branch; the return PR on the base.
    returning = part.lstrip().startswith("возврат")
    branch = release_base_of(project_row) if returning else base_branch_of(project_row)
    try:
        details = await plugins.git_ops.get_ci_failure_logs(
            int(number.group(1)),
            branch,
            2000,
            repo=(project.get("workspace_path") or "").strip() or None,
            gh_repo=(project.get("repo") or "").strip() or None,
            forge=forge_of(project_row),
        )
    except Exception:  # noqa: BLE001 - evidence is optional, the alert is not
        return evidence
    evidence["run_url"] = str((details or {}).get("run_url") or "")
    evidence["failed_checks"] = [
        str(c) for c in (details or {}).get("failed_checks") or [] if c
    ]
    return evidence


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
    return [
        {
            "kind": row["kind"],
            "at": row["created_at"],
            "reason": str(payload.get("reason") or ""),
            "minutes": payload.get("minutes"),
            "ci": ci_evidence_text(payload.get("ci")),
        }
        for row in rows
        for payload in (_payload_of(row),)
    ]
