"""Messages between agent sessions (#773, feature #770): the channel itself.

The registry (#771) gave a session an address; this module lets one write to
another. Three properties decide everything below, and each of them is a
mechanism rather than a promise:

1. **A message is data, never a command.** Nothing in this module calls a
   lifecycle transition — no approve, no verdict, no done. A message saying
   "approve #42" is a string that a reader may act on under its own identity
   and its own gates, exactly like any other text an agent reads. If the
   channel could move a gate, every human gate in the hub would have a second
   door with no lock on it.

2. **The sender is the token.** Agent, principal and session come from the
   authenticated identity and the registry, never from the request body. A
   sender that could name itself is a forged provenance, and provenance is the
   only thing that makes a message worth trusting at all.

3. **Silence is not delivery.** Writing to a session that has been quiet past
   its TTL answers "stored, not delivered now, last seen N seconds ago"
   instead of a bare success — and writing to an address nobody registered is
   refused. "Delivered into the void" is worse than no channel: the sender
   walks away believing the thing was said.

Reading follows the same shape: the inbox query is bounded by the caller's own
addressing in SQL, so no filter can widen it into someone else's mail.
"""

from __future__ import annotations

from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import config
from hub import repository as repo
from hub.mcp_envelope import enrich_error_payload
from hub.models import MessageDelivery, MessageSend, MessageSendResult, MessageView
from hub.services.sessions import session_view

# What a message can be addressed to. Four forms, and no fifth: a broadcast to
# everything would be a channel nobody owns and everybody mutes.
ADDRESS_KINDS = frozenset({"session", "agent", "task", "project"})

# What a message can claim to be. The set is small on purpose — these are
# coordination moves, not a general chat vocabulary, and a reader can tell at a
# glance whether something needs an answer.
MESSAGE_KINDS = frozenset({"note", "question", "answer", "handoff", "claim_request"})


def _refuse(status: int, reason: str, message: str, hint: str) -> HTTPException:
    return HTTPException(
        status,
        detail=enrich_error_payload(
            {
                "reason": reason,
                "actor_hint": "agent",
                "message": message,
                "hint": hint,
            }
        ),
    )


def message_view(row: aiosqlite.Row | dict, *, matched_by: str = "") -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "thread_id": data.get("thread_id") or "",
        "from_principal_id": data.get("from_principal_id"),
        "from_session_id": data.get("from_session_id") or "",
        "from_agent": data.get("from_agent") or "",
        "from_model": data.get("from_model") or "",
        "to_kind": data.get("to_kind") or "",
        "to_ref": data.get("to_ref") or "",
        "kind": data.get("kind") or "note",
        "body": data.get("body") or "",
        "related_task_id": data.get("related_task_id"),
        "created_at": data.get("created_at") or "",
        "matched_by": matched_by,
    }


def _matched_by(row: dict, *, session_id: str, agent: str) -> str:
    """Why this message is in your inbox — the address that matched.

    Cheap to compute and worth returning: without it a project-channel notice
    and a message meant for you personally look identical in the list.
    """
    to_kind = row.get("to_kind") or ""
    to_ref = row.get("to_ref") or ""
    if to_kind == "session" and to_ref == session_id:
        return "session"
    if to_kind == "agent" and to_ref == agent:
        return "agent"
    return to_kind


async def _own_session(
    db: aiosqlite.Connection,
    session_id: str,
    *,
    agent: str,
    principal_id: int | None,
) -> dict:
    """The caller's own session, or a refusal. Empty dict when none is given.

    Used on both sides of the channel, and for the same reason. Sending without
    a session stays allowed — that is how a human posts from the UI (#775) —
    but naming a session you do not own is refused whether you are writing
    (forged provenance) or reading (someone else's mail).
    """
    if not session_id:
        return {}
    row = await repo.get_agent_session(db, session_id)
    if not row:
        raise _refuse(
            404,
            "unknown_session",
            f"session {session_id} is not registered",
            "Register it with hub_session_register, or omit session_id.",
        )
    data = dict(row)
    owner_principal = data.get("principal_id")
    if principal_id is not None and owner_principal is not None:
        if owner_principal != principal_id:
            raise _refuse(
                403,
                "foreign_session",
                f"session {session_id} belongs to another principal",
                "Use your own session: the token decides whose session this is.",
            )
    elif (data.get("agent") or "") != agent:
        raise _refuse(
            403,
            "foreign_session",
            f"session {session_id} belongs to {data.get('agent') or 'another agent'}",
            "Use your own session: the token decides whose session this is.",
        )
    return data


async def _delivery(db: aiosqlite.Connection, to_kind: str, to_ref: str) -> dict:
    """What can honestly be said about reaching this address, before writing."""
    if to_kind == "session":
        row = await repo.get_agent_session(db, to_ref)
        if not row:
            raise _refuse(
                404,
                "unknown_addressee",
                f"no session {to_ref} in the registry",
                "List addressable sessions with hub_sessions.",
            )
        presence = session_view(row)
        age = presence["last_seen_age_seconds"]
        if presence["online"]:
            return {
                "delivered_now": True,
                "addressee_online": True,
                "addressee_last_seen_age_seconds": age,
                "note": "адресат в сети, сообщение в его инбоксе",
            }
        seen = "неизвестно когда" if age is None else f"{age}s назад"
        return {
            "delivered_now": False,
            "addressee_online": False,
            "addressee_last_seen_age_seconds": age,
            "note": (
                "сообщение сохранено, но сейчас не доставлено: адресат не в сети, "
                f"последний признак жизни {seen}"
            ),
        }
    if to_kind == "task":
        if not to_ref.isdigit() or not await repo.get_task(db, int(to_ref)):
            raise _refuse(
                404,
                "unknown_addressee",
                f"no task #{to_ref}",
                "Address a task channel by its numeric id.",
            )
    # Channels have no single addressee, so there is nobody whose presence
    # could be reported. Saying "delivered" here would be a claim about
    # readers nobody counted.
    return {
        "delivered_now": None,
        "addressee_online": None,
        "addressee_last_seen_age_seconds": None,
        "note": "канал: сообщение доступно всем, кто его читает",
    }


async def send_message(
    db: aiosqlite.Connection,
    body: MessageSend,
    *,
    agent: str,
    principal_id: int | None,
) -> MessageSendResult:
    """Write one message and announce it, in a single transaction."""
    to_kind = (body.to_kind or "").strip().lower()
    if to_kind not in ADDRESS_KINDS:
        raise _refuse(
            422,
            "invalid_address_kind",
            f"to_kind must be one of {sorted(ADDRESS_KINDS)}, got '{to_kind}'",
            "session = one session, agent = an agent's mail, task/project = channels.",
        )
    kind = (body.kind or "note").strip().lower()
    if kind not in MESSAGE_KINDS:
        raise _refuse(
            422,
            "invalid_message_kind",
            f"kind must be one of {sorted(MESSAGE_KINDS)}, got '{kind}'",
            "Pick the coordination move; 'note' when nothing is expected back.",
        )
    to_ref = (body.to_ref or "").strip()
    if not to_ref:
        raise _refuse(
            422,
            "missing_address",
            "to_ref is required",
            "A message without an address has nowhere to arrive.",
        )
    text = (body.body or "").strip()
    if not text:
        raise _refuse(
            422,
            "empty_message",
            "body is required",
            "Say the thing: an empty message is noise with a timestamp.",
        )
    if len(text) > config.MESSAGE_MAX_CHARS:
        raise _refuse(
            422,
            "message_too_long",
            f"body is {len(text)} chars, limit is {config.MESSAGE_MAX_CHARS}",
            "Link the task or PR instead of pasting diffs and logs into the channel.",
        )

    session = await _own_session(
        db, (body.session_id or "").strip(), agent=agent, principal_id=principal_id
    )
    session_id = session.get("session_id") or ""
    recent = await repo.count_recent_messages(
        db, session_id=session_id, agent=agent, within_minutes=1
    )
    if recent >= config.MESSAGE_RATE_PER_MINUTE:
        raise _refuse(
            429,
            "message_rate_limited",
            f"{recent} messages in the last minute, limit is "
            f"{config.MESSAGE_RATE_PER_MINUTE}",
            "Coordination is a few messages, not a stream — batch what you have to say.",
        )

    thread_id = ""
    if body.reply_to:
        parent = await repo.get_agent_message(db, body.reply_to)
        if not parent:
            raise _refuse(
                404,
                "unknown_thread",
                f"no message #{body.reply_to} to reply to",
                "Reply to a message id you saw in the inbox, or omit reply_to.",
            )
        thread_id = dict(parent).get("thread_id") or str(body.reply_to)

    delivery = await _delivery(db, to_kind, to_ref)

    try:
        message_id = await repo.insert_agent_message(
            db,
            from_principal_id=principal_id,
            from_session_id=session_id,
            from_agent=agent,
            from_model=session.get("model") or "",
            to_kind=to_kind,
            to_ref=to_ref,
            kind=kind,
            body=text,
            related_task_id=body.related_task_id,
            thread_id=thread_id,
        )
        # The event carries the address and the id, never the text: it says
        # "you have mail", and the mail itself is read through the inbox under
        # the reader's own authorization (#774 builds the wake-up on this).
        await repo.insert_event(
            db,
            kind="message_posted",
            task_id=body.related_task_id,
            actor=agent,
            payload={
                "message_id": message_id,
                "to_kind": to_kind,
                "to_ref": to_ref,
                "kind": kind,
                "from_agent": agent,
                "from_session_id": session_id,
            },
        )
        await db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        raise _refuse(
            500,
            "message_not_stored",
            f"message was not stored: {exc}",
            "Nothing was written and no notification was sent — retry the send.",
        ) from exc

    if session_id:
        await repo.touch_agent_session(db, session_id)
        await db.commit()

    row = await repo.get_agent_message(db, message_id)
    return MessageSendResult(
        message=MessageView(**message_view(row)),  # type: ignore[arg-type]
        delivery=MessageDelivery(
            addressee_kind=to_kind,
            addressee=to_ref,
            **delivery,
        ),
    )


async def inbox(
    db: aiosqlite.Connection,
    *,
    agent: str,
    session_id: str = "",
    principal_id: int | None = None,
    after_id: int = 0,
    limit: int = 100,
) -> list[MessageView]:
    """Messages addressed to this caller, oldest first, after the cursor.

    ``session_id`` names one of the CALLER'S sessions and is verified as such
    before it reaches the query. Without that check the parameter would be a
    read-anyone's-mail switch: the address predicate lives in SQL precisely so
    that nothing a caller passes can widen it, and a session id taken on trust
    would hand the caller a wider address instead.
    """
    session_id = (session_id or "").strip()
    if session_id:
        await _own_session(db, session_id, agent=agent, principal_id=principal_id)
    rows = await repo.list_inbox_messages(
        db, session_id=session_id, agent=agent, after_id=after_id, limit=limit
    )
    views: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        views.append(
            message_view(
                data, matched_by=_matched_by(data, session_id=session_id, agent=agent)
            )
        )
    return [MessageView(**v) for v in views]


async def thread(
    db: aiosqlite.Connection, thread_id: str, *, limit: int = 200
) -> list[MessageView]:
    """A whole conversation, for the owner's view (#775) and for context."""
    rows = await repo.list_thread_messages(db, thread_id, limit=limit)
    return [MessageView(**message_view(row)) for row in rows]
