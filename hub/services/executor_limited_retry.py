"""One atomic repeat of a pinned k8 order that demonstrably never started."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import aiosqlite

from hub import repository as repo
from hub.db import fetchall, write_transaction
from hub.services import executor_orders as orders
from hub.services import executor_skill_bundle as skills
from hub.services import steward_limited_act as scope

if TYPE_CHECKING:
    from hub.services.executor_orders import _Booking

HOST = "k8"


@dataclass(frozen=True)
class RetryIntent:
    source_order_id: int
    task_id: int
    statement_generation: int
    generation: int
    starting_sha: str
    model: str
    approval_event_id: int
    expires_at: datetime


@dataclass(frozen=True)
class IncludedQuota:
    """Proof from the enforcing runtime for this intent, never from a task or host ACK."""

    intent: RetryIntent
    expires_at: datetime
    workspace_ready: bool
    zero_additional_cents: bool
    resume_after: datetime | None = None

    def valid_for(self, intent: RetryIntent, now: datetime) -> bool:
        return (
            self.intent == intent
            and now < self.expires_at <= intent.expires_at
            and self.zero_additional_cents
        )


class RetryTransport(Protocol):
    async def preflight(self, intent: RetryIntent) -> IncludedQuota | None:
        """Read current host readiness and quota. No provider operation starts here."""
        ...

    async def issue(
        self,
        db: aiosqlite.Connection,
        project: Any,
        booking: _Booking,
        grant: IncludedQuota,
    ) -> int:
        """Queue a locally durable order in this transaction without network or commit.

        The worker rechecks the exact bounded admission before contacting a model.
        """
        ...


def retry_transport() -> RetryTransport | None:
    """A regular k8 order cannot enforce included-only quota on model start."""
    return None


async def _intent(db: aiosqlite.Connection) -> RetryIntent | None:
    from hub.services.steward_apply import policy_refusal
    from hub.services.steward_shadow import act_granted_now

    pilot = scope.read_pilot()
    active = pilot.active_pin(datetime.now(UTC)) if pilot else None
    if pilot is None or active is None or active.retry is None:
        return None
    if await policy_refusal(db, active.task_id) or not await act_granted_now(db):
        return None
    if not await scope.approval_matches(db, pilot):
        return None
    project = await repo.resolve_project_for_task(db, active.task_id)
    if project is None or project["slug"] != pilot.project:
        return None
    if not await _source_matches(db, active):
        return None

    retry = active.retry
    return RetryIntent(
        retry.source_order_id,
        active.task_id,
        active.statement_generation,
        active.submission_generation + 1,
        retry.starting_sha,
        retry.model,
        pilot.approval_event_id,
        pilot.expires_at,
    )


async def _source_matches(db: aiosqlite.Connection, active: scope.TaskPin) -> bool:
    source = active.retry
    if source is None:
        return False
    rows = await fetchall(
        db,
        """SELECT 1
           FROM executor_orders o
           JOIN tasks t ON t.id = o.task_id
           JOIN executor_runs r ON r.id = o.executor_run_id
           WHERE o.id = ? AND t.id = ? AND o.host = 'k8'
             AND o.project_slug = 'default' AND o.state = 'closed'
             AND o.ack_result = 'redeem_failed' AND o.close_reason = o.ack_result
             AND o.closed_at IS NOT NULL AND o.acked_at IS NOT NULL
             AND o.first_redeemed_at IS NULL AND o.redeemed_at IS NULL
             AND o.session_id IS NULL
             AND o.report_outcome IN ('', 'failed')
             AND o.report_reason_code IN ('', 'redeem_failed')
             AND o.report_sha = '' AND o.report_branch = ''
             AND t.status = 'open' AND NOT t.archived AND t.risk_class IN ('R0', 'R1')
             AND t.statement_generation = ? AND o.statement_generation = t.statement_generation
             AND t.submission_generation = ? AND o.generation = t.submission_generation + 1
             AND r.task_id = t.id AND r.submission_generation = o.generation
             AND r.channel = 'slot' AND r.slot = o.host AND r.outcome = 'aborted'
             AND r.starting_ref = ? AND r.model = ?
             AND r.agent_id = '' AND r.run_id = ''
             AND NOT EXISTS (SELECT 1 FROM chat_pair_sessions s WHERE s.executor_run_id = r.id)
             AND NOT EXISTS (SELECT 1 FROM executor_orders newer
                             WHERE newer.task_id = o.task_id AND newer.id > o.id)
             AND NOT EXISTS (SELECT 1 FROM executor_limited_retry retry
                             WHERE retry.task_id = t.id
                               AND retry.statement_generation = t.statement_generation
                               AND retry.generation = o.generation
                               AND (retry.order_id IS NOT NULL
                                    OR julianday(retry.resume_after) > julianday('now')))
           """,
        (
            source.source_order_id,
            active.task_id,
            active.statement_generation,
            active.submission_generation,
            source.starting_sha,
            source.model,
        ),
    )
    return bool(rows)


async def _wait_for_quota(
    db: aiosqlite.Connection, intent: RetryIntent, resume_after: datetime
) -> None:
    recorded = await db.execute(
        """INSERT INTO executor_limited_retry
           (source_order_id, task_id, statement_generation, generation, resume_after)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(task_id, statement_generation, generation) DO UPDATE
           SET resume_after = excluded.resume_after
           WHERE source_order_id = excluded.source_order_id AND order_id IS NULL
           """,
        (
            intent.source_order_id,
            intent.task_id,
            intent.statement_generation,
            intent.generation,
            resume_after.astimezone(UTC).isoformat(),
        ),
    )

    if recorded.rowcount == 1:
        await repo.insert_event(
            db,
            kind="steward_executor_retry_waiting_quota",
            task_id=intent.task_id,
            actor="hub",
            payload={
                "source_order_id": intent.source_order_id,
                "generation": intent.generation,
                "resume_after": resume_after.astimezone(UTC).isoformat(),
                "approval_event_id": intent.approval_event_id,
            },
        )


async def _verify_issued(
    db: aiosqlite.Connection, intent: RetryIntent, order_id: int
) -> None:
    rows = await fetchall(
        db,
        """SELECT 1 FROM executor_orders o JOIN executor_runs r ON r.id = o.executor_run_id
           WHERE o.id = ? AND o.id > ? AND o.task_id = ? AND o.generation = ?
             AND o.host = 'k8' AND o.project_slug = 'default'
             AND o.statement_generation = ? AND o.state = 'pending'
             AND r.task_id = o.task_id AND r.submission_generation = o.generation
             AND r.channel = 'slot' AND r.slot = o.host
             AND r.model = ? AND r.starting_ref = ?""",
        (
            order_id,
            intent.source_order_id,
            intent.task_id,
            intent.generation,
            intent.statement_generation,
            intent.model,
            intent.starting_sha,
        ),
    )
    if not rows:
        raise RuntimeError("retry transport did not persist the exact bounded order")


async def _issue(
    db: aiosqlite.Connection,
    transport: RetryTransport,
    intent: RetryIntent,
    grant: IncludedQuota,
) -> int | None:
    now = datetime.now(UTC)
    if not grant.valid_for(intent, now):
        return None
    if grant.resume_after is not None:
        if now < grant.resume_after:
            await _wait_for_quota(
                db, intent, min(grant.resume_after, intent.expires_at)
            )
        return None
    if not grant.workspace_ready:
        return None

    project = await repo.resolve_project_for_task(db, intent.task_id)
    if project is None:
        return None
    bundle = await skills.read_active_bundle(db)
    booked = await orders._book(db, project, HOST, intent.task_id, bundle)
    if isinstance(booked, str) or booked.returning:
        return None
    if not grant.valid_for(intent, datetime.now(UTC)):
        return None
    order_id = await transport.issue(db, project, booked, grant)
    await _verify_issued(db, intent, order_id)
    stamped = await db.execute(
        """INSERT INTO executor_limited_retry
           (source_order_id, task_id, statement_generation, generation, order_id)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(task_id, statement_generation, generation) DO UPDATE
           SET resume_after = NULL, order_id = excluded.order_id
           WHERE source_order_id = excluded.source_order_id AND order_id IS NULL""",
        (
            intent.source_order_id,
            intent.task_id,
            intent.statement_generation,
            intent.generation,
            order_id,
        ),
    )
    if stamped.rowcount != 1:
        raise RuntimeError("retry was already decided; roll back the new order")
    await repo.insert_event(
        db,
        kind="steward_executor_retried",
        task_id=intent.task_id,
        actor="hub",
        payload={
            "source_order_id": intent.source_order_id,
            "order_id": order_id,
            "statement_generation": intent.statement_generation,
            "generation": intent.generation,
            "sha": intent.starting_sha,
            "model": intent.model,
            "approval_event_id": intent.approval_event_id,
            "reason": "confirmed_redeem_failed_before_start",
            "zero_additional_cents": True,
            "quota_valid_until": grant.expires_at.astimezone(UTC).isoformat(),
        },
    )
    return order_id


async def sweep(db: aiosqlite.Connection) -> int | None:
    """Preflight outside the lock. The locked pass rechecks the entire intent."""
    transport = retry_transport()
    if transport is None or db.in_transaction:
        return None
    intent = await _intent(db)
    if intent is None:
        return None
    grant = await transport.preflight(intent)
    if grant is None:
        return None
    async with write_transaction(db):
        if await _intent(db) != intent:
            return None
        return await _issue(db, transport, intent, grant)
