"""Small database fixtures for T11 operation/outbox tests."""

from hub.db import write_transaction
from hub.idempotency import key_expires_at, result_expires_at

OP = "test-operation-2241"
PRINCIPAL = "t:test"

_INSERT_KEY_SQL = """INSERT OR IGNORE INTO operation_keys (
    principal_ref, operation_id, operation, args_hash, args_hash_version,
    scope_ref, actor_ref, status, response_status, response_body,
    response_headers, response_bytes, expires_at, key_expires_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


async def _seed_key_row(db, operation_id: str) -> None:
    """The real create commits the key and the effect in ONE transaction.

    The outbox checks the intent it was committed with, so a bare effect row
    would be a state production cannot produce. OR IGNORE lets a test seed the
    key first and the effect afterwards without a duplicate-key failure.
    """
    await db.execute(
        _INSERT_KEY_SQL,
        (
            PRINCIPAL,
            operation_id,
            "POST /api/tasks",
            "test",
            1,
            None,
            None,
            "completed",
            201,
            '{"saved":true}',
            "{}",
            14,
            result_expires_at(),
            key_expires_at(),
        ),
    )


async def seed_key(db, operation_id=OP):
    await _seed_key_row(db, operation_id)
    await db.commit()


async def seed_effect(db, key="dispatch-2241", operation_id=OP, ordering_key=""):
    from hub.services.effects import enqueue_effect

    async with write_transaction(db):
        await _seed_key_row(db, operation_id)
        return await enqueue_effect(
            db,
            operation_id=operation_id,
            principal_ref=PRINCIPAL,
            effect_kind="task.dispatch",
            effect_key=key,
            payload={"task_id": 1},
            ordering_key=ordering_key,
        )
