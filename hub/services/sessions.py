"""Agent session registry (#771, feature #770): the session becomes an address.

Until now a session existed in the hub only as ``tasks.claim_session_id`` — a
string written on claim and erased on release. It could not be listed, found by
agent, or told apart from a session that died an hour ago. Everything one
session needed to say to another therefore went through a human.

Two decisions shape this module, and both are about honesty rather than
convenience:

1. **Presence is derived, never stored.** There is no ``online`` column. An
   agent dies without saying goodbye — a crashed shell, a closed laptop, a
   cloud run that timed out — so a stored ``online=true`` would keep glowing
   over a session that is not there. That is the same green-light-over-an-
   unrun-check failure #725 fixed, and the reason every read here reports
   ``last_seen_age_seconds`` next to the status: the caller sees how fresh the
   claim of liveness actually is.

2. **Identity comes from the token.** ``agent`` and ``principal_id`` are taken
   from the authenticated identity, never from the request body. A session that
   could name itself would let anyone claim another agent's address — and the
   drift between a body-supplied name and the token's principal is exactly what
   made draft #741 unwithdrawable. The row itself is bound to that principal
   (#977): a later register or heartbeat from someone else must not take the
   address or refresh its sign of life.

The registry is optional by construction: nothing in the task lifecycle
requires a session to be registered, and every write below is a no-op when it
is not (#771 AC-5).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import config
from hub import repository as repo
from hub.actionable_errors import (
    session_address_conflict_detail,
    session_owned_by_other_detail,
)
from hub.models import SessionRegister, SessionView, UnaddressableTask

_NOT_REGISTERED = (
    "session {session_id} is not registered — call POST /api/sessions/register first"
)

ONLINE = "online"
OFFLINE = "offline"


def _age_seconds(stamp: str | None, *, now: datetime | None = None) -> int | None:
    """Whole seconds since a SQLite timestamp, or None if it cannot be read.

    None is not zero and not "fresh": an unreadable timestamp means the age is
    unknown, and the caller renders it as such instead of inventing liveness.
    """
    if not stamp:
        return None
    text = str(stamp).strip().replace(" ", "T")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return max(0, int((current - moment).total_seconds()))


def session_view(row: aiosqlite.Row | dict, *, now: datetime | None = None) -> dict:
    """One registry row with presence computed at read time."""
    data = dict(row)
    age = _age_seconds(data.get("last_seen_at"), now=now)
    ttl_seconds = max(0, config.SESSION_TTL_MINUTES) * 60
    # An unknown age is treated as offline: silence is never a sign of life.
    online = age is not None and age <= ttl_seconds
    return {
        "session_id": data.get("session_id") or "",
        "agent": data.get("agent") or "",
        "model": data.get("model") or "",
        "host": data.get("host") or "",
        "workspace": data.get("workspace") or "",
        "principal_id": data.get("principal_id"),
        "current_task_id": data.get("current_task_id"),
        "started_at": data.get("started_at") or "",
        "last_seen_at": data.get("last_seen_at") or "",
        "last_seen_age_seconds": age,
        "online": online,
        "status": ONLINE if online else OFFLINE,
        "ttl_minutes": config.SESSION_TTL_MINUTES,
    }


def _owned_by_other(row: aiosqlite.Row | dict, principal_id: int | None) -> bool:
    """True when this row already belongs to a different principal (#977).

    An unowned row (principal_id NULL, typically open-mode) is not a foreign
    hold — the first attributed caller may take it. A row with an owner
    refuses every other principal, including an anonymous one.
    """
    owner = dict(row).get("principal_id")
    if owner is None:
        return False
    return principal_id != owner


_ADDRESS_FIELDS = ("host", "workspace")


def _address_conflict(
    row: aiosqlite.Row | dict, declared: dict[str, str]
) -> dict[str, str] | None:
    """Declared address fields that contradict the stored ones (#1288).

    The second question the registry was missing. #977 asks whether the row
    belongs to someone else; every executor runs under the same principal, so
    that check passes and an id collision reads as the same session saying
    hello twice — which is how the row a task's ``claim_session_id`` pointed at
    came to describe another machine and another worktree.

    Only DECLARED non-empty values are compared. An omitted field means "not
    declared" — it neither erases what is stored (the CASE WHEN rule) nor
    counts as a mismatch, so a heartbeat-shaped register stays idempotent. A
    stored empty value is unknown, not different: the first declaration fills
    it in.
    """
    stored = dict(row)
    differing = {
        field: value
        for field in _ADDRESS_FIELDS
        if (value := declared.get(field, "").strip())
        and (known := str(stored.get(field) or "").strip())
        and known != value
    }
    return differing or None


async def register_session(
    db: aiosqlite.Connection,
    body: SessionRegister,
    *,
    agent: str,
    principal_id: int | None,
) -> SessionView:
    """Register this session under the caller's identity, or refresh it.

    Idempotent by ``session_id``: a second call from the same session updates
    what it declares and its sign of life, and keeps ``started_at`` — saying
    hello twice does not make it a new session. A ``session_id`` already
    held by another principal is refused (409); the existing row is not
    rewritten (#977).
    """
    session_id = body.session_id.strip()
    if not session_id:
        raise HTTPException(422, "session_id is required")
    declared = {"host": body.host.strip(), "workspace": body.workspace.strip()}
    existing = await repo.get_agent_session(db, session_id)
    if existing is not None and _owned_by_other(existing, principal_id):
        # Ownership is asked first, on purpose: a stranger learns the id is
        # taken and nothing about where its work lives.
        raise HTTPException(
            409, detail=session_owned_by_other_detail(session_id=session_id)
        )
    if existing is not None and (diff := _address_conflict(existing, declared)):
        raise HTTPException(
            409,
            detail=session_address_conflict_detail(
                session_id=session_id,
                registered={f: str(dict(existing).get(f) or "") for f in diff},
                declared=diff,
            ),
        )
    await repo.upsert_agent_session(
        db,
        session_id=session_id,
        principal_id=principal_id,
        agent=agent,
        model=body.model.strip(),
        host=declared["host"],
        workspace=declared["workspace"],
    )
    await db.commit()
    row = await repo.get_agent_session(db, session_id)
    if row is not None and _owned_by_other(row, principal_id):
        # Lost the insert race to another principal; the UPSERT WHERE left
        # their row untouched. Refuse without returning it.
        raise HTTPException(
            409, detail=session_owned_by_other_detail(session_id=session_id)
        )
    if row is not None and (diff := _address_conflict(row, declared)):
        # Same race, other question: a register that slipped in between the
        # check above and this write kept its address (the UPSERT WHERE), so
        # what we read back is not the address we declared. Refuse rather than
        # return a row describing someone else's machine.
        raise HTTPException(
            409,
            detail=session_address_conflict_detail(
                session_id=session_id,
                registered={f: str(dict(row).get(f) or "") for f in diff},
                declared=diff,
            ),
        )
    return SessionView(**session_view(row))  # type: ignore[arg-type]


async def heartbeat_session(
    db: aiosqlite.Connection,
    session_id: str,
    *,
    principal_id: int | None = None,
) -> SessionView:
    """Record a sign of life for an already registered session.

    A foreign ``session_id`` is indistinguishable from an unregistered one
    (404, no ``last_seen_at`` bump): leaking that the id exists would be
    the other half of the hijack #977 closes.
    """
    known = await repo.get_agent_session(db, session_id)
    if known is None or _owned_by_other(known, principal_id):
        raise HTTPException(404, _NOT_REGISTERED.format(session_id=session_id))
    touched = await repo.touch_agent_session(db, session_id)
    if not touched:
        raise HTTPException(404, _NOT_REGISTERED.format(session_id=session_id))
    await db.commit()
    row = await repo.get_agent_session(db, session_id)
    return SessionView(**session_view(row))  # type: ignore[arg-type]


async def list_sessions(
    db: aiosqlite.Connection,
    *,
    agent: str = "",
    status: str = "",
    limit: int = 200,
) -> list[SessionView]:
    """Registered sessions with presence computed now.

    ``status`` filters the computed value (online/offline) rather than a stored
    one — the filter and the badge can never disagree because there is only one
    place where presence is decided.
    """
    wanted = (status or "").strip().lower()
    if wanted and wanted not in {ONLINE, OFFLINE}:
        raise HTTPException(422, f"status must be '{ONLINE}' or '{OFFLINE}'")
    rows = await repo.list_agent_sessions(db, agent=agent.strip(), limit=limit)
    now = datetime.now(UTC)
    views: list[dict[str, Any]] = [session_view(row, now=now) for row in rows]
    if wanted:
        views = [v for v in views if v["status"] == wanted]
    return [SessionView(**v) for v in views]


async def note_session_task(
    db: aiosqlite.Connection, session_id: str, task_id: int | None
) -> None:
    """Point a session at the task it just took, or clear it on release.

    Called from the claim/release path inside its transaction, and silent when
    the session is unregistered: the registry follows the lifecycle, never the
    other way round.
    """
    if not session_id:
        return
    await repo.set_agent_session_task(db, session_id, task_id)


async def pair_executor_online(
    db: aiosqlite.Connection, task: dict, *, now: datetime | None = None
) -> bool:
    """Is the session that owns this pair task still signing in (#1030)?

    Asked by the delivery sweep before it keeps a task on the conveyor over a
    refusal only the executor can cure. Waiting for somebody who is not there
    is not patience — it is a task nobody will ever pick up, and ``running:pair``
    has no machine deadline to catch it (the #418 backstop covers machine-owned
    instances only).

    Presence is the registry's own answer, computed at read time as everywhere
    else: an unregistered claim, an unknown session or a stale sign of life all
    mean "not around", which is exactly today's behaviour — a human is called.
    """
    session_id = (task.get("claim_session_id") or "").strip()
    if not session_id:
        return False
    row = await repo.get_agent_session(db, session_id)
    if row is None:
        return False
    return bool(session_view(row, now=now)["online"])


async def unaddressable_tasks(
    db: aiosqlite.Connection, *, limit: int = 200
) -> list[UnaddressableTask]:
    """Tasks in flight that no session can be reached about (#852).

    The complement of the registry: ``list_sessions`` answers "who is around",
    this answers "what is being done by nobody addressable". Tightening the
    claim contract keeps NEW tasks out of that state; it cannot empty the
    tail that is already there, and a tail nobody can see reads as absent.
    """
    rows = await repo.list_unaddressable_tasks(db, limit=limit)
    return [
        UnaddressableTask(
            id=int(dict(row).get("id") or 0),
            title=str(dict(row).get("title") or ""),
            status=str(dict(row).get("status") or ""),
            claimed_by=str(dict(row).get("claimed_by") or ""),
            claimed_at=str(dict(row).get("claimed_at") or ""),
            branch=str(dict(row).get("branch") or ""),
        )
        for row in rows
    ]
