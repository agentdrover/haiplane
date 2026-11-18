"""Transactional intake and owner answers for results of an admitted role run."""

import hashlib
import json
import re
import unicodedata
from contextlib import asynccontextmanager
from hmac import compare_digest
from typing import Any, Literal, NoReturn

import aiosqlite
from fastapi import HTTPException

from hub import repository as repo
from hub.agent_outbox_models import OutboxAck, OutboxStatus, OutboxPublish
from hub.db import fetchall, write_transaction
from hub.services import role_orders
from hub.services.secret_values import find_secret
from hub.services.untrusted_quote import sanitize_untrusted_quote

PENDING_LIMIT = 100
HOURLY_LIMIT = 120
_HUB_SECRET = re.compile(r"(?i)\b(?:ochk_|ahcp_|gho_)[A-Za-z0-9_-]{12,}")
_LEASE = re.compile(r"[a-f0-9]{32}")
TTL_SECONDS = 24 * 60 * 60
RETENTION_SECONDS = 14 * 24 * 60 * 60


def refuse(status: int, reason: str) -> NoReturn:
    raise HTTPException(status, detail={"reason": reason})


async def one(db: aiosqlite.Connection, sql: str, args: tuple) -> aiosqlite.Row | None:
    rows = await fetchall(db, sql, args)
    return next(iter(rows), None)


def _contains_secret(text: str) -> bool:
    normalized = "".join(
        char
        for char in unicodedata.normalize("NFKC", text)
        if unicodedata.category(char) != "Cf"
    )
    return bool(find_secret(normalized) or _HUB_SECRET.search(normalized))


def clean_text(text: str) -> str:
    if len(text) > 4000:
        refuse(422, "outbox_invalid_body")
    if _contains_secret(text):
        refuse(422, "outbox_secret_detected")
    return sanitize_untrusted_quote(text, out_limit=4000)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


async def check_host(db: aiosqlite.Connection, host: str, principal: int) -> None:
    try:
        await role_orders.check_host(db, host, principal)
    except role_orders.RoleOrderError as exc:
        refuse(exc.status, "outbox_host_not_allowed")


async def live_order(
    db: aiosqlite.Connection, order_id: int, host: str
) -> aiosqlite.Row | None:
    order = await one(
        db,
        """SELECT o.*, p.slug AS project_slug,
                  t.submission_generation AS current_generation,
                  t.human_owner AS human_owner, t.archived AS task_archived,
                  o.started_at > datetime('now', ?) AS run_within_deadline,
                  a.host IS NOT NULL AS admitted
           FROM role_orders o JOIN projects p ON p.id=o.project_id
           JOIN tasks t ON t.id=o.task_id
           LEFT JOIN private_project_security private ON private.project_id=p.id
           LEFT JOIN role_host_admissions a
             ON a.host=o.host AND a.role=o.role AND a.project_id=o.project_id
           WHERE o.id=? AND o.host=? AND private.project_id IS NULL AND p.status='active'""",
        (f"-{role_orders.RUN_MAX_MINUTES} minutes", order_id, host),
    )
    if order is not None:
        project = await repo.resolve_bound_project(db, order["task_id"])
        if project is None:
            project = await repo.get_project_by_slug(db, "default")
        if project is not None and project["id"] == order["project_id"]:
            return order
    return None


def _matches(order: aiosqlite.Row, data: OutboxPublish) -> bool:
    return (
        order["project_slug"] == data.project
        and order["role"] == data.role
        and order["task_id"] == data.task_id
        and order["generation"] == data.generation
        and order["attempt"] == data.run
        and compare_digest(order["lease_id"] or "", data.lease_id.get_secret_value())
    )


async def _owner(db: aiosqlite.Connection, username: str) -> int | None:
    row = await one(
        db,
        "SELECT id FROM principals WHERE username=? AND kind='human' AND status='active'",
        (username,),
    )
    return int(row["id"]) if row else None


def _availability(order: aiosqlite.Row, generation: int) -> str:
    if order["task_archived"]:
        return "task_unavailable"
    if order["current_generation"] != generation:
        return "generation_changed"
    if not order["admitted"]:
        return "admission_revoked"
    return ""


async def _persist(
    db: aiosqlite.Connection,
    order: aiosqlite.Row,
    principal: int,
    data: OutboxPublish,
    text: str,
    fingerprint: str,
) -> OutboxAck:
    owner_id = None
    if data.kind == "question":
        owner_id = await _owner(db, order["human_owner"])
        if owner_id is None:
            refuse(409, "outbox_owner_unavailable")
    state: Literal["pending", "sent"] = "sent" if data.kind == "silent" else "pending"
    cursor = await db.execute(
        """INSERT INTO agent_outbox (
               principal_id, host, project_id, project_slug, role, task_id, order_id,
               generation, run, continuation, delivery_key, digest, kind, body, state,
               owner_id, reason
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(order_id,run,continuation) DO NOTHING""",
        (
            principal,
            order["host"],
            order["project_id"],
            data.project,
            data.role,
            data.task_id,
            order["id"],
            data.generation,
            data.run,
            data.continuation,
            data.delivery_key,
            fingerprint,
            data.kind,
            text,
            state,
            owner_id,
            "silent" if data.kind == "silent" else "",
        ),
    )
    if cursor.rowcount != 1 or cursor.lastrowid is None:
        await cursor.close()
        refuse(409, "outbox_continuation_conflict")
    delivery_id = cursor.lastrowid
    await cursor.close()
    return OutboxAck(id=delivery_id, accepted_state=state)


async def _check_new(
    db: aiosqlite.Connection, order: aiosqlite.Row, data: OutboxPublish, principal: int
) -> None:
    if order["state"] != "started" or not order["run_within_deadline"]:
        refuse(409, "outbox_run_inactive")
    stale = _availability(order, data.generation)
    if stale:
        refuse(409, stale)
    if data.kind == "question" and not await role_orders.is_fresh(db, order):
        refuse(409, "task_entry_changed")
    count = await one(
        db,
        "SELECT COUNT(*) AS n FROM agent_outbox WHERE principal_id=? AND state='pending'",
        (principal,),
    )
    if count and count["n"] >= PENDING_LIMIT:
        refuse(429, "outbox_queue_full")
    recent = await one(
        db,
        "SELECT COUNT(*) AS n FROM agent_outbox WHERE principal_id=? AND created_at>=CAST(strftime('%s','now') AS INTEGER)-3600",
        (principal,),
    )
    if recent and recent["n"] >= HOURLY_LIMIT:
        refuse(429, "outbox_rate_limited")


async def publish(
    db: aiosqlite.Connection,
    host: str,
    principal: int,
    order_id: int,
    data: OutboxPublish,
) -> OutboxAck:
    lease = data.lease_id.get_secret_value()
    if not _LEASE.fullmatch(lease):
        refuse(422, "outbox_invalid_lease")
    if any(
        lease in field
        for field in (data.body, data.delivery_key, data.continuation or "")
    ):
        refuse(422, "outbox_secret_detected")
    text = clean_text(data.body.strip())
    if lease in text:
        refuse(422, "outbox_secret_detected")
    if bool(data.continuation) != (data.kind == "question"):
        refuse(422, "outbox_invalid_continuation")
    if bool(text) != (data.kind != "silent"):
        refuse(422, "outbox_invalid_body")
    if _contains_secret(data.delivery_key) or _contains_secret(data.continuation or ""):
        refuse(422, "outbox_secret_detected")
    fingerprint = digest(
        data.model_dump(exclude={"lease_id"}) | {"body": data.body.strip()}
    )
    async with write_transaction(db):
        await check_host(db, host, principal)
        order = await live_order(db, order_id, host)
        if order is None or not _matches(order, data):
            refuse(404, "outbox_not_found")
        previous = await one(
            db,
            "SELECT * FROM agent_outbox WHERE principal_id=? AND project_id=? AND delivery_key=?",
            (principal, order["project_id"], data.delivery_key),
        )
        if previous:
            if previous["order_id"] != order_id or previous["digest"] != fingerprint:
                refuse(409, "outbox_key_conflict")
            if not order["admitted"]:
                refuse(409, "admission_revoked")
            return OutboxAck(
                id=previous["id"],
                accepted_state="sent" if data.kind == "silent" else "pending",
            )
        await _check_new(db, order, data, principal)
        return await _persist(db, order, principal, data, text, fingerprint)


async def continuation_refusal(
    db: aiosqlite.Connection, row: aiosqlite.Row, order: aiosqlite.Row | None
) -> str:
    if row["invalidated_reason"]:
        return row["invalidated_reason"]
    if order is None or order["attempt"] != row["run"]:
        return "order_unavailable"
    if order["state"] not in ("started", "done"):
        return "outbox_run_inactive"
    if order["state"] == "started" and not order["run_within_deadline"]:
        return "outbox_run_inactive"
    try:
        await check_host(db, row["host"], row["principal_id"])
    except HTTPException:
        return "host_disabled"
    reason = _availability(order, row["generation"])
    if reason:
        return reason
    owner = await _owner(db, order["human_owner"])
    if owner != row["owner_id"]:
        return "owner_changed"
    if not await role_orders.is_fresh(db, order):
        return "task_entry_changed"
    return ""


async def get_status(
    db: aiosqlite.Connection,
    host: str,
    principal: int,
    order_id: int,
    delivery_id: int,
) -> OutboxStatus:
    # The HTTP door requires its own request connection.
    async with _read_snapshot(db):
        return await _status(db, host, principal, order_id, delivery_id)


@asynccontextmanager
async def _read_snapshot(db: aiosqlite.Connection):
    if db.in_transaction:
        yield
        return
    await db.execute("BEGIN")
    try:
        yield
    finally:
        await db.rollback()


async def _status(
    db: aiosqlite.Connection,
    host: str,
    principal: int,
    order_id: int,
    delivery_id: int,
) -> OutboxStatus:
    await check_host(db, host, principal)
    row = await one(
        db,
        """SELECT * FROM agent_outbox WHERE id=? AND order_id=? AND host=? AND principal_id=?
           AND NOT EXISTS (
               SELECT 1 FROM private_project_security p WHERE p.project_id=agent_outbox.project_id
           )""",
        (delivery_id, order_id, host, principal),
    )
    order = await live_order(db, order_id, host)
    if row is None or order is None:
        refuse(404, "outbox_not_found")
    reason = await continuation_refusal(db, row, order) if row["continuation"] else ""
    state = _continuation_state(row, reason)
    return OutboxStatus.model_validate(
        {
            "id": row["id"],
            "state": row["state"],
            "reason": row["reason"],
            "message_id": row["message_id"],
            "answer_message_id": row["answer_message_id"]
            if state == "answered"
            else None,
            "answer_event_id": row["answer_event_id"] if state == "answered" else None,
            "continuation": row["continuation"],
            "continuation_state": state,
            "answer": row["answer"] if state == "answered" else None,
            "continuation_reason": reason,
        }
    )


def _continuation_state(row: aiosqlite.Row, reason: str) -> str:
    if not row["continuation"]:
        return "none"
    if reason:
        return "stale"
    if row["answered_at"]:
        return "answered"
    if row["state"] == "pending":
        return "queued"
    if row["state"] in ("failed", "unknown"):
        return "undeliverable"
    return "waiting"


async def append_message(
    db: aiosqlite.Connection,
    row: aiosqlite.Row,
    *,
    actor_id: int,
    actor: str,
    to_kind: str,
    to_ref: str,
    kind: str,
    text: str,
    thread_id: str = "",
) -> tuple[int, int]:
    message_id = await repo.insert_agent_message(
        db,
        from_principal_id=actor_id,
        from_session_id="",
        from_agent=actor,
        from_model="",
        to_kind=to_kind,
        to_ref=to_ref,
        body=text,
        kind=kind,
        related_task_id=row["task_id"],
        thread_id=thread_id,
    )
    event_id = await repo.insert_event(
        db,
        kind="message_posted",
        task_id=row["task_id"],
        actor=actor,
        payload={
            "message_id": message_id,
            "to_kind": to_kind,
            "to_ref": to_ref,
            "kind": kind,
            "from_agent": actor,
            "from_session_id": "",
            "for_session": "",
        },
    )
    return message_id, event_id


async def answer(
    db: aiosqlite.Connection,
    task_id: int,
    message_id: int,
    owner_id: int,
    username: str,
    body: str,
) -> dict:
    text = clean_text(body.strip())
    if not text:
        refuse(422, "outbox_invalid_body")
    fingerprint = digest(body.strip())
    async with write_transaction(db):
        row = await one(
            db,
            """SELECT o.*, t.human_owner FROM agent_outbox o JOIN tasks t ON t.id=o.task_id
               WHERE o.task_id=? AND o.message_id=? AND o.kind='question' AND o.owner_id=?
               AND NOT EXISTS (SELECT 1 FROM private_project_security p WHERE p.project_id=o.project_id)""",
            (task_id, message_id, owner_id),
        )
        if (
            row is None
            or row["human_owner"] != username
            or await _owner(db, username) != owner_id
        ):
            refuse(404, "outbox_not_found")
        order = await live_order(db, row["order_id"], row["host"])
        reason = await continuation_refusal(db, row, order)
        if reason:
            refuse(409, reason)
        if row["answered_at"]:
            if row["answer_digest"] != fingerprint:
                refuse(409, "outbox_answer_conflict")
            return {"id": row["id"], "answer_message_id": row["answer_message_id"]}
        lease = order["lease_id"] if order is not None else None
        if lease and (lease in body or lease in text):
            refuse(422, "outbox_secret_detected")
        answer_id, event_id = await append_message(
            db,
            row,
            actor_id=owner_id,
            actor=username,
            to_kind="agent",
            to_ref=f"executor-host-{row['host']}",
            kind="answer",
            text=text,
            thread_id=str(message_id),
        )
        await db.execute(
            """UPDATE agent_outbox SET answer=?, answer_digest=?, answer_message_id=?, answer_event_id=?,
                   answered_at=CAST(strftime('%s','now') AS INTEGER) WHERE id=?""",
            (text, fingerprint, answer_id, event_id, row["id"]),
        )
        return {"id": row["id"], "answer_message_id": answer_id}


async def answerable_messages(
    db: aiosqlite.Connection, task_id: int, owner_id: int
) -> set[int]:
    rows = await fetchall(
        db,
        """SELECT o.* FROM agent_outbox o JOIN tasks t ON t.id=o.task_id
           JOIN principals owner ON owner.id=o.owner_id AND owner.username=t.human_owner
           WHERE o.task_id=? AND owner.id=? AND owner.kind='human' AND owner.status='active'
             AND o.kind='question' AND o.message_id IS NOT NULL AND o.answered_at IS NULL
             AND o.invalidated_reason=''
             AND o.generation=t.submission_generation AND NOT t.archived
             AND NOT EXISTS (SELECT 1 FROM private_project_security p WHERE p.project_id=o.project_id)
           ORDER BY o.message_id DESC LIMIT 200""",
        (task_id, owner_id),
    )
    answerable = set()
    for row in rows:
        order = await live_order(db, row["order_id"], row["host"])
        if not await continuation_refusal(db, row, order):
            answerable.add(int(row["message_id"]))
    return answerable
