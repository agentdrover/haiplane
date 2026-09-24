"""The review queue in one call (#1334).

22–23.09.2026 the steward built its table of "what to approve" by calling the
review brief once per task in review and needs_decision — 20+ briefs at 8–60 s
each — and pulling five fields out of each with jq. Two passes timed out. The
brief is expensive because it computes everything (the diff, call sites, the
evidence package); the five fields need none of that.

One rule, not two. Every field here is produced by the SAME function the
brief uses for it:

- ``sha_check`` — ``review_evidence.sha_check_of``, fed the tip the hub last
  observed (``lifecycle.observed_branch_tip``) instead of a fetch. No fresh
  observation is ``unknown`` with its reason, never ``match``;
- the report — ``review_evidence.report_view``, which is ``review_report``
  minus its diff read;
- the flight — ``review_evidence.inflight_view``;
- "does this generation have a review" — ``review_availability.generation_review``;
- the verdict — ``lifecycle.latest_review_projection``.

The stall reason is the one fact the brief does not carry: the newest gate
line since the task entered its status, taken from the structured
``needs_decision`` event when there is one and from the gate's own alert
otherwise.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from hub import repository as repo
from hub.db import fetchall
from hub.models import ReviewQueueRow, ReviewQueueView
from hub.services import review_evidence
from hub.services.lifecycle import (
    latest_review_projection,
    observed_branch_tip,
    observed_tip_age_minutes,
)
from hub.services.review_availability import (
    REFUSAL_ALERT_PREFIXES,
    generation_review,
)

QUEUE_STATUSES = ("review", "needs_decision")

#: Starts of the lines the delivery gate and the review dispatcher write when
#: a submission stops moving. Each is written by ``poller._deliver_pair_task``,
#: ``orchestration._deliver_completed_pair_task`` or
#: ``review_dispatch.maybe_dispatch_review``; the test drives the merge_failed
#: one through the same shape the gate writes.
STALL_LINE_PREFIXES: tuple[str, ...] = (
    "Доставка отложена",
    "Доставка не состоялась",
    "Done report NOT completed",
    "Ревью одобрено, но PR",
    *REFUSAL_ALERT_PREFIXES,
)

#: Order of the groups: what can be approved now, what would be but the
#: branch tip is not verified, what needs its findings answered, what waits
#: for a report, and what no verdict can move (a diverged branch needs a
#: resubmission, needs_decision needs a decision).
#:
#: ``ready_sha_unverified`` is its own group, not ``ready`` and not
#: ``blocked`` (finding 522c9ff79cdad14e): a clean report over a branch
#: nobody observed recently is not ready to approve, but after a restart every
#: tip is unobserved, and ``blocked`` would swallow the whole queue.
READY = "ready"
READY_SHA_UNVERIFIED = "ready_sha_unverified"
READINESS_ORDER = (
    READY,
    READY_SHA_UNVERIFIED,
    "findings",
    "awaiting_report",
    "blocked",
)

QUEUE_LIMIT = 200


def _parse_at(raw: str) -> datetime | None:
    text = (raw or "").strip()[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


async def last_stall(db, task_id: int, since: str) -> tuple[str, str]:
    """The newest gate stall since ``since``: ``(reason, at)``, empty when none."""
    events = await fetchall(
        db,
        "SELECT payload, created_at FROM events WHERE task_id=? "
        "AND kind='needs_decision' AND created_at >= ? ORDER BY id DESC LIMIT 1",
        (task_id, since),
    )
    updates = [
        dict(u)
        for u in await fetchall(
            db,
            "SELECT content, created_at FROM task_updates WHERE task_id=? "
            "AND agent='hub' AND created_at >= ? ORDER BY id DESC LIMIT 50",
            (task_id, since),
        )
        if str(dict(u)["content"]).startswith(STALL_LINE_PREFIXES)
    ]
    line = updates[0] if updates else None
    if events:
        event = dict(events[0])
        at = str(event["created_at"])
        if line is None or at >= str(line["created_at"]):
            try:
                payload = json.loads(event["payload"] or "{}")
            except ValueError:
                payload = {}
            reason = str(payload.get("detail") or payload.get("reason") or "")
            if reason:
                return reason, at
    if line is not None:
        return str(line["content"]), str(line["created_at"])
    return "", ""


def report_status(report_state: str, machine_review: Any, in_flight: Any) -> str:
    """none | in_flight | current | incomplete — the report of THIS generation."""
    if report_state == "current":
        return "incomplete" if machine_review.incomplete else "current"
    return "in_flight" if in_flight is not None else "none"


def readiness(row: ReviewQueueRow) -> str:
    if row.status != "review" or row.sha_check == "diverged":
        return "blocked"
    if row.report_status != "current":
        return "awaiting_report"
    if row.findings_confirmed or row.findings_unresolved:
        return "findings"
    if not row.generation_has_review:
        return "awaiting_report"
    return READY if row.sha_check == "match" else READY_SHA_UNVERIFIED


async def queue_row(db, task_row: dict[str, Any]) -> ReviewQueueRow:
    task_id = int(task_row["id"])
    branch = str(task_row.get("branch") or "")
    tip, tip_reason = observed_branch_tip(task_id, branch)
    sha_check, sha_reason = review_evidence.sha_check_of(
        str(task_row.get("submission_sha") or ""), branch, tip, tip_reason
    )
    tip_age = observed_tip_age_minutes(task_id, branch) if tip else None
    if tip_age is not None:
        sha_reason = f"наблюдение вершины {tip_age} мин назад. {sha_reason}"
    mr_row = await repo.get_latest_machine_review(db, task_id)
    report = await review_evidence.report_view(db, task_row, mr_row)
    mr = report.machine_review
    current = mr if mr is not None and mr.is_current else None
    flight = await review_evidence.inflight_view(db, task_row)
    gen_review = await generation_review(db, task_row)
    latest = latest_review_projection(task_row)
    since = str(task_row.get("status_entered_at") or "")
    stall, stall_at = await last_stall(db, task_id, since)
    entered = _parse_at(since)
    row = ReviewQueueRow(
        task_id=task_id,
        title=str(task_row.get("title") or ""),
        status=str(task_row.get("status") or ""),
        submission_generation=int(task_row.get("submission_generation") or 0),
        submission_sha=report.submission_sha,
        sha_check=sha_check,
        sha_check_reason=sha_reason,
        tip_observed_minutes_ago=tip_age,
        report_state=report.state,
        report_status=report_status(report.state, mr, flight),
        report_outcome=mr.outcome if mr is not None else "",
        findings_confirmed=len(current.findings_confirmed) if current else None,
        findings_unresolved=len(current.unresolved) if current else None,
        review_in_flight=flight,
        generation_has_review=gen_review.has_review,
        generation_review_reason=gen_review.reason,
        verdict=latest.verdict.value if latest else None,
        verdict_generation=latest.submission_generation if latest else None,
        verdict_is_current=latest.is_current if latest else False,
        stall_reason=stall,
        stall_at=stall_at,
        waiting_since=since,
        waiting_minutes=(
            max(0, int((datetime.now(UTC) - entered).total_seconds() // 60))
            if entered
            else None
        ),
    )
    row.readiness = readiness(row)
    return row


async def review_queue(db, *, project_id: int | None = None) -> ReviewQueueView:
    """Every task in review and needs_decision, one row each, by readiness."""
    tasks = await repo.list_tasks_by_statuses(
        db, list(QUEUE_STATUSES), limit=QUEUE_LIMIT, project_id=project_id
    )
    rows = [await queue_row(db, dict(t)) for t in tasks]
    rows.sort(
        key=lambda r: (READINESS_ORDER.index(r.readiness), -(r.waiting_minutes or 0))
    )
    note = ""
    if len(tasks) >= QUEUE_LIMIT:
        note = f"показаны первые {QUEUE_LIMIT} задач очереди — очередь длиннее"
    return ReviewQueueView(rows=rows, note=note)
