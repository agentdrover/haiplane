"""Role mail is read only under the session it was addressed to (#1678).

The four doors besides the inbox that Codex found open on the first cut: the
thread read (one visible message opened the whole thread), the dashboard thread
panel and the task card (an agent token without a session saw the bodies), the
events feed (only agent identities were filtered) and the sender's thread
(a sessionless send was readable by sibling sessions). A human sees everything,
as before: the owner reading the whole channel is what lets it exist (#775).
"""

from __future__ import annotations

import json

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub import services
from hub.config import TokenIdentity
from hub.models import MessageSend

SECRET = "ТЕЛО-РОЛЕВОГО-СООБЩЕНИЯ"


def _tokens() -> dict[str, TokenIdentity]:
    return {
        "alpha-token": TokenIdentity("alpha", "agent", principal_id=11),
        "beta-token": TokenIdentity("beta", "agent", principal_id=12),
        "watch-token": TokenIdentity("watch", "watcher", principal_id=14),
        "nopid-token": TokenIdentity("alpha", "agent"),
        "human-token": TokenIdentity("denis", "human", principal_id=1),
    }


def _auth(monkeypatch) -> dict[str, dict[str, str]]:
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return {
        name: {"Authorization": f"Bearer {name}-token"}
        for name in ("alpha", "beta", "watch", "nopid", "human")
    }


async def _seed(db: aiosqlite.Connection) -> None:
    for slug in ("a", "b"):
        await repo.create_project(db, slug=slug, name=slug)
    for sid, principal, agent, role, projects in (
        ("s1", 11, "alpha", "steward", ["a"]),
        ("s2", 11, "alpha", "steward", ["b"]),
        ("s-beta", 12, "beta", "executor", ["a"]),
        ("s-watch", 14, "watch", "steward", ["a"]),
    ):
        await repo.upsert_agent_session(
            db,
            session_id=sid,
            principal_id=principal,
            agent=agent,
            projects=json.dumps(projects),
            work_role=role,
        )
    await db.commit()


async def _send(db, *, agent, principal, session_id="", **kw):
    body = {"body": SECRET, "session_id": session_id, **kw}
    return (
        await services.send_message(
            db, MessageSend(**body), agent=agent, principal_id=principal
        )
    ).message


async def test_thread_read_filters_each_role_row(client: AsyncClient, monkeypatch, db):
    """P1-1: a reply_to chain cannot pull a foreign role message into a thread
    the caller legitimately sees."""
    auth = _auth(monkeypatch)
    await _seed(db)
    role_msg = await _send(
        db, agent="beta", principal=12, session_id="s-beta",
        to_kind="role", to_ref="steward@a",
    )  # fmt: skip
    # S2 (steward of b) writes to itself, replying to the foreign message.
    own = await _send(
        db, agent="alpha", principal=11, session_id="s2",
        to_kind="session", to_ref="s2", reply_to=role_msg.id,
        body="мой ответ себе",
    )  # fmt: skip
    assert own.thread_id == role_msg.thread_id

    url = f"/api/messages?thread_id={role_msg.thread_id}&session_id="
    s2 = await client.get(url + "s2", headers=auth["alpha"])
    assert s2.status_code == 200, s2.text
    assert [m["id"] for m in s2.json()] == [own.id]
    assert SECRET not in s2.text
    s1 = await client.get(url + "s1", headers=auth["alpha"])
    assert [m["id"] for m in s1.json()] == [role_msg.id, own.id]
    sender = await client.get(url + "s-beta", headers=auth["beta"])
    assert role_msg.id in [m["id"] for m in sender.json()]
    human = await client.get(url[:-12], headers=auth["human"])
    assert [m["id"] for m in human.json()] == [role_msg.id, own.id]


async def test_sessionless_role_send_is_not_shared_with_siblings(
    client: AsyncClient, monkeypatch, db
):
    """P2-2: a role message sent without a session is not the sender's "own"
    thread for sibling sessions of the same token, nor for a request without one."""
    auth = _auth(monkeypatch)
    await _seed(db)
    sent = await _send(
        db, agent="alpha", principal=11, to_kind="role", to_ref="steward@a"
    )
    base = f"/api/messages?thread_id={sent.thread_id}"
    for suffix in ("", "&session_id=s2"):
        resp = await client.get(base + suffix, headers=auth["alpha"])
        assert resp.status_code == 403, (suffix, resp.text)
    matching = await client.get(base + "&session_id=s1", headers=auth["alpha"])
    assert [m["id"] for m in matching.json()] == [sent.id]
    human = await client.get(base, headers=auth["human"])
    assert [m["id"] for m in human.json()] == [sent.id]


async def test_web_panels_hide_role_mail_from_non_humans(
    client: AsyncClient, monkeypatch, db
):
    """P1-2: dashboard thread panel and task card — a token without a verified
    session gets no role bodies; the human sees them as before."""
    auth = _auth(monkeypatch)
    await _seed(db)
    task_id = (
        await client.post("/api/tasks", json={"title": "T"}, headers=auth["human"])
    ).json()["id"]
    await _send(
        db, agent="beta", principal=12, session_id="s-beta", to_kind="role",
        to_ref="steward@a", related_task_id=task_id,
    )  # fmt: skip
    await _send(
        db, agent="beta", principal=12, session_id="s-beta", to_kind="task",
        to_ref=str(task_id), related_task_id=task_id, body="обычное сообщение",
    )  # fmt: skip

    for path in ("/", f"/tasks/{task_id}"):
        agent_page = await client.get(path, headers=auth["alpha"])
        assert agent_page.status_code == 200, (path, agent_page.status_code)
        assert SECRET not in agent_page.text, path
        human_page = await client.get(path, headers=auth["human"])
        assert SECRET in human_page.text, path
    card = await client.get(f"/tasks/{task_id}", headers=auth["alpha"])
    assert "обычное сообщение" in card.text, "non-role talk stays as it was"


async def test_events_feed_judges_role_mail_for_every_non_human(
    client: AsyncClient, monkeypatch, db
):
    """P2-1: a watcher principal gets role events only from its own matching
    session; a foreign session id is refused; the human sees them all."""
    auth = _auth(monkeypatch)
    await _seed(db)
    await _send(
        db, agent="beta", principal=12, session_id="s-beta", to_kind="role",
        to_ref="steward@a",
    )  # fmt: skip
    await _send(
        db, agent="beta", principal=12, session_id="s-beta", to_kind="project",
        to_ref="a", body="канал проекта",
    )  # fmt: skip

    def feed(headers, suffix=""):
        return client.get(f"/api/events?kinds=message_posted{suffix}", headers=headers)

    def kinds(resp):
        return [e["payload"]["to_kind"] for e in resp.json()["events"]]

    own = await feed(auth["watch"], "&session_id=s-watch")
    assert kinds(own) == ["role", "project"]
    bare = await feed(auth["watch"])
    assert bare.status_code == 200 and "role" not in kinds(bare)
    assert kinds(bare) == ["project"], "other addressees are unchanged"
    foreign = await feed(auth["watch"], "&session_id=s1")
    assert foreign.status_code in (403, 404), foreign.text
    mismatch = await feed(auth["alpha"], "&session_id=s2")
    assert "role" not in kinds(mismatch)
    human = await feed(auth["human"])
    assert kinds(human) == ["role", "project"]


async def test_role_mail_needs_proven_session_ownership(
    client: AsyncClient, monkeypatch, db
):
    """P1 (delta): a caller without a principal id matches a session by agent
    NAME elsewhere; for role mail that is not ownership. Two principals sharing
    one agent name must not read each other's role mail on any surface."""
    auth = _auth(monkeypatch)
    await _seed(db)
    # alpha under ANOTHER principal (21) holds a matching steward session.
    await repo.upsert_agent_session(
        db,
        session_id="s-other",
        principal_id=21,
        agent="alpha",
        projects=json.dumps(["a"]),
        work_role="steward",
    )
    await db.commit()
    role_msg = await _send(
        db, agent="beta", principal=12, session_id="s-beta",
        to_kind="role", to_ref="steward@a",
    )  # fmt: skip
    nopid = auth["nopid"]

    inbox = await client.get("/api/messages?session_id=s-other", headers=nopid)
    assert inbox.status_code == 200, inbox.text
    assert SECRET not in inbox.text
    thread = await client.get(
        f"/api/messages?thread_id={role_msg.thread_id}&session_id=s-other",
        headers=nopid,
    )
    assert thread.status_code == 403, thread.text
    # A visible non-role reply opens the thread, but not the role row inside it.
    reply = await _send(
        db, agent="beta", principal=12, session_id="s-beta", to_kind="project",
        to_ref="a", reply_to=role_msg.id, body="канал проекта",
    )  # fmt: skip
    opened = await client.get(
        f"/api/messages?thread_id={role_msg.thread_id}&session_id=s-other",
        headers=nopid,
    )
    assert [m["id"] for m in opened.json()] == [reply.id]
    # Sending from that session is not proof of ownership either.
    sent_by_other = await _send(
        db, agent="alpha", principal=21, session_id="s-other",
        to_kind="role", to_ref="steward@b",
    )  # fmt: skip
    feed = await client.get(
        "/api/events?kinds=message_posted&session_id=s-other", headers=nopid
    )
    assert feed.status_code == 200
    assert [e["payload"]["to_kind"] for e in feed.json()["events"]] == []
    assert sent_by_other.id not in [
        e["payload"]["message_id"] for e in feed.json()["events"]
    ]

    # The real owner (principal 21) still reads it.
    monkeypatch.setattr(
        __import__("hub.config", fromlist=["HUB_TOKENS"]),
        "HUB_TOKENS",
        {
            **_tokens(),
            "owner21-token": TokenIdentity("alpha", "agent", principal_id=21),
        },
    )
    owner = {"Authorization": "Bearer owner21-token"}
    mine = await client.get("/api/messages?session_id=s-other", headers=owner)
    assert [m["id"] for m in mine.json()] == [role_msg.id, reply.id]


async def test_thread_limit_counts_only_visible_rows(
    client: AsyncClient, monkeypatch, db
):
    """P2 (delta): a hidden role message first and a visible reply second must
    not turn limit=1 into an empty page; the human's full thread is unchanged."""
    auth = _auth(monkeypatch)
    await _seed(db)
    hidden = await _send(
        db, agent="beta", principal=12, session_id="s-beta",
        to_kind="role", to_ref="steward@a",
    )  # fmt: skip
    own = await _send(
        db, agent="alpha", principal=11, session_id="s2",
        to_kind="session", to_ref="s2", reply_to=hidden.id, body="мой ответ",
    )  # fmt: skip
    url = f"/api/messages?thread_id={hidden.thread_id}"
    page = await client.get(url + "&session_id=s2&limit=1", headers=auth["alpha"])
    assert [m["id"] for m in page.json()] == [own.id]
    human = await client.get(url + "&limit=2", headers=auth["human"])
    assert [m["id"] for m in human.json()] == [hidden.id, own.id]
    capped = await client.get(url + "&limit=1", headers=auth["human"])
    assert [m["id"] for m in capped.json()] == [hidden.id]
