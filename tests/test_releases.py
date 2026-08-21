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


# ---- #495: the deploy callback, the only writer of the facts above ----


def _deploy_tokens(monkeypatch) -> dict[str, dict[str, str]]:
    """Identities for the callback: one that may record, two that may not.

    Production grants ``deploys.record`` through the DB-backed ``ci_runner``
    role; from env tokens only ``admin`` carries every permission, so that
    stands in for CI here — the same substitution the ci-run-report tests make.
    """
    from hub import config

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        config.parse_tokens("denis:human-token:human,ci:ci-token:admin"),
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return {
        "ci": {"Authorization": "Bearer ci-token"},
        "human": {"Authorization": "Bearer human-token"},
        "none": {},
    }


async def _release_rows(db: aiosqlite.Connection) -> list[dict]:
    return [dict(r) for r in await db.execute_fetchall("SELECT * FROM releases")]


async def test_deploy_callback_requires_auth(client, db, monkeypatch):
    # AC-1 (#495): checked against the TABLE, not the status code. A 401 that
    # still wrote a row would be the worst of both — the caller told "no" while
    # the hub believes something shipped.
    auth = _deploy_tokens(monkeypatch)
    body = {"sha": "shipped-one", "ref": "main", "status": "success"}

    anonymous = await client.post("/api/deploys", json=body, headers=auth["none"])
    as_human = await client.post("/api/deploys", json=body, headers=auth["human"])

    assert anonymous.status_code in (401, 403), anonymous.text
    assert as_human.status_code == 403, "a human token does not carry deploys.record"
    assert await _release_rows(db) == [], "a refused call must write nothing"


async def test_deploy_callback_records_release(client, db, monkeypatch):
    # AC-2 (#495): the fact enters the hub and reads back as production state.
    auth = _deploy_tokens(monkeypatch)

    resp = await client.post(
        "/api/deploys",
        json={"sha": "shipped-two", "ref": "main", "status": "success"},
        headers=auth["ci"],
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["deployed_sha"] == "shipped-two"
    latest = await repo.latest_successful_release(db)
    assert latest is not None and latest["deployed_sha"] == "shipped-two"
    assert latest["source"], "who reported it is part of the fact"


async def test_deploy_callback_is_idempotent_by_sha(client, db, monkeypatch):
    # AC-3 (#495): CI runs get re-run, and a re-run redelivers the callback.
    # Two rows would claim the commit was deployed twice.
    auth = _deploy_tokens(monkeypatch)
    body = {"sha": "shipped-three", "ref": "main", "status": "success"}

    first = await client.post("/api/deploys", json=body, headers=auth["ci"])
    second = await client.post("/api/deploys", json=body, headers=auth["ci"])

    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert len(await _release_rows(db)) == 1


async def test_failed_callback_is_stored_but_not_prod_state(client, db, monkeypatch):
    # AC-4 (#495): a failed rollout is evidence about the pipeline and is kept,
    # but production still runs the last release that worked.
    auth = _deploy_tokens(monkeypatch)
    await client.post(
        "/api/deploys",
        json={"sha": "shipped-four", "ref": "main", "status": "success"},
        headers=auth["ci"],
    )
    await client.post(
        "/api/deploys",
        json={"sha": "fell-over", "ref": "main", "status": "failed"},
        headers=auth["ci"],
    )

    rows = await _release_rows(db)
    latest = await repo.latest_successful_release(db)

    assert len(rows) == 2, "the failure is recorded, not dropped"
    assert latest is not None and latest["deployed_sha"] == "shipped-four"
