"""The owner sees the whole conversation, and can answer in it (#775).

This is the condition the whole feature rests on: a channel between agents is
allowed to exist because a person reads all of it and can step in. While that
was true only through the API, the constraint lived in a sentence. These tests
put it in the pages.
"""

from __future__ import annotations

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub import services
from hub.config import TokenIdentity
from hub.models import MessageSend


def _tokens() -> dict[str, TokenIdentity]:
    return {
        "alpha-token": TokenIdentity("alpha", "agent", principal_id=11),
        "beta-token": TokenIdentity("beta", "agent", principal_id=12),
        "human-token": TokenIdentity("denis", "human", principal_id=1),
    }


def _auth(monkeypatch) -> dict[str, dict[str, str]]:
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return {
        name: {"Authorization": f"Bearer {name}-token"}
        for name in ("alpha", "beta", "human")
    }


async def _two_sessions(db: aiosqlite.Connection) -> None:
    await repo.upsert_agent_session(
        db,
        session_id="s-alpha",
        principal_id=11,
        agent="alpha",
        model="claude-opus-5",
    )
    await repo.upsert_agent_session(
        db, session_id="s-beta", principal_id=12, agent="beta", model="grok-4.6"
    )
    await db.commit()


# ---- AC-1: the task page shows the thread with its provenance ----


async def test_task_page_shows_the_thread_with_provenance(
    client: AsyncClient, monkeypatch, db
):
    auth = _auth(monkeypatch)
    await _two_sessions(db)

    resp = await client.post(
        "/api/tasks", json={"title": "Talked about"}, headers=auth["human"]
    )
    task_id = resp.json()["id"]

    first = await services.send_message(
        db,
        MessageSend(
            to_kind="task",
            to_ref=str(task_id),
            body="база диффа резолвится без фетча, проверь",
            kind="question",
            session_id="s-alpha",
            related_task_id=task_id,
        ),
        agent="alpha",
        principal_id=11,
    )
    await services.send_message(
        db,
        MessageSend(
            to_kind="session",
            to_ref="s-alpha",
            body="проверил, база верная",
            kind="answer",
            session_id="s-beta",
            related_task_id=task_id,
            reply_to=first.message.id,
        ),
        agent="beta",
        principal_id=12,
    )

    page = await client.get(f"/tasks/{task_id}", headers=auth["human"])
    assert page.status_code == 200
    html = page.text
    assert "база диффа резолвится без фетча" in html
    assert "проверил, база верная" in html
    # Provenance: who, which model, which session — all visible, not implied.
    assert "claude-opus-5" in html and "grok-4.6" in html
    assert "alpha" in html and "beta" in html
    assert "сессия s-alpha"[:12] in html or "s-alpha"[:8] in html
    # Two lists, not one: the update feed keeps its own heading.
    assert "Updates &amp; Q&amp;A" in html
    assert "Сообщения сессий" in html


# ---- AC-2: the dashboard lists sessions with presence you can check ----


async def test_dashboard_lists_sessions_with_honest_presence(
    client: AsyncClient, monkeypatch, db
):
    auth = _auth(monkeypatch)
    await _two_sessions(db)
    await db.execute(
        "UPDATE agent_sessions SET last_seen_at = datetime('now', '-2 hours') "
        "WHERE session_id = ?",
        ("s-beta",),
    )
    await db.commit()

    page = await client.get("/", headers=auth["human"])
    assert page.status_code == 200
    html = page.text
    assert "Сессии агентов" in html
    assert "alpha" in html and "beta" in html
    assert "claude-opus-5" in html and "grok-4.6" in html
    assert "session-badge--online" in html
    assert "session-badge--offline" in html
    # The badge never travels alone: the age of the last sign of life is there.
    assert "ч назад" in html or "мин назад" in html


# ---- AC-3: the owner writes into the same channel ----


async def test_human_posts_into_the_same_channel(client: AsyncClient, monkeypatch, db):
    auth = _auth(monkeypatch)
    await _two_sessions(db)

    resp = await client.post(
        "/api/tasks", json={"title": "Owner speaks"}, headers=auth["human"]
    )
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": "beta", "session_id": "s-beta"},
        headers=auth["beta"],
    )

    posted = await client.post(
        f"/tasks/{task_id}/web-message",
        data={"body": "не мержите до релиза", "to_kind": "task", "kind": "note"},
        headers=auth["human"],
        follow_redirects=False,
    )
    assert posted.status_code == 303

    # Same table, same provenance rules — and it lands in the holder's inbox
    # like any agent's message would.
    rows = [dict(r) for r in await repo.list_task_messages(db, task_id)]
    assert [r["body"] for r in rows] == ["не мержите до релиза"]
    assert rows[0]["from_agent"] == "denis"
    assert rows[0]["from_principal_id"] == 1

    beta_inbox = await client.get(
        "/api/messages?session_id=s-beta", headers=auth["beta"]
    )
    assert [m["body"] for m in beta_inbox.json()] == ["не мержите до релиза"]

    # An agent token has no business posting through the human web path.
    refused = await client.post(
        f"/tasks/{task_id}/web-message",
        data={"body": "я тоже человек", "to_kind": "task"},
        headers=auth["alpha"],
        follow_redirects=False,
    )
    assert refused.status_code == 403


# ---- AC-4: no thread is hidden from the owner ----


async def test_no_thread_is_hidden_from_the_owner(client: AsyncClient, monkeypatch, db):
    auth = _auth(monkeypatch)
    await _two_sessions(db)

    # Addressed straight between two sessions, no task anywhere near it.
    await services.send_message(
        db,
        MessageSend(
            to_kind="session",
            to_ref="s-beta",
            body="занял ворктри на этой машине, не запускай второй",
            kind="note",
            session_id="s-alpha",
        ),
        agent="alpha",
        principal_id=11,
    )

    page = await client.get("/", headers=auth["human"])
    assert page.status_code == 200
    assert "занял ворктри на этой машине" in page.text, (
        "a session-to-session thread the owner cannot see is the one thing "
        "this feature is not allowed to have"
    )
    assert "session:s-beta" in page.text


# ---- Bodies are agent-written text, and are rendered as text ----


async def test_message_bodies_are_escaped_not_rendered(
    client: AsyncClient, monkeypatch, db
):
    auth = _auth(monkeypatch)
    await _two_sessions(db)

    resp = await client.post(
        "/api/tasks", json={"title": "Escaping"}, headers=auth["human"]
    )
    task_id = resp.json()["id"]
    await services.send_message(
        db,
        MessageSend(
            to_kind="task",
            to_ref=str(task_id),
            body="<script>alert('xss')</script>",
            session_id="s-alpha",
            related_task_id=task_id,
        ),
        agent="alpha",
        principal_id=11,
    )

    page = await client.get(f"/tasks/{task_id}", headers=auth["human"])
    assert "<script>alert('xss')</script>" not in page.text
    assert "&lt;script&gt;" in page.text

    dashboard = await client.get("/", headers=auth["human"])
    assert "<script>alert('xss')</script>" not in dashboard.text
