"""The session becomes an address, and an honest one (#771, feature #770).

Until this registry a session existed only as ``tasks.claim_session_id``: a
string nobody could list, find or ask whether it was still alive. These tests
hold the two properties that make the registry worth having — identity comes
from the token, and presence is derived from the last sign of life — plus the
one that makes it safe to ship: the existing lifecycle never depends on it.
"""

from __future__ import annotations

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub import services
from hub.config import TokenIdentity
from hub.models import TaskClaim, TaskCreate, TaskRelease, TaskReviewVerdict


def _tokens() -> dict[str, TokenIdentity]:
    return {
        "agent-token": TokenIdentity("bot", "agent", principal_id=7),
        "human-token": TokenIdentity("denis", "human"),
    }


def _auth(monkeypatch) -> dict[str, str]:
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return {"Authorization": "Bearer agent-token"}


async def _rows(db: aiosqlite.Connection, session_id: str) -> list[dict]:
    return [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM agent_sessions WHERE session_id = ?", (session_id,)
        )
    ]


# ---- AC-1: registration is idempotent and names come from the token ----


async def test_register_is_idempotent_and_identity_comes_from_the_token(
    client: AsyncClient, monkeypatch, db
):
    agent = _auth(monkeypatch)

    resp = await client.post(
        "/api/sessions/register",
        json={
            "session_id": "s-1",
            "model": "claude-opus-5",
            "host": "mac-1",
            # A session naming itself is exactly what must not work: the body
            # says someone else, the token says bot.
            "agent": "someone-else",
            "principal_id": 999,
        },
        headers=agent,
    )
    assert resp.status_code == 200, resp.text
    first = resp.json()
    assert first["agent"] == "bot"
    assert first["principal_id"] == 7
    assert first["online"] is True

    resp = await client.post(
        "/api/sessions/register",
        # Second hello from the same session: a new model, no host this time.
        json={"session_id": "s-1", "model": "claude-fable-5"},
        headers=agent,
    )
    assert resp.status_code == 200, resp.text
    second = resp.json()

    rows = await _rows(db, "s-1")
    assert len(rows) == 1, "saying hello twice does not create a second session"
    assert second["started_at"] == first["started_at"], "the session did not restart"
    assert second["model"] == "claude-fable-5"
    assert second["host"] == "mac-1", "an omitted field must not erase what is known"


# ---- AC-2: a session past its TTL is offline, and says how stale it is ----


async def test_stale_session_is_offline_not_silently_ok(
    client: AsyncClient, monkeypatch, db
):
    agent = _auth(monkeypatch)
    await client.post(
        "/api/sessions/register", json={"session_id": "s-2"}, headers=agent
    )

    await db.execute(
        "UPDATE agent_sessions SET last_seen_at = datetime('now', '-90 minutes') "
        "WHERE session_id = ?",
        ("s-2",),
    )
    await db.commit()

    resp = await client.get("/api/sessions", headers=agent)
    assert resp.status_code == 200, resp.text
    stale = next(s for s in resp.json() if s["session_id"] == "s-2")
    assert stale["online"] is False
    assert stale["status"] == "offline"
    assert stale["last_seen_age_seconds"] >= 90 * 60, (
        "offline is not enough: the reader must see how old the evidence is"
    )
    offline_only = await client.get("/api/sessions?status=offline", headers=agent)
    assert [s["session_id"] for s in offline_only.json()] == ["s-2"]

    resp = await client.post("/api/sessions/s-2/heartbeat", headers=agent)
    assert resp.status_code == 200, resp.text
    assert resp.json()["online"] is True, "a heartbeat revives without re-registration"
    assert resp.json()["last_seen_age_seconds"] < 60

    unknown = await client.post(
        "/api/sessions/never-said-hello/heartbeat", headers=agent
    )
    assert unknown.status_code == 404
    assert "register" in unknown.text


# ---- AC-3: claim and release move the current task inside the same write ----


async def test_claim_and_release_move_the_current_task(
    client: AsyncClient, monkeypatch, db
):
    agent = _auth(monkeypatch)
    await client.post(
        "/api/sessions/register", json={"session_id": "s-3"}, headers=agent
    )
    await db.execute(
        "UPDATE agent_sessions SET last_seen_at = datetime('now', '-30 minutes') "
        "WHERE session_id = ?",
        ("s-3",),
    )
    await db.commit()

    resp = await client.post(
        "/api/tasks",
        json={"title": "Session follows the claim"},
        headers={"Authorization": "Bearer human-token"},
    )
    task_id = resp.json()["id"]

    stale_before_claim = (await _rows(db, "s-3"))[0]["last_seen_at"]

    resp = await client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": "bot", "session_id": "s-3"},
        headers=agent,
    )
    assert resp.status_code == 200, resp.text
    row = (await _rows(db, "s-3"))[0]
    assert row["current_task_id"] == task_id
    assert row["last_seen_at"] > stale_before_claim, "claiming is a sign of life"

    task = dict(await repo.get_task(db, task_id))
    assert task["claim_session_id"] == "s-3", "registry and task agree on the holder"

    await db.execute(
        "UPDATE agent_sessions SET last_seen_at = datetime('now', '-30 minutes') "
        "WHERE session_id = ?",
        ("s-3",),
    )
    await db.commit()
    stale_before_release = (await _rows(db, "s-3"))[0]["last_seen_at"]

    resp = await client.post(
        f"/api/tasks/{task_id}/release",
        json={"agent": "bot", "session_id": "s-3"},
        headers=agent,
    )
    assert resp.status_code == 200, resp.text
    row = (await _rows(db, "s-3"))[0]
    assert row["current_task_id"] is None
    assert row["last_seen_at"] > stale_before_release, "so is releasing"


# ---- AC-4: retention drops only what has gone quiet ----


async def test_pruning_drops_only_expired_sessions(db: aiosqlite.Connection):
    for session_id in ("fresh", "long-gone"):
        await repo.upsert_agent_session(
            db, session_id=session_id, principal_id=1, agent="bot"
        )
    await db.execute(
        "UPDATE agent_sessions SET last_seen_at = datetime('now', '-30 days') "
        "WHERE session_id = ?",
        ("long-gone",),
    )
    await db.commit()

    removed = await repo.prune_agent_sessions(db, keep_days=14)
    await db.commit()

    assert removed == 1
    left = [
        dict(r)["session_id"]
        for r in await db.execute_fetchall("SELECT session_id FROM agent_sessions")
    ]
    assert left == ["fresh"]


# ---- AC-5: the lifecycle never depends on the registry ----


async def test_registry_is_optional_for_the_existing_lifecycle(
    db: aiosqlite.Connection,
):
    """The whole pair path runs with an empty registry, exactly as before.

    A new record that becomes a precondition for claiming a task would be a
    worse failure than the gap it closes: coordination is an addition, not a
    toll booth.
    """
    tv = await services.create_task(db, TaskCreate(title="No registry here"))

    await services.claim_task(db, tv.id, TaskClaim(agent="dev", session_id="ghost"))
    await services.release_task(db, tv.id, TaskRelease(agent="dev", session_id="ghost"))
    await services.claim_task(db, tv.id, TaskClaim(agent="dev", session_id="ghost"))

    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: build")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")
    await services.submit_for_review(db, tv.id)
    await services.record_review_verdict(
        db, tv.id, TaskReviewVerdict(verdict="approved", agent="reviewer")
    )

    task = dict(await repo.get_task(db, tv.id))
    assert task["status"] == "running", "APPROVED returns the task to running"
    assert task["claim_session_id"] == "ghost"

    rows = list(await db.execute_fetchall("SELECT * FROM agent_sessions"))
    assert rows == [], "no session ever registered, and nothing needed one"
