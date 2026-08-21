"""Admin service layer for principals, roles, API keys, passwords, audit."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from hub.config import TokenIdentity
from hub.db import fetchall, inserted_id, has_active_admin

log = logging.getLogger("hub.services.admin")

_ph = PasswordHasher()


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False


# ---------------------------------------------------------------------------
# API key hashing
# ---------------------------------------------------------------------------

_KEY_PREFIX_LEN = 8


def generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext_key, key_prefix, key_hash)."""
    plaintext = "ochk_" + secrets.token_urlsafe(32)
    prefix = plaintext[:_KEY_PREFIX_LEN]
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    return plaintext, prefix, key_hash


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------


def generate_session_token() -> tuple[str, str]:
    """Return (plaintext_token, session_hash)."""
    token = secrets.token_urlsafe(48)
    session_hash = hashlib.sha256(token.encode()).hexdigest()
    return token, session_hash


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


async def write_audit(
    db: aiosqlite.Connection,
    *,
    actor_id: int | None,
    action: str,
    target_type: str,
    target_id: str = "",
    summary: str,
    detail: str | None = None,
) -> None:
    await db.execute(
        "INSERT INTO admin_audit_log (actor_principal_id, action, target_type, target_id, summary, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (actor_id, action, target_type, target_id, summary, detail),
    )
    await db.commit()


async def list_audit(
    db: aiosqlite.Connection, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    rows = await fetchall(
        db,
        "SELECT a.*, p.username AS actor_username "
        "FROM admin_audit_log a "
        "LEFT JOIN principals p ON a.actor_principal_id = p.id "
        "ORDER BY a.id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Principals
# ---------------------------------------------------------------------------


async def create_principal(
    db: aiosqlite.Connection,
    *,
    kind: str,
    username: str,
    display_name: str = "",
    email: str = "",
    notes: str = "",
    created_by: int | None = None,
    password: str | None = None,
    role_slug: str | None = None,
) -> dict[str, Any]:
    cursor = await db.execute(
        "INSERT INTO principals (kind, username, display_name, email, notes, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (kind, username, display_name, email, notes, created_by),
    )
    principal_id = inserted_id(cursor)

    if password and kind == "human":
        pw_hash = hash_password(password)
        await db.execute(
            "INSERT INTO password_credentials (principal_id, password_hash) VALUES (?, ?)",
            (principal_id, pw_hash),
        )

    if role_slug:
        role_rows = await fetchall(
            db, "SELECT id FROM roles WHERE slug = ?", (role_slug,)
        )
        if role_rows:
            await db.execute(
                "INSERT INTO principal_roles (principal_id, role_id, granted_by) VALUES (?, ?, ?)",
                (principal_id, role_rows[0][0], created_by),
            )

    await db.commit()
    return await get_principal(db, principal_id)  # type: ignore[return-value]


async def get_principal(
    db: aiosqlite.Connection, principal_id: int
) -> dict[str, Any] | None:
    rows = await fetchall(db, "SELECT * FROM principals WHERE id = ?", (principal_id,))
    if not rows:
        return None
    p = dict(rows[0])
    p["roles"] = await _get_principal_role_slugs(db, principal_id)
    return p


async def list_principals(
    db: aiosqlite.Connection,
    *,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM principals"
    conditions: list[str] = []
    params: list[Any] = []
    if kind:
        conditions.append("kind = ?")
        params.append(kind)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY id ASC LIMIT ?"
    params.append(limit)
    rows = await fetchall(db, sql, params)
    result = []
    for r in rows:
        p = dict(r)
        p["roles"] = await _get_principal_role_slugs(db, p["id"])
        result.append(p)
    return result


async def update_principal(
    db: aiosqlite.Connection,
    principal_id: int,
    *,
    display_name: str | None = None,
    email: str | None = None,
    notes: str | None = None,
) -> dict[str, Any] | None:
    sets: list[str] = ["updated_at = datetime('now')"]
    params: list[Any] = []
    if display_name is not None:
        sets.append("display_name = ?")
        params.append(display_name)
    if email is not None:
        sets.append("email = ?")
        params.append(email)
    if notes is not None:
        sets.append("notes = ?")
        params.append(notes)
    params.append(principal_id)
    await db.execute(f"UPDATE principals SET {', '.join(sets)} WHERE id = ?", params)  # nosec B608
    await db.commit()
    return await get_principal(db, principal_id)


async def disable_principal(
    db: aiosqlite.Connection, principal_id: int
) -> dict[str, Any] | None:
    p = await get_principal(db, principal_id)
    if not p:
        return None
    roles = set(p.get("roles", []))
    if roles & {"super_admin", "admin"} and await _is_last_admin(db, principal_id):
        raise LastAdminError("cannot disable the last active admin")
    await db.execute(
        "UPDATE principals SET status = 'disabled', updated_at = datetime('now') WHERE id = ?",
        (principal_id,),
    )
    await _revoke_all_sessions(db, principal_id)
    await _revoke_all_keys(db, principal_id)
    await db.commit()
    return await get_principal(db, principal_id)


async def enable_principal(
    db: aiosqlite.Connection, principal_id: int
) -> dict[str, Any] | None:
    await db.execute(
        "UPDATE principals SET status = 'active', updated_at = datetime('now') WHERE id = ?",
        (principal_id,),
    )
    await db.commit()
    return await get_principal(db, principal_id)


class LastAdminError(Exception):
    pass


async def _is_last_admin(db: aiosqlite.Connection, principal_id: int) -> bool:
    """True if disabling/removing admin role from this principal leaves zero active admins."""
    rows = await fetchall(
        db,
        """SELECT p.id FROM principals p
           JOIN principal_roles pr ON p.id = pr.principal_id
           JOIN roles r ON pr.role_id = r.id
           WHERE p.status = 'active'
             AND r.slug IN ('super_admin', 'admin')
             AND p.id != ?""",
        (principal_id,),
    )
    return len(rows) == 0


async def _get_principal_role_slugs(
    db: aiosqlite.Connection, principal_id: int
) -> list[str]:
    rows = await fetchall(
        db,
        "SELECT r.slug FROM roles r "
        "JOIN principal_roles pr ON r.id = pr.role_id "
        "WHERE pr.principal_id = ?",
        (principal_id,),
    )
    return [r[0] for r in rows]


async def get_principal_permissions(
    db: aiosqlite.Connection, principal_id: int
) -> frozenset[str]:
    rows = await fetchall(
        db,
        "SELECT DISTINCT rp.permission FROM role_permissions rp "
        "JOIN principal_roles pr ON rp.role_id = pr.role_id "
        "WHERE pr.principal_id = ?",
        (principal_id,),
    )
    return frozenset(r[0] for r in rows)


async def get_effective_role(db: aiosqlite.Connection, principal_id: int) -> str:
    """Return the highest-privilege legacy role name for backward compat."""
    slugs = await _get_principal_role_slugs(db, principal_id)
    for priority in (
        "super_admin",
        "admin",
        "security_admin",
        "operator",
        "developer",
        "reviewer_agent",
        "agent",
        "viewer",
    ):
        if priority in slugs:
            if priority in ("super_admin", "admin", "security_admin"):
                return "admin"
            if priority in ("operator", "developer", "viewer"):
                return "human"
            return "agent"
    return "human"


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


async def list_roles(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    rows = await fetchall(db, "SELECT * FROM roles ORDER BY id ASC")
    result = []
    for r in rows:
        role = dict(r)
        role["system"] = bool(role.get("system"))
        perm_rows = await fetchall(
            db,
            "SELECT permission FROM role_permissions WHERE role_id = ?",
            (role["id"],),
        )
        role["permissions"] = [pr[0] for pr in perm_rows]
        result.append(role)
    return result


async def set_principal_roles(
    db: aiosqlite.Connection,
    principal_id: int,
    role_slugs: list[str],
    *,
    granted_by: int | None = None,
) -> list[str]:
    """Replace the principal's roles. Refuses to remove last admin."""
    p = await get_principal(db, principal_id)
    if not p:
        raise ValueError("principal not found")

    current_slugs = set(p.get("roles", []))
    removing_admin = bool(current_slugs & {"super_admin", "admin"}) and not (
        set(role_slugs) & {"super_admin", "admin"}
    )
    if removing_admin and await _is_last_admin(db, principal_id):
        raise LastAdminError("cannot remove admin role from the last active admin")

    role_ids: list[tuple[int, str]] = []
    for slug in role_slugs:
        rows = await fetchall(db, "SELECT id FROM roles WHERE slug = ?", (slug,))
        if not rows:
            raise ValueError(f"role {slug!r} not found")
        role_ids.append((rows[0][0], slug))

    await db.execute(
        "DELETE FROM principal_roles WHERE principal_id = ?", (principal_id,)
    )
    for rid, _ in role_ids:
        await db.execute(
            "INSERT INTO principal_roles (principal_id, role_id, granted_by) VALUES (?, ?, ?)",
            (principal_id, rid, granted_by),
        )
    await db.commit()
    return [s for _, s in role_ids]


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


async def create_api_key(
    db: aiosqlite.Connection,
    principal_id: int,
    *,
    name: str,
    expires_days: int | None = None,
    created_by: int | None = None,
) -> dict[str, Any]:
    plaintext, prefix, key_hash = generate_api_key()
    expires_at: str | None = None
    if expires_days:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=expires_days)
        ).isoformat()

    cursor = await db.execute(
        "INSERT INTO api_keys (principal_id, name, key_prefix, key_hash, expires_at, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (principal_id, name, prefix, key_hash, expires_at, created_by),
    )
    key_id = cursor.lastrowid
    await db.commit()

    return {
        "id": key_id,
        "principal_id": principal_id,
        "name": name,
        "key_prefix": prefix,
        "expires_at": expires_at,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": created_by,
        "last_used_at": None,
        "revoked_at": None,
        "plaintext_key": plaintext,
    }


async def list_api_keys(
    db: aiosqlite.Connection, *, principal_id: int | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    if principal_id is not None:
        rows = await fetchall(
            db,
            "SELECT * FROM api_keys WHERE principal_id = ? ORDER BY id DESC LIMIT ?",
            (principal_id, limit),
        )
    else:
        rows = await fetchall(
            db, "SELECT * FROM api_keys ORDER BY id DESC LIMIT ?", (limit,)
        )
    return [dict(r) for r in rows]


async def revoke_api_key(db: aiosqlite.Connection, key_id: int) -> bool:
    cursor = await db.execute(
        "UPDATE api_keys SET revoked_at = datetime('now') "
        "WHERE id = ? AND revoked_at IS NULL",
        (key_id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def resolve_api_key(
    db: aiosqlite.Connection, plaintext_key: str
) -> TokenIdentity | None:
    """Look up an API key by hash, return a TokenIdentity or None."""
    key_hash = hash_api_key(plaintext_key)
    rows = await fetchall(
        db,
        "SELECT ak.*, p.username, p.kind, p.status "
        "FROM api_keys ak "
        "JOIN principals p ON ak.principal_id = p.id "
        "WHERE ak.key_hash = ? AND ak.revoked_at IS NULL",
        (key_hash,),
    )
    if not rows:
        return None
    row = dict(rows[0])
    if row["status"] != "active":
        return None
    if row.get("expires_at"):
        try:
            exp = datetime.fromisoformat(row["expires_at"])
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < datetime.now(timezone.utc):
                return None
        except (ValueError, TypeError):
            pass

    # Best-effort last_used_at update
    try:
        await db.execute(
            "UPDATE api_keys SET last_used_at = datetime('now') WHERE id = ?",
            (row["id"],),
        )
        await db.execute(
            "UPDATE principals SET last_seen_at = datetime('now') WHERE id = ?",
            (row["principal_id"],),
        )
        await db.commit()
    except Exception as exc:
        log.debug("best-effort api key last_used update failed: %s", exc)

    perms = await get_principal_permissions(db, row["principal_id"])
    role = await get_effective_role(db, row["principal_id"])
    return TokenIdentity(
        username=row["username"],
        role=role,
        principal_id=row["principal_id"],
        permissions=perms,
        auth_source="db_api_key",
        api_key_id=row["id"],
    )


async def _revoke_all_keys(db: aiosqlite.Connection, principal_id: int) -> None:
    await db.execute(
        "UPDATE api_keys SET revoked_at = datetime('now') "
        "WHERE principal_id = ? AND revoked_at IS NULL",
        (principal_id,),
    )


# ---------------------------------------------------------------------------
# Browser sessions
# ---------------------------------------------------------------------------


async def create_browser_session(
    db: aiosqlite.Connection,
    principal_id: int,
    *,
    ip_hash: str = "",
    user_agent: str = "",
    max_age_seconds: int = 30 * 24 * 3600,
) -> str:
    """Create a new browser session. Returns the plaintext session token."""
    plaintext, session_hash = generate_session_token()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=max_age_seconds)
    ).isoformat()
    await db.execute(
        "INSERT INTO browser_sessions (principal_id, session_hash, expires_at, ip_hash, user_agent) "
        "VALUES (?, ?, ?, ?, ?)",
        (principal_id, session_hash, expires_at, ip_hash, user_agent),
    )
    await db.commit()
    return plaintext


async def resolve_browser_session(
    db: aiosqlite.Connection, session_token: str
) -> TokenIdentity | None:
    session_hash = hash_session_token(session_token)
    rows = await fetchall(
        db,
        "SELECT bs.*, p.username, p.kind, p.status "
        "FROM browser_sessions bs "
        "JOIN principals p ON bs.principal_id = p.id "
        "WHERE bs.session_hash = ? AND bs.revoked_at IS NULL",
        (session_hash,),
    )
    if not rows:
        return None
    row = dict(rows[0])
    if row["status"] != "active":
        return None
    try:
        exp = datetime.fromisoformat(row["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < datetime.now(timezone.utc):
            return None
    except (ValueError, TypeError):
        return None

    try:
        await db.execute(
            "UPDATE browser_sessions SET last_seen_at = datetime('now') WHERE id = ?",
            (row["id"],),
        )
        await db.execute(
            "UPDATE principals SET last_seen_at = datetime('now') WHERE id = ?",
            (row["principal_id"],),
        )
        await db.commit()
    except Exception as exc:
        log.debug("best-effort browser session last_seen update failed: %s", exc)

    perms = await get_principal_permissions(db, row["principal_id"])
    role = await get_effective_role(db, row["principal_id"])
    return TokenIdentity(
        username=row["username"],
        role=role,
        principal_id=row["principal_id"],
        permissions=perms,
        auth_source="db_session",
    )


async def revoke_browser_session(db: aiosqlite.Connection, session_token: str) -> bool:
    """Revoke ONE browser session by its plaintext token. Returns whether a
    live row was closed.

    Deliberately not ``_revoke_all_sessions``: logging out of a laptop must
    not sign the same person out of their phone. The narrow scope is the
    point, not an optimisation (#368).

    Idempotent — a token that is unknown, already revoked, or expired closes
    nothing and returns False, so a double logout is not an error.
    """
    session_hash = hash_session_token(session_token)
    cursor = await db.execute(
        "UPDATE browser_sessions SET revoked_at = datetime('now') "
        "WHERE session_hash = ? AND revoked_at IS NULL",
        (session_hash,),
    )
    await db.commit()
    return bool(cursor.rowcount)


async def _revoke_all_sessions(db: aiosqlite.Connection, principal_id: int) -> None:
    await db.execute(
        "UPDATE browser_sessions SET revoked_at = datetime('now') "
        "WHERE principal_id = ? AND revoked_at IS NULL",
        (principal_id,),
    )


# ---------------------------------------------------------------------------
# Password login
# ---------------------------------------------------------------------------


async def authenticate_password(
    db: aiosqlite.Connection, username: str, password: str
) -> int | None:
    """Verify username + password. Returns principal_id on success, None on failure."""
    rows = await fetchall(
        db,
        "SELECT p.id, p.status, pc.password_hash, pc.failed_attempts, pc.locked_until "
        "FROM principals p "
        "JOIN password_credentials pc ON p.id = pc.principal_id "
        "WHERE p.username = ?",
        (username,),
    )
    if not rows:
        return None
    row = dict(rows[0])
    if row["status"] != "active":
        return None
    if row.get("locked_until"):
        try:
            locked = datetime.fromisoformat(row["locked_until"])
            if locked.tzinfo is None:
                locked = locked.replace(tzinfo=timezone.utc)
            if locked > datetime.now(timezone.utc):
                return None
        except (ValueError, TypeError):
            pass

    if not verify_password(row["password_hash"], password):
        await db.execute(
            "UPDATE password_credentials SET failed_attempts = failed_attempts + 1 WHERE principal_id = ?",
            (row["id"],),
        )
        failed = row.get("failed_attempts", 0) + 1
        if failed >= 5:
            lock_until = (
                datetime.now(timezone.utc) + timedelta(minutes=15)
            ).isoformat()
            await db.execute(
                "UPDATE password_credentials SET locked_until = ? WHERE principal_id = ?",
                (lock_until, row["id"]),
            )
            await db.execute(
                "UPDATE principals SET status = 'locked', updated_at = datetime('now') WHERE id = ?",
                (row["id"],),
            )
        await db.commit()
        return None

    await db.execute(
        "UPDATE password_credentials SET failed_attempts = 0, locked_until = NULL, "
        "last_login_at = datetime('now') WHERE principal_id = ?",
        (row["id"],),
    )
    await db.commit()
    return row["id"]


async def set_password(
    db: aiosqlite.Connection, principal_id: int, password: str
) -> None:
    pw_hash = hash_password(password)
    existing = await fetchall(
        db,
        "SELECT 1 FROM password_credentials WHERE principal_id = ?",
        (principal_id,),
    )
    if existing:
        await db.execute(
            "UPDATE password_credentials SET password_hash = ?, "
            "password_changed_at = datetime('now'), must_rotate = 0, "
            "failed_attempts = 0, locked_until = NULL "
            "WHERE principal_id = ?",
            (pw_hash, principal_id),
        )
    else:
        await db.execute(
            "INSERT INTO password_credentials (principal_id, password_hash) VALUES (?, ?)",
            (principal_id, pw_hash),
        )
    await db.commit()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


async def bootstrap_admin(
    db: aiosqlite.Connection,
    *,
    username: str,
    password: str,
    display_name: str = "",
    email: str = "",
) -> dict[str, Any]:
    """Create the first super_admin. Raises if one already exists."""
    if await has_active_admin(db):
        raise ValueError("admin already exists; bootstrap not allowed")

    principal = await create_principal(
        db,
        kind="human",
        username=username,
        display_name=display_name or username,
        email=email,
        password=password,
        role_slug="super_admin",
    )
    await write_audit(
        db,
        actor_id=principal["id"],
        action="bootstrap",
        target_type="principal",
        target_id=str(principal["id"]),
        summary=f"Admin bootstrap: created super_admin {username!r}",
    )
    return principal


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


async def admin_summary(db: aiosqlite.Connection) -> dict[str, Any]:
    from hub import config

    active_users = await fetchall(
        db, "SELECT COUNT(*) FROM principals WHERE kind = 'human' AND status = 'active'"
    )
    disabled_users = await fetchall(
        db,
        "SELECT COUNT(*) FROM principals WHERE kind = 'human' AND status != 'active'",
    )
    active_agents = await fetchall(
        db, "SELECT COUNT(*) FROM principals WHERE kind = 'agent' AND status = 'active'"
    )
    active_keys = await fetchall(
        db, "SELECT COUNT(*) FROM api_keys WHERE revoked_at IS NULL"
    )
    locked_users = await fetchall(
        db, "SELECT COUNT(*) FROM principals WHERE status = 'locked'"
    )
    active_sessions = await fetchall(
        db,
        "SELECT COUNT(*) FROM browser_sessions "
        "WHERE revoked_at IS NULL AND expires_at > datetime('now')",
    )
    expiring_7d = await fetchall(
        db,
        "SELECT COUNT(*) FROM api_keys "
        "WHERE revoked_at IS NULL AND expires_at IS NOT NULL "
        "AND expires_at > datetime('now') "
        "AND expires_at <= datetime('now', '+7 days')",
    )
    has_admin = await has_active_admin(db)
    audit_rows = await list_audit(db, limit=5)

    return {
        "active_users": active_users[0][0],
        "disabled_users": disabled_users[0][0],
        "active_agents": active_agents[0][0],
        "active_api_keys": active_keys[0][0],
        "active_sessions": active_sessions[0][0],
        "expiring_keys_7d": expiring_7d[0][0],
        "locked_users": locked_users[0][0],
        "recent_audit": audit_rows,
        "env_tokens_active": bool(config.HUB_TOKENS),
        "admin_bootstrap_required": not has_admin,
    }
