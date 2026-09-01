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

Since #1143 the steward (#994) is covered by the same digest rather than a
second one. Its decisions arrive with their GROUNDS, because a verdict on
its own gives a reader nothing to check, and "delegating" now means the
policy autopilot OR the steward: a project that hands only the verdict to
the steward used to fall outside the check and get no digest at all, which
is indistinguishable from a quiet day.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

from hub.db import fetchall
from hub import repository as repo
from hub.services.gate_events import STEWARD_JUDGEMENT

log = logging.getLogger(__name__)

_AUTOPILOT_EVENT_KINDS = (
    "task_approved",
    "review_verdict_recorded",
    "verdict_escalated",
    # #1143: the steward writes its own kind (#1023). Reading it here rather
    # than in a second query keeps ONE project-attribution walk: two walks
    # would eventually disagree about which project a task belongs to, and
    # the one that disagreed silently would be the oversight half.
    STEWARD_JUDGEMENT,
)

# Which gate_policy values mean "somebody other than a person decides here".
# "auto" was the only one while the policy autopilot was the only delegate;
# a project that hands the verdict to the steward has delegated exactly as
# much, and asking for "auto" specifically refused it a digest entirely
# (#1143). The set is the place to add the next delegate — the check reads
# it, so a new word cannot be added to the policy and forgotten here.
_DELEGATED_TO_MACHINE: frozenset[str] = frozenset({"auto", "steward"})


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
    """Does this project let a machine decide anything at all?

    True for the policy autopilot ("auto") and for the steward ("steward").
    The digest is the oversight of delegated decisions, so the question is
    "is anything delegated", not "is it delegated to the autopilot" — the
    narrower reading left a steward-only project with no digest at all, and
    a missing digest looks exactly like a quiet day (#1143).
    """
    try:
        policy = json.loads(gate_policy_raw or "{}")
    except ValueError:
        return False
    if not isinstance(policy, dict):
        return False
    return any(
        isinstance(value, str) and value in _DELEGATED_TO_MACHINE
        for value in policy.values()
    )


# The window the category debt is read over. Wider than a digest's own day on
# purpose: a class that recurs across three tasks does not do so within
# twenty-four hours, and a one-day window would report an empty debt every
# morning (#878).
_DEBT_WINDOW = "-90 days"


async def _steward_entry(db: aiosqlite.Connection, entry: dict, payload: dict) -> dict:
    """One steward decision the way the digest must show it: WITH its grounds.

    A verdict on its own is not something a person can check — «одобрено»
    tells the reader what happened and nothing about whether it should have.
    The grounds are the whole reason a shadow decision is worth reading, so
    they are fetched here rather than left to whoever renders the section.

    Absence is spelled out. Grounds are stored as a JSON string and an empty
    list serialises to "[]", which a template reads as truthy; a judgement
    that attached nothing would then look exactly like one that attached
    everything (#762). ``grounds_state`` says which of the two it is.
    """
    judged = await repo.get_steward_judgement(
        db,
        entry["task_id"],
        int(payload.get("generation") or 0),
        str(payload.get("kind") or ""),
    )
    grounds: list = []
    if judged is not None:
        try:
            parsed = json.loads(dict(judged).get("grounds") or "[]")
        except (TypeError, ValueError):
            parsed = []
        grounds = parsed if isinstance(parsed, list) else []
    return {
        **entry,
        "verdict": str(payload.get("verdict") or ""),
        "judgement_kind": str(payload.get("kind") or ""),
        "generation": int(payload.get("generation") or 0),
        "grounds": grounds,
        "grounds_state": "present" if grounds else "absent",
    }


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
    projects = await fetchall(
        db,
        "SELECT id, slug, gate_policy FROM projects "
        "WHERE archived=0 AND status='active'",
    )
    for project in projects:
        if not _policy_delegates(project["gate_policy"]):
            continue
        existing = await fetchall(
            db,
            "SELECT id FROM autopilot_digests WHERE project_id=? AND digest_date=?",
            (project["id"], day),
        )
        if existing:
            continue

        # One placeholder per entry of _AUTOPILOT_EVENT_KINDS. Written out
        # rather than generated: building SQL by concatenation is the shape
        # every injection review has to stop and read, and here it buys
        # nothing — a miscount raises on the first execute, so every digest
        # test in this suite fails loudly rather than the poller failing at
        # midnight.
        events = await fetchall(
            db,
            "SELECT id, kind, actor, task_id, payload, created_at FROM events "
            "WHERE kind IN (?, ?, ?, ?) AND created_at >= ? AND created_at < ? "
            "ORDER BY id ASC",
            (*_AUTOPILOT_EVENT_KINDS, day_start, day_end),
        )
        approvals: list[dict] = []
        verdicts: list[dict] = []
        escalations: list[dict] = []
        steward: list[dict] = []
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
                # Model diversity (#758): the digest shows WHO wrote and WHO
                # reviewed — the pair the monoculture rule compares.
                task_row = await repo.get_task(db, event["task_id"])
                mr = await repo.get_latest_machine_review(db, event["task_id"])
                entry["models"] = {
                    "implementer": (
                        dict(task_row).get("submission_model", "") if task_row else ""
                    ),
                    "reviewer": (dict(mr).get("model", "") if mr else ""),
                }
                verdicts.append(entry)
            elif event["kind"] == "verdict_escalated":
                escalations.append(entry)
            elif event["kind"] == STEWARD_JUDGEMENT and event["actor"] == "steward":
                steward.append(await _steward_entry(db, entry, payload))

        if not (approvals or verdicts or escalations or steward):
            # The empty-day rule now covers the steward too, in both
            # directions: a day of steward-only activity IS a day worth a
            # digest, and a day with neither still produces nothing. An
            # empty report read daily stops being read within a week, and
            # that is how oversight dies quietly (#739).
            continue

        merges = await fetchall(
            db,
            "SELECT pr_number, task_id, merge_sha, merged_at FROM pipeline_merges "
            "WHERE project_id=? AND merged_at >= ? AND merged_at < ?",
            (project["id"], day_start, day_end),
        )
        sample = deterministic_sample(
            [a["task_id"] for a in approvals] + [v["task_id"] for v in verdicts],
            day,
        )
        # #878: the debt rides along with a digest that is being created for
        # other reasons. It does NOT cause one: this digest is per-project and
        # only exists on days with autopilot activity, while the debt is a
        # property of the practice. Making it a trigger would have it arrive
        # on some days and not others with no way to tell which.
        from hub.services.orchestration import (
            build_category_debt,
            recurring_categories,
        )

        debt = [
            d
            for d in await build_category_debt(
                db, await recurring_categories(db, _DEBT_WINDOW)
            )
            if not d["covered"]
        ]
        # #1020: the human queue rides along the same way the category debt
        # does, and for the same reason — it is a property of the practice,
        # not of this day's autopilot activity. Riding along has a real cost
        # to state plainly: a digest is only created for a delegating project
        # on a day with autopilot events, so this line appears when a digest
        # happens to exist. The reminder's dependable channel is the events
        # feed the poller writes; the digest is the summary, not the alarm.
        human_queue = await repo.human_queue_reminders_between(db, day_start, day_end)
        payload = {
            "date": day,
            "project": project["slug"],
            "category_debt": debt,
            "human_queue": human_queue,
            "auto_approvals": approvals,
            "auto_verdicts": verdicts,
            "escalations": escalations,
            "steward_judgements": steward,
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
                "steward_judgements": len(steward),
                "audit_sample": sample,
            },
        )
        await db.commit()
        created += 1
        log.info(
            "digest #%s created for project %s (%s): %d approvals, "
            "%d verdicts, %d escalations, %d steward judgements, sample %s",
            digest_id,
            project["slug"],
            day,
            len(approvals),
            len(verdicts),
            len(escalations),
            len(steward),
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
