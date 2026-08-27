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


# ---- #852: the claim carries a session, or it is not a claim ----
#
# The hole these tests close: claim_session_id was optional, so a task could
# run held by an agent NAME. A name is not an executor — pda_claude ran four
# sessions on the day this was found — and every address the hub grew (the
# registry above, messages #773, wake-up #774) routes by session. #842 was
# running, claimed_by=pda_claude, claim_session_id=NULL: nobody to ask.


async def test_claim_without_session_id_is_refused(
    client: AsyncClient, monkeypatch, db
):
    """An agent must say WHICH session takes the task (AC-1)."""
    agent = _auth(monkeypatch)
    task_id = (await services.create_task(db, TaskCreate(title="Needs an address"))).id

    resp = await client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": "bot", "session_id": ""},
        headers=agent,
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "claim_without_session"
    # The refusal has to carry the way out, not just the verdict.
    assert "session_id" in detail["hint"]
    assert "hub_session_register" in detail["hint"]

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "open", "a refused claim leaves the task open"
    assert not task["claim_session_id"]

    # With a session the very same call goes through.
    ok = await client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": "bot", "session_id": "s-852"},
        headers=agent,
    )
    assert ok.status_code == 200, ok.text
    assert dict(await repo.get_task(db, task_id))["claim_session_id"] == "s-852"


async def test_pair_start_refuses_other_session_of_same_agent(
    client: AsyncClient, monkeypatch, db
):
    """The name check passes for both sessions; the session check does not (AC-2)."""
    agent = _auth(monkeypatch)
    task_id = (
        await services.create_task(db, TaskCreate(title="One holder at a time"))
    ).id
    await client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": "bot", "session_id": "session-A"},
        headers=agent,
    )
    await repo.add_task_update(db, task_id, "bot", "status", "Plan: work")
    await db.commit()

    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        # Same agent name — this is what used to be enough.
        json={"assigned_agent": "bot", "session_id": "session-B"},
        headers=agent,
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "pair_start_session_mismatch"
    assert detail["claim_session_id"] == "session-A"
    assert detail["caller_session_id"] == "session-B"

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "claimed", "the loser does not move the task"

    ok = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"assigned_agent": "bot", "session_id": "session-A"},
        headers=agent,
    )
    assert ok.status_code == 200, ok.text
    assert dict(await repo.get_task(db, task_id))["status"] == "running"


async def test_pair_start_from_open_records_the_session(
    client: AsyncClient, monkeypatch, db
):
    """Pair-start skips the claim, so it must write the address itself (AC-2)."""
    agent = _auth(monkeypatch)
    task_id = (
        await services.create_task(db, TaskCreate(title="Straight to running"))
    ).id

    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"assigned_agent": "bot", "plan": "Plan: go", "session_id": "s-open"},
        headers=agent,
    )
    assert resp.status_code == 200, resp.text
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running"
    assert task["claim_session_id"] == "s-open", "running work has an address"

    # And without a session an agent cannot start one at all.
    other_id = (
        await services.create_task(db, TaskCreate(title="No address, no start"))
    ).id
    refused = await client.post(
        f"/api/tasks/{other_id}/pair-start",
        json={"assigned_agent": "bot", "plan": "Plan: go"},
        headers=agent,
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["reason"] == "claim_without_session"
    assert dict(await repo.get_task(db, other_id))["status"] == "open"


async def test_running_without_session_is_visible(client: AsyncClient, monkeypatch, db):
    """The tail that predates the contract has to be findable (AC-3)."""
    agent = _auth(monkeypatch)
    addressed = await services.create_task(db, TaskCreate(title="Has a session"))
    orphan = await services.create_task(db, TaskCreate(title="Nobody to ask"))
    headless = await services.create_task(db, TaskCreate(title="Dispatched"))
    await repo.update_task(
        db,
        addressed.id,
        status="running",
        claimed_by="bot",
        claim_session_id="s-live",
    )
    # Exactly the shape #842 was found in: claimed by a name, no session.
    await repo.update_task(
        db, orphan.id, status="running", claimed_by="bot", claim_session_id=None
    )
    # Headless work is not unaddressable — its executor is the job, not a
    # session — so it must NOT show up here.
    await repo.update_task(
        db, headless.id, status="running", claimed_by="bot", job_id="job-1"
    )
    await db.commit()

    resp = await client.get("/api/sessions/unaddressable", headers=agent)
    assert resp.status_code == 200, resp.text
    ids = [row["id"] for row in resp.json()]
    assert ids == [orphan.id], resp.json()
    assert resp.json()[0]["claimed_by"] == "bot"


# ---- #977: a leaked token cannot steal another principal's session_id ----
#
# Register upserts principal_id on conflict and heartbeat has no owner check.
# Harmless for trusted laptop tokens; fatal once a transcript token can call
# these routes. Identity already comes from the token (#771); this is the
# missing half: the row itself is not a public address to overwrite.


def _two_agent_tokens(monkeypatch) -> tuple[dict[str, str], dict[str, str]]:
    from hub import config

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "token-a": TokenIdentity("agent-a", "agent", principal_id=11),
            "token-b": TokenIdentity("agent-b", "agent", principal_id=12),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return (
        {"Authorization": "Bearer token-a"},
        {"Authorization": "Bearer token-b"},
    )


async def _freeze_last_seen(db: aiosqlite.Connection, session_id: str) -> str:
    """Pin last_seen_at so a refused write is observable, not lost in the same second."""
    await db.execute(
        "UPDATE agent_sessions SET last_seen_at = datetime('now', '-10 minutes') "
        "WHERE session_id = ?",
        (session_id,),
    )
    await db.commit()
    return (await _rows(db, session_id))[0]["last_seen_at"]


async def test_register_refuses_foreign_session_id(
    client: AsyncClient, monkeypatch, db
):
    """AC-1: principal B cannot take a session_id already registered to A."""
    headers_a, headers_b = _two_agent_tokens(monkeypatch)
    created = await client.post(
        "/api/sessions/register",
        json={"session_id": "s-owned", "model": "opus", "host": "mac-a"},
        headers=headers_a,
    )
    assert created.status_code == 200, created.text
    frozen = await _freeze_last_seen(db, "s-owned")
    before = (await _rows(db, "s-owned"))[0]

    stolen = await client.post(
        "/api/sessions/register",
        json={"session_id": "s-owned", "model": "hijack", "host": "mac-b"},
        headers=headers_b,
    )
    assert stolen.status_code == 409, stolen.text
    detail = stolen.json()["detail"]
    assert detail["reason"] == "session_owned_by_other"
    assert "agent-a" not in stolen.text, "the holder must not be named to the loser"

    after = (await _rows(db, "s-owned"))[0]
    assert after["principal_id"] == 11
    assert after["agent"] == "agent-a"
    assert after["model"] == "opus"
    assert after["host"] == "mac-a"
    assert after["last_seen_at"] == frozen
    assert after["principal_id"] == before["principal_id"]
    assert after["agent"] == before["agent"]


async def test_heartbeat_of_foreign_session_is_not_found(
    client: AsyncClient, monkeypatch, db
):
    """AC-2: B's heartbeat does not bump A's last_seen_at and looks unregistered."""
    headers_a, headers_b = _two_agent_tokens(monkeypatch)
    await client.post(
        "/api/sessions/register", json={"session_id": "s-owned"}, headers=headers_a
    )
    frozen = await _freeze_last_seen(db, "s-owned")

    resp = await client.post("/api/sessions/s-owned/heartbeat", headers=headers_b)
    assert resp.status_code == 404, resp.text
    assert (await _rows(db, "s-owned"))[0]["last_seen_at"] == frozen
    assert (await _rows(db, "s-owned"))[0]["principal_id"] == 11


async def test_same_principal_register_refreshes_without_changing_owner(
    client: AsyncClient, monkeypatch, db
):
    """AC-3: the owner can say hello again; principal_id stays put."""
    headers_a, _headers_b = _two_agent_tokens(monkeypatch)
    first = await client.post(
        "/api/sessions/register", json={"session_id": "s-owned"}, headers=headers_a
    )
    assert first.status_code == 200, first.text
    frozen = await _freeze_last_seen(db, "s-owned")

    again = await client.post(
        "/api/sessions/register",
        json={"session_id": "s-owned", "model": "fable"},
        headers=headers_a,
    )
    assert again.status_code == 200, again.text
    row = (await _rows(db, "s-owned"))[0]
    assert again.json()["principal_id"] == 11
    assert row["principal_id"] == 11
    assert row["agent"] == "agent-a"
    assert row["model"] == "fable"
    assert row["last_seen_at"] > frozen
