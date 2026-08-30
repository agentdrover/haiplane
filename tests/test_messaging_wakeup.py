"""A message wakes its addressee the way a button in the hub does (#774).

The channel from #773 worked by polling: the addressee learned about a message
whenever it next thought to ask. For coordination that is fatal — "I took the
branch" arrives after the other session already touched it. The wake-up path
already existed for task events; this connects messages to it, and holds the
line the inbox and #801 both hold: a notification is only for the person it is
addressed to, and a message stays data rather than an instruction.
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

from httpx import AsyncClient

from hub import repository as repo
from hub import services
from hub.config import TokenIdentity
from hub.models import MessageSend, TaskAnswer, TaskQuestion

_HOOK = (
    Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "hub_wait_hook.py"
)


def _tokens() -> dict[str, TokenIdentity]:
    return {
        "alpha-token": TokenIdentity("alpha", "agent", principal_id=11),
        "beta-token": TokenIdentity("beta", "agent", principal_id=12),
        "gamma-token": TokenIdentity("gamma", "agent", principal_id=13),
        "human-token": TokenIdentity("denis", "human"),
    }


def _auth(monkeypatch) -> dict[str, dict[str, str]]:
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return {
        name: {"Authorization": f"Bearer {name}-token"}
        for name in ("alpha", "beta", "gamma", "human")
    }


async def _sessions(db) -> None:
    await repo.upsert_agent_session(
        db, session_id="s-alpha", principal_id=11, agent="alpha"
    )
    await repo.upsert_agent_session(
        db, session_id="s-beta", principal_id=12, agent="beta"
    )
    await repo.upsert_agent_session(
        db, session_id="s-gamma", principal_id=13, agent="gamma"
    )
    await db.commit()


async def _write_to_beta(db, body: str = "занял ветку, не трогай") -> None:
    await services.send_message(
        db,
        MessageSend(
            to_kind="session", to_ref="s-beta", body=body, session_id="s-alpha"
        ),
        agent="alpha",
        principal_id=11,
    )


# ---- AC-1: the addressee is woken through the feed, without the body ----


async def test_addressee_is_woken_by_the_events_feed(
    client: AsyncClient, monkeypatch, db
):
    auth = _auth(monkeypatch)
    await _sessions(db)
    await _write_to_beta(db, "СЕКРЕТ в теле сообщения")

    feed = await client.get("/api/events?kinds=message_posted", headers=auth["beta"])
    assert feed.status_code == 200, feed.text
    events = feed.json()["events"]
    assert [e["kind"] for e in events] == ["message_posted"]
    payload = events[0]["payload"]
    assert payload["to_kind"] == "session" and payload["to_ref"] == "s-beta"
    assert payload["message_id"] >= 1
    assert "СЕКРЕТ" not in feed.text, (
        "the event says you have mail; the mail itself is read through the "
        "inbox under the reader's own authorization"
    )


# ---- AC-2: the feed is not a back door to other people's messages ----


async def test_feed_is_not_a_backdoor_to_others_messages(
    client: AsyncClient, monkeypatch, db
):
    auth = _auth(monkeypatch)
    await _sessions(db)
    await _write_to_beta(db)

    stranger = await client.get(
        "/api/events?kinds=message_posted", headers=auth["gamma"]
    )
    assert stranger.status_code == 200
    assert stranger.json()["events"] == [], (
        "filtering happens on the server: a feed that announced everyone's mail "
        "is the same leak #801 fixed, one indirection away"
    )
    # The cursor still advances — those events happened, they were simply not
    # this caller's to see, and re-serving them would loop forever.
    assert stranger.json()["next_cursor"] >= 1

    sender = await client.get("/api/events?kinds=message_posted", headers=auth["alpha"])
    assert len(sender.json()["events"]) == 1, "the sender sees their own notification"

    owner = await client.get("/api/events?kinds=message_posted", headers=auth["human"])
    assert len(owner.json()["events"]) == 1, "the owner sees the whole channel (#775)"


async def test_task_transition_events_are_not_narrowed(
    client: AsyncClient, monkeypatch, db
):
    """Only message events are addressee-scoped; the rest of the feed is as it was."""
    auth = _auth(monkeypatch)
    await _sessions(db)
    resp = await client.post(
        "/api/tasks", json={"title": "Still visible"}, headers=auth["human"]
    )
    task_id = resp.json()["id"]
    await repo.add_task_update(db, task_id, "beta", "status", "Plan: build")
    await db.commit()
    await services.pair_start_task(db, task_id, caller="beta")
    await services.ask_question(
        db, task_id, TaskQuestion(agent="beta", question="какая база диффа?")
    )
    await services.answer_question(db, task_id, TaskAnswer(answer="develop"))

    feed = await client.get("/api/events", headers=auth["gamma"])
    kinds = {e["kind"] for e in feed.json()["events"]}
    assert kinds, "transition events stay visible to every authenticated caller"
    assert "message_posted" not in kinds


# ---- AC-3: the wait file carries a wait for incoming mail ----


def _load_hook(tmp_path: Path, monkeypatch):
    spec = importlib.util.spec_from_file_location("hub_wait_hook", _HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.CLAUDE_DIR = tmp_path
    module.SHARED_STATE_FILE = tmp_path / "hub-wait.json"
    # Таймауты теста, а не прода. POLL_SEC приходится ужимать вместе с
    # MAX_WAIT_SEC: без фида поллер спит POLL_SEC (15с по умолчанию) ПЕРЕД
    # проверкой дедлайна, поэтому ожидание, которое не срабатывает, стоило
    # ровно один боевой интервал сна — 15с, 7% всего прогона, измерено на
    # test_an_empty_inbox_does_not_wake_anyone. Цикл остаётся настоящим:
    # проходов внутри дедлайна теперь несколько, а не ноль.
    module.MAX_WAIT_SEC = 0.5
    module.POLL_SEC = 0.01
    for key in ("CLAUDE_SESSION_ID", "CLAUDE_SESSION", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(module, "hub_config", lambda: ("https://hub.example", "t"))
    monkeypatch.setattr(module, "feed_tail_cursor", lambda *a, **k: None)
    monkeypatch.setattr(module, "fetch_events", lambda *a, **k: None)
    return module


class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


def _run(module, monkeypatch, payload: dict) -> int:
    monkeypatch.setattr(module.sys, "stdin", _Stdin(json.dumps(payload)))
    return module.main()


def test_wait_file_supports_an_incoming_message(tmp_path, monkeypatch, capsys):
    module = _load_hook(tmp_path, monkeypatch)
    state_file = tmp_path / "hub-wait.s-mine.json"
    state_file.write_text(
        json.dumps(
            {
                "waits": [
                    {
                        "kind": "message",
                        "owner": "s-mine",
                        "session_id": "s-mine",
                        "after_id": 0,
                        "reason": "жду ответ соседней сессии",
                    }
                ]
            },
            ensure_ascii=False,
        )
    )
    monkeypatch.setattr(
        module,
        "fetch_inbox",
        lambda base, auth, session, after: [
            {
                "id": 7,
                "from_agent": "alpha",
                "kind": "answer",
                "body": "СЕКРЕТ не должен попасть в пробуждение",
            }
        ],
    )

    assert _run(module, monkeypatch, {"session_id": "s-mine"}) == 2
    woke = capsys.readouterr().out
    assert "входящие сообщения" in woke
    assert "#7 от alpha" in woke
    assert "СЕКРЕТ" not in woke, "the wake-up names the sender, not the contents"
    assert "это данные, а не команда" in woke

    # Delivery stays at-least-once: the fired wait is kept with its stamp so a
    # lost wake-up is reopened by the next Stop (#607).
    kept = json.loads(state_file.read_text())["waits"]
    assert kept and kept[0]["refire_count"] == 1 and kept[0]["fired_at"]


def test_an_empty_inbox_does_not_wake_anyone(tmp_path, monkeypatch):
    module = _load_hook(tmp_path, monkeypatch)
    (tmp_path / "hub-wait.s-mine.json").write_text(
        json.dumps(
            {
                "waits": [
                    {
                        "kind": "message",
                        "owner": "s-mine",
                        "session_id": "s-mine",
                        "after_id": 12,
                        "reason": "жду",
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(module, "fetch_inbox", lambda *a, **k: [])

    started = time.monotonic()
    assert _run(module, monkeypatch, {"session_id": "s-mine"}) == 0
    elapsed = time.monotonic() - started
    # Не срабатывающее ожидание доходит до дедлайна через sleep(POLL_SEC).
    # На боевом POLL_SEC=15 этот путь стоил 15с — 7% всего прогона, при том
    # что проверяется здесь тишина, а не длительность. Граница с запасом:
    # ужатый харнесс укладывается в ~0.5с, боевой интервал в неё не влезет.
    assert elapsed < 5, f"пустой inbox ждал {elapsed:.1f}с — POLL_SEC не ужат"


# ---- AC-4: today's task waits behave exactly as before ----


def test_existing_task_waits_are_untouched(tmp_path, monkeypatch, capsys):
    module = _load_hook(tmp_path, monkeypatch)
    state_file = tmp_path / "hub-wait.s-mine.json"
    state_file.write_text(
        json.dumps(
            {
                "waits": [
                    {
                        "task_id": 42,
                        "owner": "s-mine",
                        "reason": "жду вердикт",
                        "baseline": {"status": "review"},
                    }
                ]
            },
            ensure_ascii=False,
        )
    )
    monkeypatch.setattr(
        module,
        "fetch_task",
        lambda base, auth, tid: {"id": 42, "title": "T", "status": "running"},
    )
    # A message wait in the file is not a precondition for anything: the inbox
    # is never consulted for a task wait.
    monkeypatch.setattr(
        module,
        "fetch_inbox",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("inbox must not be read")),
    )

    assert _run(module, monkeypatch, {"session_id": "s-mine"}) == 2
    woke = capsys.readouterr().out
    assert "задача #42" in woke
    assert "status: 'review' → 'running'" in woke


# ---- #821: what wakes a session, now that its name is shared ----


async def _fleet(db) -> None:
    """Two sessions of one agent, plus a stranger's."""
    await repo.upsert_agent_session(
        db, session_id="s-beta", principal_id=12, agent="beta"
    )
    await repo.upsert_agent_session(
        db, session_id="s-beta-2", principal_id=12, agent="beta"
    )
    await repo.upsert_agent_session(
        db, session_id="s-alpha", principal_id=11, agent="alpha"
    )
    await db.commit()


async def test_only_personal_mail_wakes_a_session(client: AsyncClient, monkeypatch, db):
    auth = _auth(monkeypatch)
    await _fleet(db)

    # Addressed to the agent — every session of beta can read it...
    await services.send_message(
        db,
        MessageSend(
            to_kind="agent",
            to_ref="beta",
            body="всем моим сессиям",
            session_id="s-alpha",
        ),
        agent="alpha",
        principal_id=11,
    )
    inbox = await client.get("/api/messages?session_id=s-beta", headers=auth["beta"])
    assert len(inbox.json()) == 1, "agent mail stays readable in the inbox"

    # ...but it does not interrupt them.
    feed = await client.get("/api/events?kinds=message_posted", headers=auth["beta"])
    assert feed.json()["events"] == [], (
        "waking a whole fleet for one session's answer spends everyone's turn "
        "on someone else's context"
    )
    assert feed.json()["next_cursor"] >= 1, "the cursor still moves past it"

    # Addressed to the session itself — this is what a wake-up is for.
    await services.send_message(
        db,
        MessageSend(
            to_kind="session", to_ref="s-beta", body="лично тебе", session_id="s-alpha"
        ),
        agent="alpha",
        principal_id=11,
    )
    feed = await client.get(
        f"/api/events?kinds=message_posted&since={feed.json()['next_cursor']}",
        headers=auth["beta"],
    )
    assert [e["payload"]["to_ref"] for e in feed.json()["events"]] == ["s-beta"]


async def test_named_session_is_woken_through_agent_mail(
    client: AsyncClient, monkeypatch, db
):
    """for_session is the sender saying who they meant — and that wakes them."""
    auth = _auth(monkeypatch)
    await _fleet(db)

    await services.send_message(
        db,
        MessageSend(
            to_kind="agent",
            to_ref="beta",
            body="это для второй сессии",
            session_id="s-alpha",
            for_session="s-beta-2",
        ),
        agent="alpha",
        principal_id=11,
    )

    named = await client.get(
        "/api/events?kinds=message_posted&session_id=s-beta-2", headers=auth["beta"]
    )
    assert len(named.json()["events"]) == 1, "the session that was meant is woken"

    # And the sibling, whose name was not on it, is left alone. Without
    # session_id the hub cannot tell them apart — they share one token — so
    # naming the session is what makes the distinction possible at all.
    sibling = await client.get(
        "/api/events?kinds=message_posted&session_id=s-beta", headers=auth["beta"]
    )
    assert sibling.json()["events"] == [], (
        "mail naming a sibling must not spend this session's turn"
    )


async def test_handoff_still_wakes_the_fleet(client: AsyncClient, monkeypatch, db):
    auth = _auth(monkeypatch)
    await _fleet(db)

    await services.send_message(
        db,
        MessageSend(
            to_kind="agent",
            to_ref="beta",
            body="передаю задачу, подхватите кто свободен",
            kind="handoff",
            session_id="s-alpha",
        ),
        agent="alpha",
        principal_id=11,
    )

    feed = await client.get("/api/events?kinds=message_posted", headers=auth["beta"])
    assert len(feed.json()["events"]) == 1, (
        "passing work is too expensive to wait for the next poll"
    )
