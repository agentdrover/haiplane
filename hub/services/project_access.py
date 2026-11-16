"""Fail-closed decision contract; surface enforcement ships separately.

No global role bypass and no legacy default-project substitution. Decisions
are snapshots, not bearer grants: each operation must recheck its epoch.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite

from hub.config import TokenIdentity
from hub.db import fetchall


SELF_SERVICE_CAPABILITIES = frozenset(
    {
        "github.self.connect",
        "projects.self.create",
        "projects.self.manage",
    }
)
READ_PERMISSIONS = frozenset({"tasks.read", "verdict.evidence.read"})
MEMBER_PERMISSIONS = READ_PERMISSIONS | frozenset(
    {
        "tasks.create",
        "tasks.refine",
        "tasks.update",
        "tasks.agent_report",
        "tasks.human_gate",
        "tasks.decision",
        "tasks.archive",
    }
)
OWNER_PERMISSIONS = MEMBER_PERMISSIONS | frozenset(
    {"tasks.delete", "projects.self.manage"}
)
HUMAN_ROLES = frozenset(
    {
        "human",
        "admin",
        "super_admin",
        "operator",
        "developer",
        "viewer",
        "security_admin",
    }
)


def _human_actor(identity: TokenIdentity, principal_kind: str) -> bool:
    # Chat-pair can act under its issuing human's principal ID.
    return (
        principal_kind == "human"
        and identity.role in HUMAN_ROLES
        and identity.chat_pair_kind is None
        and identity.chat_pair_task_id is None
    )


def _holds(identity: TokenIdentity, permission: str) -> bool:
    # Stored role unions must not lift ceilings on watcher, CI or host keys.
    return permission in identity.permissions and identity.has_permission(permission)


@dataclass(frozen=True)
class Delegation:
    """Trusted server-issued agent scope, never parsed directly from a request."""

    principal_id: int
    project_id: int
    task_id: int
    auth_epoch: int
    permissions: frozenset[str]
    expires_at: datetime


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    project_id: int | None = None
    auth_epoch: int | None = None


async def self_service_capabilities(
    db: aiosqlite.Connection, identity: TokenIdentity
) -> frozenset[str]:
    """Capabilities require an active DB human; global roles alone grant none."""
    rows = await fetchall(
        db, "SELECT kind,status FROM principals WHERE id=?", (identity.principal_id,)
    )
    if not rows or rows[0]["status"] != "active" or identity.scopes_damaged:
        return frozenset()
    if not _human_actor(identity, rows[0]["kind"]):
        return frozenset()
    return frozenset(cap for cap in SELF_SERVICE_CAPABILITIES if _holds(identity, cap))


async def _task_project(db: aiosqlite.Connection, task_id: int) -> int | None:
    seen: set[int] = set()
    current: int | None = task_id
    while current is not None and current not in seen and len(seen) < 20:
        seen.add(current)
        rows = await fetchall(
            db, "SELECT parent_id,project_id FROM tasks WHERE id=?", (current,)
        )
        if not rows:
            return None
        if rows[0]["project_id"] is not None:
            return int(rows[0]["project_id"])
        current = rows[0]["parent_id"]
    return None


def _delegation_allows(
    delegation: Delegation | None,
    identity: TokenIdentity,
    pid: int,
    task_id: int | None,
    epoch: int,
    permission: str,
) -> bool:
    if delegation is None or delegation.expires_at.tzinfo is None:
        return False
    return (
        delegation.principal_id == identity.principal_id
        and delegation.project_id == pid
        and delegation.task_id == task_id
        and delegation.auth_epoch == epoch
        and permission in delegation.permissions
        and delegation.expires_at > datetime.now(timezone.utc)
    )


async def decide(
    db: aiosqlite.Connection,
    identity: TokenIdentity,
    *,
    permission: str,
    project_id: int | None = None,
    task_id: int | None = None,
    delegation: Delegation | None = None,
    expected_epoch: int | None = None,
) -> AccessDecision:
    """System permission ∩ project role ∩ agent delegation; unknown means deny.

    Humans require membership. Agents/services require both membership and a
    task-bound, current server delegation; human gates remain human-only.
    """
    if task_id is not None:
        resolved = await _task_project(db, task_id)
        if resolved is None or (project_id is not None and resolved != project_id):
            return AccessDecision(False)
        project_id = resolved
    if (
        project_id is None
        or not _holds(identity, permission)
        or identity.scopes_damaged
    ):
        return AccessDecision(False)
    rows = await fetchall(
        db,
        "SELECT s.auth_epoch,m.role,p.kind,pr.slug FROM private_project_security s "
        "JOIN projects pr ON pr.id=s.project_id "
        "JOIN project_memberships m ON m.project_id=s.project_id AND m.principal_id=? "
        "JOIN principals p ON p.id=m.principal_id "
        "WHERE s.project_id=? AND p.status='active' AND pr.status='active' AND pr.archived=0",
        (identity.principal_id, project_id),
    )
    if not rows:
        return AccessDecision(False)
    row = rows[0]
    if identity.scopes is not None and row["slug"] not in identity.scopes:
        return AccessDecision(False)
    if identity.chat_pair_task_id is not None and task_id != identity.chat_pair_task_id:
        return AccessDecision(False)
    epoch = int(row["auth_epoch"])
    if expected_epoch is not None and epoch != expected_epoch:
        return AccessDecision(False)
    permissions = {
        "viewer": READ_PERMISSIONS,
        "member": MEMBER_PERMISSIONS,
        "owner": OWNER_PERMISSIONS,
    }[row["role"]]
    if permission not in permissions:
        return AccessDecision(False)
    if not _human_actor(identity, row["kind"]):
        if permission in {"tasks.human_gate", "tasks.decision", "projects.self.manage"}:
            return AccessDecision(False)
        if not _delegation_allows(
            delegation, identity, project_id, task_id, epoch, permission
        ):
            return AccessDecision(False)
    return AccessDecision(True, project_id, epoch)
