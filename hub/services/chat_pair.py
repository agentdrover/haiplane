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


async def issue_code(db: aiosqlite.Connection, principal_id: int) -> tuple[str, int]:
    """Burn this principal's unspent codes, mint a new one.

    Returns ``(code_for_display, ttl_seconds)``. Burning first is what makes
    "I lost the code, give me another" safe: at most one code of a principal is
    ever live, so a code glimpsed over a shoulder dies the moment its owner
    asks for a replacement.
    """
    await db.execute(
        "DELETE FROM chat_pair_codes WHERE principal_id = ? AND redeemed_at IS NULL",
        (principal_id,),
    )
    ttl = config.CHAT_PAIR_CODE_SECONDS
    code = generate_pair_code()
    # The TTL is bound, not interpolated: the value comes from config and is an
    # int, but a security-sensitive module should not carry a query a scanner
    # has to be argued with.
    await db.execute(
        "INSERT INTO chat_pair_codes (principal_id, code_hash, expires_at) "
        "VALUES (?, ?, datetime('now', ?))",
        (principal_id, hash_pair_code(code), f"+{int(ttl)} seconds"),
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
        "SELECT c.id, c.principal_id, p.username, p.status "
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

    # Burn-after-read is a conditional UPDATE, not a read followed by a write:
    # two redeems racing on one code must not both see it unspent.
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
        "INSERT INTO chat_pair_sessions (principal_id, token_hash, expires_at) "
        "VALUES (?, ?, datetime('now', ?))",
        (row["principal_id"], hash_pair_code(token), f"+{int(ttl)} seconds"),
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
        "username": row["username"],
        "expires_at": _as_iso(dict(stored[0])["expires_at"] if stored else None),
    }


async def resolve_session(db: aiosqlite.Connection, token: str) -> TokenIdentity | None:
    """Identity behind a chat-pair bearer token, or ``None``.

    ``role='human'`` is presentational — it names the issuer for whoami and for
    attribution. No gate may decide by it: ``is_admin`` trusts the role string
    and ``has_permission`` short-circuits to True for ``super_admin``, so the
    role is forced to the harmless value and authority comes from
    ``auth_source`` plus the route allowlist.
    """
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    rows = await fetchall(
        db,
        "SELECT s.principal_id, p.username, p.status "
        "FROM chat_pair_sessions s JOIN principals p ON p.id = s.principal_id "
        "WHERE s.token_hash = ? AND s.revoked_at IS NULL "
        "AND s.expires_at > datetime('now')",
        (hash_pair_code(token),),
    )
    if not rows:
        return None
    row = dict(rows[0])
    if row["status"] != "active":
        return None
    return TokenIdentity(
        username=row["username"],
        role="human",
        principal_id=row["principal_id"],
        permissions=config.CHAT_PAIR_PERMS,
        auth_source="chat_pair",
    )


async def revoke_sessions(db: aiosqlite.Connection, principal_id: int) -> int:
    """Close every live chat-pair session of a principal. Returns how many.

    Deliberately narrow: browser sessions and API keys are other channels, and
    "I am done with the phone" must not sign the laptop out.
    """
    cursor = await db.execute(
        "UPDATE chat_pair_sessions SET revoked_at = datetime('now') "
        "WHERE principal_id = ? AND revoked_at IS NULL",
        (principal_id,),
    )
    await db.commit()
    return int(cursor.rowcount or 0)


async def purge_expired(db: aiosqlite.Connection) -> int:
    """Drop what can no longer be used: the reaper's half of the channel.

    Spent codes linger for a retention window so "was this code used?" has an
    answer for a while; everything expired or revoked goes immediately.
    """
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
