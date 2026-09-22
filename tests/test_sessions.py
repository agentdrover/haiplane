"""Re-registering a taken session_id must not silently move the address (#1288).

The registry answers one question that only matters during a failure: where is
the working copy of this task, and on which machine. Twice on 2026-09-22 it
answered wrong — an executor registered under a ``session_id`` that already
belonged to another piece of work, and ``host`` and ``workspace`` of the row
``tasks.claim_session_id`` pointed at were overwritten.

The guard from #977 was there and did fire on foreign principals; it just asks
a different question. Every executor runs under the same principal
(``pda_claude``), so an id collision looked to the hub like the same session
saying hello again. These tests hold the missing second question — *is this the
same work address?* — without weakening the first one.
"""

from __future__ import annotations

import aiosqlite
from httpx import AsyncClient

from hub.config import TokenIdentity


def _same_principal_tokens(monkeypatch) -> tuple[dict[str, str], dict[str, str]]:
    """Two tokens, ONE principal — the shape the incident actually had.

    Both executors authenticated as ``pda_claude``, so the owner check of #977
    passed for both of them.
    """
    from hub import config

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "token-one": TokenIdentity("pda_claude", "agent", principal_id=21),
            "token-two": TokenIdentity("pda_claude", "agent", principal_id=21),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return (
        {"Authorization": "Bearer token-one"},
        {"Authorization": "Bearer token-two"},
    )


def _two_principal_tokens(monkeypatch) -> tuple[dict[str, str], dict[str, str]]:
    from hub import config

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "token-p": TokenIdentity("agent-p", "agent", principal_id=31),
            "token-q": TokenIdentity("agent-q", "agent", principal_id=32),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return (
        {"Authorization": "Bearer token-p"},
        {"Authorization": "Bearer token-q"},
    )


async def _rows(db: aiosqlite.Connection, session_id: str) -> list[dict]:
    return [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM agent_sessions WHERE session_id = ?", (session_id,)
        )
    ]


async def _freeze_last_seen(db: aiosqlite.Connection, session_id: str) -> str:
    """Pin last_seen_at so a refused write is observable within the same second."""
    await db.execute(
        "UPDATE agent_sessions SET last_seen_at = datetime('now', '-10 minutes') "
        "WHERE session_id = ?",
        (session_id,),
    )
    await db.commit()
    return (await _rows(db, session_id))[0]["last_seen_at"]


# ---- AC-1: the same principal, a different work address ----


async def test_a_same_principal_reregistration_from_another_workspace_is_not_silent(
    client: AsyncClient, monkeypatch, db
):
    """The registered address survives, and the discrepancy is named.

    Chosen outcome of the two AC-1 allows: refuse. The hub cannot tell "the
    same session moved" from "two sessions collided on an id", and the caller
    can — so it names the conflict instead of guessing which address is real.
    """
    headers_one, headers_two = _same_principal_tokens(monkeypatch)
    created = await client.post(
        "/api/sessions/register",
        json={
            "session_id": "s-shared",
            "model": "claude-opus-5",
            "host": "mac-1281",
            "workspace": "/wt/task-1281",
        },
        headers=headers_one,
    )
    assert created.status_code == 200, created.text
    frozen = await _freeze_last_seen(db, "s-shared")

    collided = await client.post(
        "/api/sessions/register",
        json={
            "session_id": "s-shared",
            "model": "claude-opus-5",
            "host": "mac-1283",
            "workspace": "/wt/task-1283",
        },
        headers=headers_two,
    )
    assert collided.status_code == 409, collided.text
    detail = collided.json()["detail"]
    assert detail["reason"] == "session_address_conflict"
    # The refusal is read by a human and an agent: it names the taken id and
    # what it is taken by, and nothing else.
    assert detail["session_id"] == "s-shared"
    body = collided.text
    assert "s-shared" in body
    assert "/wt/task-1281" in body, "the registered address must be named"
    assert "/wt/task-1283" in body, "the declared address must be named"
    assert "mac-1281" in body and "mac-1283" in body

    row = (await _rows(db, "s-shared"))[0]
    assert row["workspace"] == "/wt/task-1281", "the stored address must survive"
    assert row["host"] == "mac-1281"
    assert row["last_seen_at"] == frozen, "a refused write leaves no sign of life"
    assert len(await _rows(db, "s-shared")) == 1, "no registry row is lost"


async def test_a_changed_host_alone_is_also_refused(
    client: AsyncClient, monkeypatch, db
):
    """Two machines, one workspace path: still two different addresses."""
    headers_one, headers_two = _same_principal_tokens(monkeypatch)
    await client.post(
        "/api/sessions/register",
        json={"session_id": "s-host", "host": "mac-a", "workspace": "/wt/same"},
        headers=headers_one,
    )
    frozen = await _freeze_last_seen(db, "s-host")

    collided = await client.post(
        "/api/sessions/register",
        json={"session_id": "s-host", "host": "mac-b", "workspace": "/wt/same"},
        headers=headers_two,
    )
    assert collided.status_code == 409, collided.text
    assert collided.json()["detail"]["reason"] == "session_address_conflict"
    row = (await _rows(db, "s-host"))[0]
    assert row["host"] == "mac-a"
    assert row["last_seen_at"] == frozen


# ---- AC-2: the same address stays idempotent ----


async def test_the_same_address_stays_idempotent(client: AsyncClient, monkeypatch, db):
    """Saying hello twice from the same place is not a conflict.

    Including the heartbeat-shaped call that declares nothing: an omitted field
    means "not declared", never "erase what you know" and never a mismatch.
    """
    headers_one, headers_two = _same_principal_tokens(monkeypatch)
    first = await client.post(
        "/api/sessions/register",
        json={
            "session_id": "s-steady",
            "model": "claude-opus-5",
            "host": "mac-1",
            "workspace": "/wt/task-1288",
        },
        headers=headers_one,
    )
    assert first.status_code == 200, first.text
    started = first.json()["started_at"]
    frozen = await _freeze_last_seen(db, "s-steady")

    same = await client.post(
        "/api/sessions/register",
        json={
            "session_id": "s-steady",
            "model": "claude-opus-5",
            "host": "mac-1",
            "workspace": "/wt/task-1288",
        },
        headers=headers_two,
    )
    assert same.status_code == 200, same.text
    assert same.json()["started_at"] == started, "the session did not restart"
    assert same.json()["last_seen_at"] > frozen

    frozen = await _freeze_last_seen(db, "s-steady")
    bare = await client.post(
        "/api/sessions/register",
        json={"session_id": "s-steady"},
        headers=headers_one,
    )
    assert bare.status_code == 200, bare.text
    view = bare.json()
    assert view["started_at"] == started
    assert view["host"] == "mac-1", "an omitted field must not erase what is known"
    assert view["workspace"] == "/wt/task-1288"
    assert view["model"] == "claude-opus-5"
    assert view["last_seen_at"] > frozen, "a heartbeat-shaped register still counts"

    rows = await _rows(db, "s-steady")
    assert len(rows) == 1
    assert rows[0]["workspace"] == "/wt/task-1288"


async def test_a_first_declaration_fills_an_unknown_address(
    client: AsyncClient, monkeypatch, db
):
    """Nothing stored yet is not a conflict — the registry learns the address."""
    headers_one, _ = _same_principal_tokens(monkeypatch)
    await client.post(
        "/api/sessions/register", json={"session_id": "s-blank"}, headers=headers_one
    )
    filled = await client.post(
        "/api/sessions/register",
        json={"session_id": "s-blank", "host": "mac-1", "workspace": "/wt/x"},
        headers=headers_one,
    )
    assert filled.status_code == 200, filled.text
    row = (await _rows(db, "s-blank"))[0]
    assert row["host"] == "mac-1"
    assert row["workspace"] == "/wt/x"


# ---- AC-3: the owner check of #977 is untouched ----


async def test_another_principal_is_still_refused(client: AsyncClient, monkeypatch, db):
    """A foreign principal gets 409 and the holder's row is not read out to it.

    The owner question is asked FIRST: a caller who does not own the row learns
    that the id is taken, not where its work lives.
    """
    headers_p, headers_q = _two_principal_tokens(monkeypatch)
    created = await client.post(
        "/api/sessions/register",
        json={"session_id": "s-owned-1288", "host": "mac-p", "workspace": "/wt/p"},
        headers=headers_p,
    )
    assert created.status_code == 200, created.text
    frozen = await _freeze_last_seen(db, "s-owned-1288")

    stolen = await client.post(
        "/api/sessions/register",
        json={"session_id": "s-owned-1288", "host": "mac-q", "workspace": "/wt/q"},
        headers=headers_q,
    )
    assert stolen.status_code == 409, stolen.text
    assert stolen.json()["detail"]["reason"] == "session_owned_by_other"
    assert "agent-p" not in stolen.text, "the holder must not be named to the loser"
    assert "/wt/p" not in stolen.text, "nor may its address leak to a stranger"

    row = (await _rows(db, "s-owned-1288"))[0]
    assert row["principal_id"] == 31
    assert row["agent"] == "agent-p"
    assert row["host"] == "mac-p"
    assert row["workspace"] == "/wt/p"
    assert row["last_seen_at"] == frozen


# ---- the same question in the write itself, for the register that races ----


async def test_the_repository_write_refuses_a_moved_address(db):
    """A raced UPSERT cannot move the address the service just checked.

    #977 put the owner rule in the WHERE for exactly this reason; the address
    rule belongs there for the same one. Called at the repository level because
    the race it guards cannot be produced through the endpoint.
    """
    from hub import repository as repo

    await repo.upsert_agent_session(
        db,
        session_id="s-raced",
        principal_id=21,
        agent="pda_claude",
        host="mac-1281",
        workspace="/wt/task-1281",
    )
    await db.commit()

    await repo.upsert_agent_session(
        db,
        session_id="s-raced",
        principal_id=21,
        agent="pda_claude",
        host="mac-1283",
        workspace="/wt/task-1283",
    )
    await db.commit()

    rows = await _rows(db, "s-raced")
    assert len(rows) == 1, "no registry row is lost"
    assert rows[0]["host"] == "mac-1281"
    assert rows[0]["workspace"] == "/wt/task-1281"
