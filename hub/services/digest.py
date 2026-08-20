"""Autopilot daily digest and sampling audit (#739, epic #736).

The autopilot (#744/#745) removed pre-approval clicks; this module keeps
the OVERSIGHT: every project whose gate_policy delegates anything gets one
digest per UTC day of autopilot activity — what the policy approved and on
which grounds, what it escalated, what the pipeline delivered — plus a
deterministic ~10% sample of auto-approved tasks marked for a human spot
check. A day with no autopilot transitions produces no digest: an empty
report read daily becomes noise, and noise is how oversight dies quietly.

Delivery is двойная: a ``digest_created`` event in the feed (so
hub_wait_events and the Stop hook can bring it into chat) and the /digests
page in the web panel. The spot-check results flow back into the
human_gates metric (#737) as the ``audit`` gate — the post-hoc signal the
expand-or-roll-back decision is supposed to read.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

from hub import repository as repo

log = logging.getLogger(__name__)

_AUTOPILOT_EVENT_KINDS = (
    "task_approved",
    "review_verdict_recorded",
    "verdict_escalated",
)


def deterministic_sample(task_ids: list[int], digest_date: str) -> list[int]:
    """~10% of ``task_ids``, at least one, stable for (ids, date).

    Hash-based rather than random on purpose: the same day recomputed must
    name the same tasks, or the audit trail cannot be reasoned about.
    """
    if not task_ids:
        return []
    picked = [
        tid
        for tid in sorted(set(task_ids))
        if int(hashlib.sha256(f"{tid}:{digest_date}".encode()).hexdigest(), 16) % 10
        == 0
    ]
    if not picked:
        # Minimum one: an audit sample of zero is no audit at all. The
        # choice stays deterministic — lowest hash wins.
        picked = [
            min(
                set(task_ids),
                key=lambda tid: hashlib.sha256(
                    f"{tid}:{digest_date}".encode()
                ).hexdigest(),
            )
        ]
    return picked


def _policy_delegates(gate_policy_raw: str | None) -> bool:
    try:
        policy = json.loads(gate_policy_raw or "{}")
    except ValueError:
        return False
    return isinstance(policy, dict) and "auto" in policy.values()


async def generate_due_digests(
    db: aiosqlite.Connection, *, now: datetime | None = None
) -> int:
    """Create digests for YESTERDAY (UTC) where autopilot activity exists.

    Idempotent: the UNIQUE(project_id, digest_date) key plus the insert
    guard make repeated poller passes harmless. Returns how many digests
    were created this pass.
    """
    moment = now or datetime.now(UTC)
    day = (moment - timedelta(days=1)).strftime("%Y-%m-%d")
    day_start = f"{day} 00:00:00"
    day_end = (moment - timedelta(days=1) + timedelta(days=1)).strftime(
        "%Y-%m-%d 00:00:00"
    )

    created = 0
    projects = await db.execute_fetchall(
        "SELECT id, slug, gate_policy FROM projects "
        "WHERE archived=0 AND status='active'"
    )
    for project in projects:
        if not _policy_delegates(project["gate_policy"]):
            continue
        existing = await db.execute_fetchall(
            "SELECT id FROM autopilot_digests WHERE project_id=? AND digest_date=?",
            (project["id"], day),
        )
        if existing:
            continue

        events = await db.execute_fetchall(
            "SELECT id, kind, actor, task_id, payload, created_at FROM events "
            "WHERE kind IN (?, ?, ?) AND created_at >= ? AND created_at < ? "
            "ORDER BY id ASC",
            (*_AUTOPILOT_EVENT_KINDS, day_start, day_end),
        )
        approvals: list[dict] = []
        verdicts: list[dict] = []
        escalations: list[dict] = []
        for event in events:
            if event["task_id"] is None:
                continue
            # Attribution walks the hierarchy — the same resolver as the
            # git conveyor and the human_gates metric (#747).
            owner = await repo.resolve_project_for_task(db, event["task_id"])
            if owner is None or owner["id"] != project["id"]:
                continue
            try:
                payload = json.loads(event["payload"] or "{}")
            except ValueError:
                payload = {}
            entry = {
                "task_id": event["task_id"],
                "at": event["created_at"],
                "payload": payload,
            }
            if event["kind"] == "task_approved" and event["actor"] == "policy":
                approvals.append(entry)
            elif (
                event["kind"] == "review_verdict_recorded"
                and event["actor"] == "policy"
            ):
                verdicts.append(entry)
            elif event["kind"] == "verdict_escalated":
                escalations.append(entry)

        if not (approvals or verdicts or escalations):
            continue

        merges = await db.execute_fetchall(
            "SELECT pr_number, task_id, merge_sha, merged_at FROM pipeline_merges "
            "WHERE project_id=? AND merged_at >= ? AND merged_at < ?",
            (project["id"], day_start, day_end),
        )
        sample = deterministic_sample(
            [a["task_id"] for a in approvals] + [v["task_id"] for v in verdicts],
            day,
        )
        payload = {
            "date": day,
            "project": project["slug"],
            "auto_approvals": approvals,
            "auto_verdicts": verdicts,
            "escalations": escalations,
            "deliveries": [dict(m) for m in merges],
            "audit_sample": sample,
            "audit_results": {},
        }
        digest_id = await repo.create_digest(
            db,
            project_id=project["id"],
            digest_date=day,
            payload=json.dumps(payload, ensure_ascii=False),
        )
        if digest_id is None:
            continue
        await repo.insert_event(
            db,
            kind="digest_created",
            project_id=project["id"],
            actor="hub",
            payload={
                "digest_id": digest_id,
                "date": day,
                "auto_approvals": len(approvals),
                "auto_verdicts": len(verdicts),
                "escalations": len(escalations),
                "audit_sample": sample,
            },
        )
        await db.commit()
        created += 1
        log.info(
            "digest #%s created for project %s (%s): %d approvals, "
            "%d verdicts, %d escalations, sample %s",
            digest_id,
            project["slug"],
            day,
            len(approvals),
            len(verdicts),
            len(escalations),
            sample,
        )
    return created


async def record_audit_result(
    db: aiosqlite.Connection,
    digest_id: int,
    task_id: int,
    result: str,
    comment: str = "",
) -> dict:
    """Store a spot-check outcome for a sampled task (human gate, #739).

    Writes three places at once: the digest payload (the page shows the
    checkbox state), the task feed (the task carries its own audit trail)
    and the events feed (the human_gates metric reads the ``audit`` gate
    from there). Raises ValueError on an unknown digest or a task outside
    its sample — auditing a task nobody sampled would fabricate coverage.
    """
    row = await repo.get_digest(db, digest_id)
    if row is None:
        raise ValueError(f"digest #{digest_id} not found")
    payload = json.loads(row["payload"] or "{}")
    sample = payload.get("audit_sample") or []
    if task_id not in sample:
        raise ValueError(
            f"task #{task_id} is not in the audit sample of digest #{digest_id}"
        )
    payload.setdefault("audit_results", {})[str(task_id)] = result
    await repo.update_digest_payload(
        db, digest_id, json.dumps(payload, ensure_ascii=False)
    )
    verdict_text = "ок" if result == "ok" else "найдена проблема"
    await repo.add_task_update(
        db,
        task_id,
        "human",
        "decision",
        (
            f"Выборочный аудит автопилота (дайджест #{digest_id}, "
            f"{payload.get('date')}): {verdict_text}."
            + (f" {comment}" if comment else "")
        ),
    )
    await repo.insert_event(
        db,
        kind="audit_result",
        task_id=task_id,
        project_id=row["project_id"],
        actor="human",
        payload={"digest_id": digest_id, "result": result},
    )
    await db.commit()
    return payload
