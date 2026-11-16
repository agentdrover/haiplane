"""Internal persistence contract for a future verified GitHub import flow.

These writers are not public APIs. Callers must authenticate/authorize and
verify GitHub delegation before entering them. No legacy route calls them.
"""

from contextlib import asynccontextmanager
from uuid import uuid4

import aiosqlite

from hub.db import fetchall, inserted_id, write_transaction


class OwnershipConflict(ValueError):
    """The observed project authorization epoch has changed."""


@asynccontextmanager
async def _atomic(db: aiosqlite.Connection):
    """Roll back deferred constraint failures too; preserve a caller's work."""
    owns = not db.in_transaction
    try:
        async with write_transaction(db):
            await db.execute("SAVEPOINT private_project_write")
            try:
                yield
            except BaseException:
                await db.execute("ROLLBACK TO private_project_write")
                await db.execute("RELEASE private_project_write")
                raise
            else:
                await db.execute("RELEASE private_project_write")
    except BaseException:
        # write_transaction does not catch an exception from its own commit.
        if owns and db.in_transaction:
            await db.rollback()
        raise


async def _check_owner(db: aiosqlite.Connection, owner_id: int, link_id: int) -> None:
    rows = await fetchall(
        db,
        "SELECT 1 FROM github_account_links l JOIN principals p ON p.id=l.principal_id "
        "WHERE l.id=? AND l.principal_id=? AND l.status='active' "
        "AND p.kind='human' AND p.status='active'",
        (link_id, owner_id),
    )
    if not rows:
        raise ValueError("an active human owner's account link is required")


async def create_private_project(
    db: aiosqlite.Connection,
    *,
    owner_id: int,
    account_link_id: int,
    repository_id: int,
    installation_id: int,
    name: str,
    full_name: str,
) -> int:
    """Atomically persist a verified import; identity is owner/repository ID.

    Retrying the same intent returns its ID. A different intent conflicts.
    Workspace is a server-generated relative allocation key, not a user path;
    the isolated executor will resolve it under its configured workspace root.
    """
    if not name.strip() or not full_name.strip():
        raise ValueError("project and repository names are required")
    async with _atomic(db):
        await _check_owner(db, owner_id, account_link_id)
        previous = await fetchall(
            db,
            "SELECT b.*, p.name FROM project_repository_bindings b "
            "JOIN projects p ON p.id=b.project_id WHERE b.owner_id=? AND b.repository_id=?",
            (owner_id, repository_id),
        )
        if previous:
            row = previous[0]
            if (
                row["account_link_id"],
                row["installation_id"],
                row["name"],
                row["full_name"],
            ) != (account_link_id, installation_id, name, full_name):
                raise ValueError("import intent conflicts with existing binding")
            return int(row["project_id"])
        slug = "p-" + uuid4().hex
        cur = await db.execute(
            "INSERT INTO projects(slug,name,workspace_path) VALUES (?,?,?)",
            (slug, name, "private/" + slug),
        )
        pid = inserted_id(cur)
        await db.execute(
            "INSERT INTO private_project_security(project_id,owner_id) VALUES (?,?)",
            (pid, owner_id),
        )
        await db.execute(
            "INSERT INTO project_memberships(project_id,principal_id,role) VALUES (?,?,'owner')",
            (pid, owner_id),
        )
        await db.execute(
            "INSERT INTO project_repository_bindings "
            "(project_id,owner_id,account_link_id,repository_id,installation_id,full_name) "
            "VALUES (?,?,?,?,?,?)",
            (pid, owner_id, account_link_id, repository_id, installation_id, full_name),
        )
        return pid


async def transfer_ownership(
    db: aiosqlite.Connection,
    *,
    project_id: int,
    owner_id: int,
    account_link_id: int,
    expected_epoch: int,
) -> int:
    """Persist an authorized, reverified transfer under an epoch CAS.

    Old ownership conveys no remaining membership. Invitations bound to the
    previous epoch are revoked. GitHub revalidation is the caller's duty.
    """
    async with _atomic(db):
        rows = await fetchall(
            db,
            "SELECT * FROM private_project_security WHERE project_id=?",
            (project_id,),
        )
        if not rows or rows[0]["auth_epoch"] != expected_epoch:
            raise OwnershipConflict("project authorization changed")
        await _check_owner(db, owner_id, account_link_id)
        await db.execute(
            "DELETE FROM project_memberships WHERE project_id=? AND (role='owner' OR principal_id=?)",
            (project_id, owner_id),
        )
        await db.execute(
            "INSERT INTO project_memberships(project_id,principal_id,role) VALUES (?,?,'owner')",
            (project_id, owner_id),
        )
        await db.execute(
            "UPDATE private_project_security SET owner_id=?, auth_epoch=auth_epoch+1 WHERE project_id=?",
            (owner_id, project_id),
        )
        await db.execute(
            "UPDATE project_repository_bindings SET owner_id=?,account_link_id=? WHERE project_id=?",
            (owner_id, account_link_id, project_id),
        )
        await db.execute(
            "UPDATE project_invitations SET status='revoked' WHERE project_id=? AND status='pending'",
            (project_id,),
        )
        return expected_epoch + 1
