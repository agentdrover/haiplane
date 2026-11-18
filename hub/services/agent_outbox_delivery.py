"""A bounded local worker. A messaging commit is also its durable ACK."""

import logging
import sqlite3
import uuid

import aiosqlite

from hub import config
from hub import repository as repo
from hub.db import write_transaction
from hub.services import agent_outbox as outbox

log = logging.getLogger(__name__)
BATCH = 20
LEASE_SECONDS = 60
MAX_ATTEMPTS = 8


async def _lease(db: aiosqlite.Connection, token: str) -> aiosqlite.Row | None:
    async with write_transaction(db):
        return await outbox.one(
            db,
            """UPDATE agent_outbox SET lease=?, lease_until=unixepoch()+?, attempts=attempts+1
               WHERE id=(SELECT id FROM agent_outbox WHERE state='pending'
                   AND lease_until<=unixepoch() AND next_attempt<=unixepoch()
                   ORDER BY next_attempt,id LIMIT 1)
               RETURNING *""",
            (token, LEASE_SECONDS),
        )


async def _retry(db: aiosqlite.Connection, row: aiosqlite.Row, reason: str) -> None:
    exhausted = row["attempts"] >= MAX_ATTEMPTS
    state = "pending"
    if exhausted:
        state = (
            "unknown"
            if reason in ("storage_error", "lease_recovery_exhausted")
            else "failed"
        )
    async with write_transaction(db):
        await db.execute(
            """UPDATE agent_outbox SET state=?, reason=?, lease=NULL, lease_until=0,
                   next_attempt=unixepoch()+? WHERE id=? AND lease=? AND state='pending'""",
            (
                state,
                reason,
                min(60 * 2 ** (row["attempts"] - 1), 3600),
                row["id"],
                row["lease"],
            ),
        )


async def _fail(db: aiosqlite.Connection, row: aiosqlite.Row, reason: str) -> None:
    await db.execute(
        """UPDATE agent_outbox SET state='failed', reason=?, lease=NULL,
               lease_until=0 WHERE id=? AND lease=?""",
        (reason, row["id"], row["lease"]),
    )


async def _deliver(db: aiosqlite.Connection, row: aiosqlite.Row) -> None:
    async with write_transaction(db):
        standing = await outbox.one(
            db,
            """SELECT created_at+?<=unixepoch() AS expired FROM agent_outbox
               WHERE id=? AND lease=? AND lease_until>unixepoch() AND state='pending'""",
            (outbox.TTL_SECONDS, row["id"], row["lease"]),
        )
        if standing is None:
            return
        if standing["expired"]:
            await _fail(db, row, "delivery_expired")
            return
        order = await outbox.live_order(db, row["order_id"], row["host"])
        if order is None or order["task_archived"]:
            await _fail(db, row, "task_unavailable")
            return
        if row["kind"] == "question":
            stale = await outbox.continuation_refusal(db, row, order)
            if stale:
                await _fail(db, row, stale)
                return
        if row["attempts"] > MAX_ATTEMPTS:
            await _retry(db, row, "lease_recovery_exhausted")
            return
        actor = f"executor-host-{row['host']}"
        if (
            await repo.count_recent_messages(db, session_id="", agent=actor)
            >= config.MESSAGE_RATE_PER_MINUTE
        ):
            await _retry(db, row, "rate_limited")
            return
        message_id, _ = await outbox.append_message(
            db,
            row,
            actor_id=row["principal_id"],
            actor=actor,
            to_kind="task",
            to_ref=str(row["task_id"]),
            kind="question" if row["kind"] == "question" else "note",
            text=row["body"],
        )
        await db.execute(
            """UPDATE agent_outbox SET state='sent', message_id=?, reason='', lease=NULL,
                   lease_until=0 WHERE id=?""",
            (message_id, row["id"]),
        )


async def sweep(db: aiosqlite.Connection) -> None:
    for _ in range(BATCH):
        row = await _lease(db, uuid.uuid4().hex)
        if row is None:
            break
        try:
            await _deliver(db, row)
        except sqlite3.Error:
            log.warning("Role outbox message delivery failed for id=%s", row["id"])
            await _retry(db, row, "storage_error")
    async with write_transaction(db):
        await db.execute(
            """DELETE FROM agent_outbox WHERE id IN (
                   SELECT id FROM agent_outbox WHERE state<>'pending'
                     AND created_at+?<=unixepoch() ORDER BY id LIMIT ?
               )""",
            (outbox.RETENTION_SECONDS, BATCH),
        )
