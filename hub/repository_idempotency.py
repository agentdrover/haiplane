"""SQL for operation keys, aliases and the transactional outbox. Never commits."""

from __future__ import annotations

from typing import Any

from hub.db import fetchall, inserted_id


async def _one(db: Any, sql: str, params: tuple = ()) -> Any:
    rows = await fetchall(db, sql, params)
    return rows[0] if rows else None


async def get_key(db: Any, principal: str, operation_id: str) -> Any:
    return await _one(
        db,
        "SELECT * FROM operation_keys WHERE principal_ref=? AND operation_id=?",
        (principal, operation_id),
    )


async def get_key_any_principal(db: Any, operation_id: str) -> Any:
    return await _one(
        db, "SELECT * FROM operation_keys WHERE operation_id=?", (operation_id,)
    )


async def insert_key(db: Any, **fields: Any) -> None:
    await db.execute(
        """INSERT INTO operation_keys (
            principal_ref, operation_id, operation, args_hash, args_hash_version,
            scope_ref, actor_ref, status, response_status, response_body,
            response_headers, response_bytes, created_at, expires_at, key_expires_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,COALESCE(?,datetime('now')),?,?)""",
        (
            fields["principal_ref"],
            fields["operation_id"],
            fields["operation"],
            fields["args_hash"],
            fields["args_hash_version"],
            fields.get("scope_ref"),
            fields.get("actor_ref"),
            fields.get("status", "completed"),
            fields.get("response_status"),
            fields.get("response_body"),
            fields.get("response_headers"),
            fields.get("response_bytes", 0),
            fields.get("created_at"),
            fields["expires_at"],
            fields["key_expires_at"],
        ),
    )


async def get_alias(db: Any, principal: str, alias: str) -> Any:
    return await _one(
        db,
        "SELECT * FROM operation_aliases WHERE principal_ref=? AND alias=?",
        (principal, alias),
    )


async def insert_alias(
    db: Any, principal: str, alias: str, operation_id: str, operation: str
) -> None:
    await db.execute(
        "INSERT INTO operation_aliases (principal_ref,alias,operation_id,operation) VALUES (?,?,?,?)",
        (principal, alias, operation_id, operation),
    )


async def list_effects(db: Any, principal: str, operation_id: str) -> list[Any]:
    return await fetchall(
        db,
        "SELECT * FROM effect_outbox WHERE principal_ref=? AND operation_id=? ORDER BY created_at,rowid",
        (principal, operation_id),
    )


async def get_effect(db: Any, effect_id: str) -> Any:
    return await _one(
        db,
        "SELECT rowid AS sequence,* FROM effect_outbox WHERE effect_id=?",
        (effect_id,),
    )


async def insert_effect(db: Any, **fields: Any) -> tuple[Any, bool]:
    cursor = await db.execute(
        """INSERT INTO effect_outbox (
            effect_id, operation_id, principal_ref, effect_kind, effect_key,
            ordering_key, payload_json, payload_hash
        ) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(effect_kind,effect_key) DO NOTHING""",
        (
            fields["effect_id"],
            fields["operation_id"],
            fields["principal_ref"],
            fields["effect_kind"],
            fields["effect_key"],
            fields.get("ordering_key", ""),
            fields["payload_json"],
            fields["payload_hash"],
        ),
    )
    # inserted_id is the DB compatibility helper; rowcount distinguishes no-op.
    if cursor.rowcount:
        inserted_id(cursor)
    row = await _one(
        db,
        "SELECT * FROM effect_outbox WHERE effect_kind=? AND effect_key=?",
        (fields["effect_kind"], fields["effect_key"]),
    )
    return row, row["payload_hash"] == fields["payload_hash"]


async def pick_ready_effects(db: Any, limit: int = 500) -> list[Any]:
    return await fetchall(
        db,
        """SELECT e.* FROM effect_outbox e WHERE effect_kind='task.dispatch'
        AND ((state='pending' AND next_at<=datetime('now')) OR
             (state='running' AND (lease_until IS NULL OR lease_until<=datetime('now'))))
        AND (e.ordering_key='' OR NOT EXISTS (
            SELECT 1 FROM effect_outbox older WHERE older.ordering_key=e.ordering_key
            AND older.state!='done' AND (older.created_at<e.created_at OR
                (older.created_at=e.created_at AND older.rowid<e.rowid))))
        ORDER BY created_at,rowid LIMIT ?""",
        (limit,),
    )


async def mark_running(db: Any, effect_id: str, owner: str, until: str) -> bool:
    cursor = await db.execute(
        """UPDATE effect_outbox AS e SET state='running',attempts=attempts+1,
        lease_owner=?,lease_until=? WHERE effect_id=? AND effect_kind='task.dispatch'
        AND state='pending' AND attempts=0 AND next_at<=datetime('now')
        AND (e.ordering_key='' OR NOT EXISTS (
            SELECT 1 FROM effect_outbox older WHERE older.ordering_key=e.ordering_key
            AND older.state!='done' AND (older.created_at<e.created_at OR
                (older.created_at=e.created_at AND older.rowid<e.rowid))))""",
        (owner, until, effect_id),
    )
    return cursor.rowcount == 1


async def finalize_effect(
    db: Any,
    effect_id: str,
    owner: str,
    state: str,
    resolution: str | None = None,
    last_error: str | None = None,
) -> bool:
    cursor = await db.execute(
        "UPDATE effect_outbox SET state=?,resolution=?,last_error=?,finished_at=datetime('now'),lease_until=NULL WHERE effect_id=? AND lease_owner=? AND state='running'",
        (state, resolution, last_error, effect_id, owner),
    )
    return cursor.rowcount == 1


async def mark_ambiguous(db: Any, effect_id: str) -> None:
    await db.execute(
        "UPDATE effect_outbox SET state='unknown',last_error='dispatch_outcome_unknown',lease_until=NULL WHERE effect_id=? AND attempts>0 AND (state='pending' OR (state='running' AND (lease_until IS NULL OR lease_until<=datetime('now'))))",
        (effect_id,),
    )


async def resolve_effect(
    db: Any, effect_id: str, state: str, resolution: str, resolved_by: str
) -> bool:
    cursor = await db.execute(
        "UPDATE effect_outbox SET state=?,resolution=?,resolved_by=?,resolved_at=datetime('now'),finished_at=datetime('now'),lease_until=NULL WHERE effect_id=? AND state IN ('unknown','failed','dead')",
        (state, resolution, resolved_by, effect_id),
    )
    return cursor.rowcount == 1


async def has_unfinished_effects(db: Any, principal: str, operation_id: str) -> bool:
    return bool(
        await _one(
            db,
            "SELECT 1 FROM effect_outbox WHERE principal_ref=? AND operation_id=? AND state IN ('pending','running','unknown') LIMIT 1",
            (principal, operation_id),
        )
    )


async def expire_results(db: Any, limit: int = 500) -> None:
    await db.execute(
        """UPDATE operation_keys SET status='expired',response_body=NULL,
        response_headers=NULL,response_status=NULL,response_bytes=0
        WHERE rowid IN (SELECT rowid FROM operation_keys
            WHERE status='completed' AND expires_at<=datetime('now')
            AND NOT EXISTS (SELECT 1 FROM effect_outbox e
                WHERE e.principal_ref=operation_keys.principal_ref
                AND e.operation_id=operation_keys.operation_id
                AND e.state IN ('pending','running','unknown')) LIMIT ?)""",
        (limit,),
    )


async def delete_keys(db: Any, limit: int = 500) -> None:
    # Aliases are deleted by the composite FK's ON DELETE CASCADE.
    await db.execute(
        """DELETE FROM operation_keys WHERE rowid IN (
            SELECT rowid FROM operation_keys WHERE key_expires_at<=datetime('now')
            AND NOT EXISTS (SELECT 1 FROM effect_outbox e
                WHERE e.principal_ref=operation_keys.principal_ref
                AND e.operation_id=operation_keys.operation_id
                AND e.state IN ('pending','running','unknown')) LIMIT ?)""",
        (limit,),
    )


async def delete_done_effects(db: Any, ttl_seconds: int, limit: int = 500) -> None:
    await db.execute(
        "DELETE FROM effect_outbox WHERE rowid IN (SELECT rowid FROM effect_outbox WHERE state='done' AND finished_at<=datetime('now',?) LIMIT ?)",
        (f"-{ttl_seconds} seconds", limit),
    )
