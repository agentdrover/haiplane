"""Chat-pair: a one-time code from the hub, spent for a short session (#961).

The channel exists because Cursor on iOS and cursor.com/agents cannot attach a
custom MCP: without an identity the agent in that chat gets 401, and a
long-lived token pasted into the chat stays in the transcript forever.

Two properties hold the whole design together:

* the code and the session token exist in plaintext exactly once — in the
  response that hands them out. The database stores hashes, so a dump of it
  cannot be replayed against the hub;
* the session carries a FIXED permission set (:data:`hub.config.CHAT_PAIR_PERMS`),
  never the issuer's own rights. The same principal is usually ``admin`` in
  production, and a code living in somebody else's transcript must not be worth
  an admin token.

What the session may reach is decided one layer up, by the deny-by-default
route allowlist in :mod:`hub.auth` — permissions alone would not have been
enough, because several hub branches read "not an agent" as "a human".
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Any, Final

import aiosqlite

from hub import config
from hub import repository as repo
from hub.auth import LoginRateLimiter
from hub.config import TokenIdentity
from hub.db import fetchall

log = logging.getLogger("hub.services.chat_pair")

# Crockford base32: no I, L, O or U — the pairs a person mistypes when copying
# a code off a screen, plus the letter that turns codes into words.
CODE_ALPHABET: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH: Final[int] = 8
CODE_PREFIX: Final[str] = "AH"

TOKEN_PREFIX: Final[str] = "ahcp_"

#: Redeem is public, so it gets a limiter of its own — writing into
#: ``login_limiter`` would share one per-IP bucket with the web login and let a
#: burst of pairing attempts lock the operator out of their own hub.
chat_pair_limiter = LoginRateLimiter(
    max_attempts=config.CHAT_PAIR_REDEEM_MAX,
    window_seconds=config.CHAT_PAIR_REDEEM_WINDOW_SECONDS,
)


def redeem_limit_reached(key: str) -> bool:
    """Whether this client already spent its attempts in the current window.

    The threshold is read from config on every call rather than frozen at
    import: tests (and a drop-in on the VM) change it at runtime, and a limiter
    that answers by a value captured at boot would quietly ignore them.
    """
    chat_pair_limiter._max_attempts = config.CHAT_PAIR_REDEEM_MAX
    chat_pair_limiter._window_seconds = config.CHAT_PAIR_REDEEM_WINDOW_SECONDS
    return chat_pair_limiter.is_blocked(key)


def record_redeem_attempt(key: str) -> None:
    chat_pair_limiter.record(key)


# ---------------------------------------------------------------------------
# Code shape
# ---------------------------------------------------------------------------


def normalize_pair_code(raw: str) -> str:
    """Bring any form a person may paste to the one form we hash.

    ``AH-7K2M9QRS``, ``ah 7k2m9qrs`` and ``7K2M9QRS`` are the same code; the
    prefix and the dash are there to be read aloud and typed, not to be stored.
    Everything downstream — the hash, the lookup, the comparison — sees only
    what this function returns, so there is exactly one normalisation.
    """
    cleaned = "".join(ch for ch in (raw or "").upper() if ch.isalnum())
    if len(cleaned) == CODE_LENGTH + len(CODE_PREFIX) and cleaned.startswith(
        CODE_PREFIX
    ):
        cleaned = cleaned[len(CODE_PREFIX) :]
    return cleaned


def format_pair_code(normalized: str) -> str:
    """The form the operator sees: ``AH-XXXXYYYY``."""
    return f"{CODE_PREFIX}-{normalized}"


def hash_pair_code(value: str) -> str:
    """One hash for both secrets of this channel — codes and session tokens."""
    return hashlib.sha256(value.encode()).hexdigest()


def generate_pair_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def generate_session_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def _as_iso(stamp: str | None) -> str:
    """SQLite's ``datetime('now')`` shape rendered as UTC ISO-8601."""
    if not stamp:
        return ""
    return stamp.replace(" ", "T") + "Z"


# ---------------------------------------------------------------------------
# Issuing a code
# ---------------------------------------------------------------------------


async def issue_code(
    db: aiosqlite.Connection,
    principal_id: int,
    *,
    kind: str = "intake",
    bound_task_id: int | None = None,
    bound_generation: int | None = None,
) -> tuple[str, int]:
    """Burn unused codes in this (principal, kind, bound_task_id) bucket, mint one.

    Intake and implementer do not kill each other: burn is scoped to the
    same kind (and, for implementer, the same bound task).
    """
    kind = (kind or "intake").strip().lower() or "intake"
    if kind in config.CHAT_PAIR_TASK_BOUND_KINDS:
        await db.execute(
            "DELETE FROM chat_pair_codes WHERE principal_id = ? AND kind = ? "
            "AND bound_task_id = ? AND redeemed_at IS NULL",
            (principal_id, kind, bound_task_id),
        )
    else:
        await db.execute(
            "DELETE FROM chat_pair_codes WHERE principal_id = ? AND kind = ? "
            "AND redeemed_at IS NULL",
            (principal_id, kind),
        )
    ttl = config.CHAT_PAIR_CODE_SECONDS
    code = generate_pair_code()
    await db.execute(
        "INSERT INTO chat_pair_codes "
        "(principal_id, kind, bound_task_id, bound_generation, code_hash, "
        " expires_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now', ?))",
        (
            principal_id,
            kind,
            bound_task_id if kind in config.CHAT_PAIR_TASK_BOUND_KINDS else None,
            bound_generation,
            hash_pair_code(code),
            f"+{int(ttl)} seconds",
        ),
    )
    await db.commit()
    return format_pair_code(code), ttl


# ---------------------------------------------------------------------------
# Spending it
# ---------------------------------------------------------------------------


async def redeem_code(db: aiosqlite.Connection, raw_code: str) -> dict[str, Any] | None:
    """Exchange a code for a session. ``None`` for every way that can fail.

    Unknown, already spent and expired are ONE answer on purpose: three
    distinguishable refusals would tell a caller enumerating codes which
    guesses were close.
    """
    normalized = normalize_pair_code(raw_code)
    if len(normalized) != CODE_LENGTH:
        return None

    rows = await fetchall(
        db,
        "SELECT c.id, c.principal_id, c.kind, c.bound_task_id, "
        "c.bound_generation, p.username, p.status "
        "FROM chat_pair_codes c JOIN principals p ON p.id = c.principal_id "
        "WHERE c.code_hash = ? AND c.redeemed_at IS NULL "
        "AND c.expires_at > datetime('now')",
        (hash_pair_code(normalized),),
    )
    if not rows:
        return None
    row = dict(rows[0])
    if row["status"] != "active":
        return None

    kind = (row.get("kind") or "intake").strip().lower() or "intake"
    bound_task_id = row.get("bound_task_id")
    acting_id: int | None = None
    acting_username = row["username"]
    if kind == "implementer":
        if bound_task_id is None:
            return None
        task_rows = await fetchall(
            db, "SELECT status FROM tasks WHERE id = ?", (int(bound_task_id),)
        )
        if not task_rows or dict(task_rows[0]).get("status") != "open":
            return None
        acting = await get_acting_agent(db)
        if acting is None:
            return None
        acting_id = int(acting["id"])
        acting_username = str(acting["username"])
    elif kind == "reviewer":
        # #1084: minted by the dispatch, not by a person, and spent by the
        # cloud run it was minted for. It acts AS its issuer — the reviewer
        # principal the dispatch resolved from CURSOR_REVIEWER_HUB_TOKEN — so
        # the report lands under the identity the dispatch already pinned
        # (#1025) and needs no second rule to be recognised as its own.
        #
        # The task must be IN REVIEW: a code outliving its submission would
        # let a run file a report against work that has moved on.
        if bound_task_id is None:
            return None
        task_rows = await fetchall(
            db,
            "SELECT status, submission_generation FROM tasks WHERE id = ?",
            (int(bound_task_id),),
        )
        if not task_rows or dict(task_rows[0]).get("status") != "review":
            return None
        # The submission, not just the status. A resubmission during a live
        # run keeps the task in review, so status alone would let a code
        # minted for generation N be spent against N+1 — a report about the
        # old diff, recorded as a report about the new one.
        pinned = row.get("bound_generation")
        if pinned is not None:
            current = dict(task_rows[0]).get("submission_generation") or 0
            if int(pinned) != int(current):
                return None

    cursor = await db.execute(
        "UPDATE chat_pair_codes SET redeemed_at = datetime('now') "
        "WHERE id = ? AND redeemed_at IS NULL",
        (row["id"],),
    )
    if not cursor.rowcount:
        await db.rollback()
        return None

    token = generate_session_token()
    ttl = config.CHAT_PAIR_TTL_SECONDS
    await db.execute(
        "INSERT INTO chat_pair_sessions "
        "(principal_id, acting_principal_id, kind, bound_task_id, "
        " bound_generation, token_hash, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, datetime('now', ?))",
        (
            row["principal_id"],
            acting_id,
            kind,
            bound_task_id if kind in config.CHAT_PAIR_TASK_BOUND_KINDS else None,
            row.get("bound_generation"),
            hash_pair_code(token),
            f"+{int(ttl)} seconds",
        ),
    )
    await db.commit()

    stored = await fetchall(
        db,
        "SELECT expires_at FROM chat_pair_sessions WHERE token_hash = ?",
        (hash_pair_code(token),),
    )
    return {
        "token": token,
        "principal_id": row["principal_id"],
        "username": acting_username,
        "kind": kind,
        "bound_task_id": int(bound_task_id) if bound_task_id is not None else None,
        "expires_at": _as_iso(dict(stored[0])["expires_at"] if stored else None),
    }


async def get_acting_agent(db: aiosqlite.Connection) -> dict[str, Any] | None:
    """The implementer acting principal, or None if missing/inactive."""
    username = (config.CHAT_PAIR_AGENT or "cloud").strip() or "cloud"
    rows = await fetchall(
        db,
        "SELECT id, username, status FROM principals WHERE username = ?",
        (username,),
    )
    if not rows:
        return None
    row = dict(rows[0])
    if row["status"] != "active":
        return None
    return row


async def resolve_session(db: aiosqlite.Connection, token: str) -> TokenIdentity | None:
    """Identity behind a chat-pair bearer token, or ``None``.

    Intake keeps ``role='human'`` as presentational naming of the issuer.
    Implementer walks as ``role='agent'`` under the acting principal's name,
    while ``principal_id`` stays the issuer so revoke and audit still land
    on the human who issued the code (#980).
    """
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    rows = await fetchall(
        db,
        "SELECT s.principal_id, s.acting_principal_id, s.kind, s.bound_task_id, "
        "s.bound_generation, p.username AS issuer_username, p.status AS issuer_status "
        "FROM chat_pair_sessions s JOIN principals p ON p.id = s.principal_id "
        "WHERE s.token_hash = ? AND s.revoked_at IS NULL "
        "AND s.expires_at > datetime('now')",
        (hash_pair_code(token),),
    )
    if not rows:
        return None
    row = dict(rows[0])
    if row["issuer_status"] != "active":
        return None
    kind = (row.get("kind") or "intake").strip().lower() or "intake"
    bound = row.get("bound_task_id")
    bound_id = int(bound) if bound is not None else None
    if kind == "implementer":
        acting_id = row.get("acting_principal_id")
        if acting_id is None:
            return None
        acting_rows = await fetchall(
            db,
            "SELECT username, status FROM principals WHERE id = ?",
            (int(acting_id),),
        )
        if not acting_rows:
            return None
        acting = dict(acting_rows[0])
        if acting["status"] != "active":
            return None
        return TokenIdentity(
            username=acting["username"],
            role="agent",
            principal_id=row["principal_id"],
            permissions=config.CHAT_PAIR_IMPLEMENTER_PERMS,
            auth_source="chat_pair",
            chat_pair_kind="implementer",
            chat_pair_task_id=bound_id,
        )
    if kind == "reviewer":
        # Walks as an agent under its issuer: no separate acting principal,
        # because the issuer IS the reviewer principal (see redeem_code).
        return TokenIdentity(
            username=row["issuer_username"],
            role="agent",
            principal_id=row["principal_id"],
            permissions=config.CHAT_PAIR_REVIEWER_PERMS,
            auth_source="chat_pair",
            chat_pair_kind="reviewer",
            chat_pair_task_id=bound_id,
            chat_pair_generation=(
                int(row["bound_generation"])
                if row.get("bound_generation") is not None
                else None
            ),
        )
    return TokenIdentity(
        username=row["issuer_username"],
        role="human",
        principal_id=row["principal_id"],
        permissions=config.CHAT_PAIR_PERMS,
        auth_source="chat_pair",
        chat_pair_kind="intake",
        chat_pair_task_id=None,
    )


async def revoke_sessions(
    db: aiosqlite.Connection,
    principal_id: int,
    *,
    kind: str = "intake",
    bound_task_id: int | None = None,
) -> int:
    """Close live chat-pair sessions of this principal, scoped by kind.

    Intake revoke must not kill a running implementer, and the other way
    around (#980). ``bound_task_id`` further narrows implementer revoke when
    the caller names a task.
    """
    kind = (kind or "intake").strip().lower() or "intake"
    if kind == "implementer" and bound_task_id is not None:
        cursor = await db.execute(
            "UPDATE chat_pair_sessions SET revoked_at = datetime('now') "
            "WHERE principal_id = ? AND kind = ? AND bound_task_id = ? "
            "AND revoked_at IS NULL",
            (principal_id, kind, bound_task_id),
        )
    else:
        cursor = await db.execute(
            "UPDATE chat_pair_sessions SET revoked_at = datetime('now') "
            "WHERE principal_id = ? AND kind = ? AND revoked_at IS NULL",
            (principal_id, kind),
        )
    await db.commit()
    return int(cursor.rowcount or 0)


async def release_expired_implementer_tasks(db: aiosqlite.Connection) -> list[int]:
    """Return bound pair tasks to open when their implementer session is dead.

    Intake TTL is unchanged (#983 out of scope). A live token must not trip
    this. Review/completed stay: the work already left the pairing window.
    Headless ``job_id`` rows are not pairing sessions and are left alone.
    A dead sibling session must not yank a task that still has another
    unexpired, unrevoked implementer session on the same bound task.
    """
    rows = await fetchall(
        db,
        "SELECT s.bound_task_id AS task_id, t.status AS status, t.job_id AS job_id "
        "FROM chat_pair_sessions s "
        "JOIN tasks t ON t.id = s.bound_task_id "
        "WHERE s.kind = 'implementer' AND s.bound_task_id IS NOT NULL "
        "AND (s.expires_at < datetime('now') OR s.revoked_at IS NOT NULL) "
        "AND t.status IN ('running', 'claimed') "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM chat_pair_sessions live "
        "  WHERE live.kind = 'implementer' "
        "  AND live.bound_task_id = s.bound_task_id "
        "  AND live.revoked_at IS NULL "
        "  AND live.expires_at > datetime('now')"
        ")",
    )
    released: list[int] = []
    seen: set[int] = set()
    for raw in rows:
        row = dict(raw)
        if row.get("job_id"):
            continue
        task_id = int(row["task_id"])
        if task_id in seen:
            continue
        seen.add(task_id)
        from_status = str(row["status"])
        if not await repo.transition_status_if(
            db, task_id, expected_from=from_status, new_status="open"
        ):
            continue
        await repo.update_task(
            db,
            task_id,
            claimed_by=None,
            claim_session_id=None,
            claimed_at=None,
            implementer_principal_id=None,
        )
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "status",
            "Implementer pairing session expired; task returned to open.",
        )
        await repo.insert_event(
            db,
            kind="chat_pair_implementer_expired",
            task_id=task_id,
            actor="hub",
            payload={"reason": "chat_pair_implementer_expired", "from": from_status},
        )
        released.append(task_id)
    if released:
        await db.commit()
        log.info(
            "Chat-pair reaper: released %d implementer-bound task(s) to open",
            len(released),
        )
    return released


async def purge_expired(db: aiosqlite.Connection) -> int:
    """Drop what can no longer be used: the reaper's half of the channel.

    Spent codes linger for a retention window so "was this code used?" has an
    answer for a while; everything expired or revoked goes immediately.
    Dead implementer sessions unstick their bound pair task first (#983),
    otherwise purge would delete the only row that still named the task.
    """
    await release_expired_implementer_tasks(db)
    hours = int(config.CHAT_PAIR_SPENT_RETENTION_HOURS)
    codes = await db.execute(
        "DELETE FROM chat_pair_codes WHERE expires_at < datetime('now') "
        "OR (redeemed_at IS NOT NULL AND redeemed_at < datetime('now', ?))",
        (f"-{hours} hours",),
    )
    sessions = await db.execute(
        "DELETE FROM chat_pair_sessions WHERE expires_at < datetime('now') "
        "OR revoked_at IS NOT NULL"
    )
    removed = int(codes.rowcount or 0) + int(sessions.rowcount or 0)
    await db.commit()
    return removed
