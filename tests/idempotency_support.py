"""Small database fixtures for T11 operation/outbox tests."""

from hub import repository_idempotency as repo
from hub.idempotency import key_expires_at, result_expires_at

OP = "test-operation-2241"
PRINCIPAL = "t:test"


async def seed_key(db, operation_id=OP):
    await repo.insert_key(
        db,
        principal_ref=PRINCIPAL,
        operation_id=operation_id,
        operation="POST /api/tasks",
        args_hash="test",
        args_hash_version=1,
        response_status=201,
        response_body='{"saved":true}',
        response_headers="{}",
        response_bytes=14,
        expires_at=result_expires_at(),
        key_expires_at=key_expires_at(),
    )
    await db.commit()


async def seed_effect(db, key="dispatch-2241", operation_id=OP, ordering_key=""):
    from hub.db import write_transaction
    from hub.services.effects import enqueue_effect

    async with write_transaction(db):
        return await enqueue_effect(
            db,
            operation_id=operation_id,
            principal_ref=PRINCIPAL,
            effect_kind="task.dispatch",
            effect_key=key,
            payload={"task_id": 1},
            ordering_key=ordering_key,
        )
