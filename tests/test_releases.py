"""What is actually running in production, as a stored fact (#839).

The hub already records its own merges (#534) and read them as delivery. On
21.08.2026 that reading was wrong in a way nobody could see from the hub: task
#823 sat ``completed`` with its PR merged into develop, while the deploy job
was skipped — deployment runs from main. "Merged" and "running" were the same
fact, and one of them was false.

These tests hold the two properties that keep the new fact honest: a failed
deploy never becomes the state of production, and NO RECORD reads as unknown
rather than as "nothing is deployed".
"""

from __future__ import annotations

import aiosqlite

from hub import repository as repo
from hub.db import _migrate


async def test_migration_creates_releases_and_is_idempotent(db: aiosqlite.Connection):
    # AC-1 (#839): the table exists after migration, and running migrations a
    # second time — every hub start does — must not fail. A migration that
    # only works once is a boot failure waiting for a restart.
    rows = list(
        await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='releases'"
        )
    )
    assert rows, "releases table must exist after migrations"

    await _migrate(db)

    rows = list(
        await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='releases'"
        )
    )
    assert rows, "a repeated migration run must leave the table in place"


async def test_latest_release_returns_what_was_recorded(db: aiosqlite.Connection):
    # AC-2 (#839): the stored deploy comes back as itself — sha and time.
    await repo.record_release(
        db,
        deployed_sha="deadbee" + "f" * 33,
        project_id=1,
        ref="main",
        source="ci",
    )

    latest = await repo.latest_successful_release(db, 1)

    assert latest is not None
    assert latest["deployed_sha"].startswith("deadbee")
    assert latest["ref"] == "main"
    assert latest["deployed_at"], "a deploy without a time cannot be reasoned about"


async def test_failed_deploy_does_not_become_prod_state(db: aiosqlite.Connection):
    # AC-3 (#839): when a deploy falls over, production keeps running the code
    # from the last one that worked. Reading "the newest row" instead would
    # name a commit that never started.
    await repo.record_release(
        db, deployed_sha="a" * 40, project_id=1, ref="main", source="ci"
    )
    await repo.record_release(
        db,
        deployed_sha="b" * 40,
        project_id=1,
        ref="main",
        status=repo.RELEASE_FAILED,
        source="ci",
    )

    latest = await repo.latest_successful_release(db, 1)

    assert latest is not None
    assert latest["deployed_sha"] == "a" * 40, (
        "a failed deploy must not be reported as what is running"
    )
    failed = list(
        await db.execute_fetchall("SELECT * FROM releases WHERE status='failed'")
    )
    assert failed, "the failure is still recorded — it is evidence about the pipeline"


async def test_no_records_reads_as_unknown(db: aiosqlite.Connection):
    # AC-4 (#839): an installation that predates this table, or one whose CI
    # does not report yet, knows nothing about production. It must not answer
    # "nothing is deployed" — that is silence turned into a denial, the exact
    # failure this epic removes.
    assert await repo.latest_successful_release(db, 1) is None
    assert await repo.latest_successful_release(db) is None
