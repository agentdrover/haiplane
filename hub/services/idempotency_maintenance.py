"""Bounded operation retention and dispatch recovery for the poller."""

from hub import config
from hub import repository_idempotency as repo
from hub.db import write_transaction
from hub.services.effects import process_ready_effects


async def run_sweep(db) -> None:
    await process_ready_effects(db)
    async with write_transaction(db):
        await repo.expire_results(db)
        await repo.delete_keys(db)
        await repo.delete_done_effects(db, config.IDEMPOTENCY_KEY_TTL_SECONDS)
