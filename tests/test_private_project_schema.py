"""Private-project foundation; no public route enables this mode yet."""

import asyncio
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from hub import db as database
from hub.config import TokenIdentity
from hub.repository_private_projects import (
    OwnershipConflict,
    create_private_project,
    transfer_ownership,
)
from hub.services.project_access import Delegation, decide, self_service_capabilities


async def human(db, name):
    cur = await db.execute(
        "INSERT INTO principals(kind, username) VALUES ('human', ?)", (name,)
    )
    return cur.lastrowid


async def account(db, owner, gh_id):
    cur = await db.execute(
        "INSERT INTO github_account_links(principal_id, github_user_id) VALUES (?, ?)",
        (owner, gh_id),
    )
    return cur.lastrowid


async def setup_owners(db):
    alice, bob = await human(db, "alice"), await human(db, "bob")
    a, b = await account(db, alice, 101), await account(db, bob, 102)
    await db.commit()
    return alice, bob, a, b


def identity(pid, role="developer", permissions=("tasks.read",)):
    return TokenIdentity(
        "test", role, principal_id=pid, permissions=frozenset(permissions)
    )


async def project(db, owner, link, repo_id=501):
    return await create_private_project(
        db,
        owner_id=owner,
        account_link_id=link,
        repository_id=repo_id,
        installation_id=42,
        name="same-name",
        full_name="org/same-name",
    )


async def test_owner_and_same_named_projects_are_isolated(db):
    alice, bob, a, b = await setup_owners(db)
    p1, p2 = await project(db, alice, a), await project(db, bob, b)
    assert p1 != p2
    rows = await database.fetchall(
        db, "SELECT * FROM projects WHERE id IN (?, ?)", (p1, p2)
    )
    assert rows[0]["name"] == rows[1]["name"] == "same-name"
    assert rows[0]["slug"] != rows[1]["slug"]
    assert rows[0]["workspace_path"] != rows[1]["workspace_path"]
    assert "same-name" not in rows[0]["slug"]
    for pid, owner in ((p1, alice), (p2, bob)):
        members = await database.fetchall(
            db, "SELECT * FROM project_memberships WHERE project_id=?", (pid,)
        )
        assert [(m["principal_id"], m["role"]) for m in members] == [(owner, "owner")]
    assert await project(db, alice, a) == p1
    assert not (
        await decide(db, identity(alice), project_id=p2, permission="tasks.read")
    ).allowed


async def test_global_role_does_not_grant_project_access(db):
    alice, bob, a, _ = await setup_owners(db)
    pid = await project(db, alice, a)
    for role in ("operator", "admin", "super_admin"):
        result = await decide(
            db, identity(bob, role), project_id=pid, permission="tasks.read"
        )
        assert not result.allowed
    assert (
        await decide(db, identity(alice), project_id=pid, permission="tasks.read")
    ).allowed
    assert not (
        await decide(db, identity(alice), project_id=pid, permission="tasks.human_gate")
    ).allowed


async def test_owner_binding_constraints_survive_concurrent_migration(db, db_dsn):
    alice, bob, a, b = await setup_owners(db)
    pid = await project(db, alice, a)
    other = await database.connect(db_dsn)
    try:
        await asyncio.gather(database._migrate(db), database._migrate(other))
        results = await asyncio.gather(
            transfer_ownership(
                db, project_id=pid, owner_id=bob, account_link_id=b, expected_epoch=1
            ),
            transfer_ownership(
                other, project_id=pid, owner_id=bob, account_link_id=b, expected_epoch=1
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(r, OwnershipConflict) for r in results) == 1
        assert sum(r == 2 for r in results) == 1
        security = (
            await database.fetchall(
                db, "SELECT * FROM private_project_security WHERE project_id=?", (pid,)
            )
        )[0]
        assert (security["owner_id"], security["auth_epoch"]) == (bob, 2)
        owners = await database.fetchall(
            db,
            "SELECT principal_id FROM project_memberships WHERE project_id=? AND role='owner'",
            (pid,),
        )
        assert [r[0] for r in owners] == [bob]
        binding = (
            await database.fetchall(
                db,
                "SELECT * FROM project_repository_bindings WHERE project_id=?",
                (pid,),
            )
        )[0]
        assert (binding["owner_id"], binding["account_link_id"]) == (bob, b)
        assert not await database.fetchall(db, "PRAGMA foreign_key_check")
        for sql, args in (
            (
                "DELETE FROM project_memberships WHERE project_id=? AND role='owner'",
                (pid,),
            ),
            (
                "UPDATE private_project_security SET owner_id=? WHERE project_id=?",
                (alice, pid),
            ),
            (
                "UPDATE project_repository_bindings SET account_link_id=? WHERE project_id=?",
                (a, pid),
            ),
            (
                "INSERT INTO project_memberships(project_id, principal_id, role) VALUES (?, ?, 'owner')",
                (pid, alice),
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                async with database.write_transaction(db):
                    await db.execute(sql, args)
            await db.rollback()
        with pytest.raises(ValueError):
            await transfer_ownership(
                db, project_id=pid, owner_id=alice, account_link_id=b, expected_epoch=2
            )
        assert (
            await decide(db, identity(bob), project_id=pid, permission="tasks.read")
        ).allowed
        assert not (
            await decide(db, identity(alice), project_id=pid, permission="tasks.read")
        ).allowed
    finally:
        await other.close()


async def test_legacy_unassigned_and_task_resolution_fail_closed(db):
    alice, bob, a, _ = await setup_owners(db)
    pid = await project(db, alice, a)
    cur = await db.execute(
        "INSERT INTO tasks(title, task_type, project_id) VALUES ('epic', 'epic', ?)",
        (pid,),
    )
    root = cur.lastrowid
    child = await db.execute(
        "INSERT INTO tasks(title, parent_id) VALUES ('child', ?)", (root,)
    )
    orphan = await db.execute("INSERT INTO tasks(title) VALUES ('orphan')")
    await db.commit()
    assert (
        await decide(
            db, identity(alice), task_id=child.lastrowid, permission="tasks.read"
        )
    ).allowed
    assert not (
        await decide(
            db,
            identity(alice),
            task_id=child.lastrowid,
            project_id=pid + 1,
            permission="tasks.read",
        )
    ).allowed
    for tid in (orphan.lastrowid, 999999):
        assert not (
            await decide(db, identity(alice), task_id=tid, permission="tasks.read")
        ).allowed
    legacy = (
        await db.execute("INSERT INTO projects(slug,name) VALUES ('default','Legacy')")
    ).lastrowid
    await db.commit()
    assert not (
        await decide(
            db,
            identity(alice, "super_admin"),
            project_id=legacy,
            permission="tasks.read",
        )
    ).allowed


async def test_system_capabilities_do_not_grant_credentials_or_viewer_escalation(db):
    alice, _, _, _ = await setup_owners(db)
    for role in ("admin", "operator", "developer"):
        perms = {
            r[0]
            for r in await database.fetchall(
                db,
                "SELECT permission FROM role_permissions rp JOIN roles r ON r.id=rp.role_id WHERE r.slug=?",
                (role,),
            )
        }
        caps = await self_service_capabilities(db, identity(alice, role, perms))
        assert {
            "github.self.connect",
            "projects.self.create",
            "projects.self.manage",
        } <= caps
        if role != "admin":
            assert "admin.credentials.write" not in perms
    assert not await self_service_capabilities(db, identity(alice, "viewer"))
    await db.execute("UPDATE principals SET kind='agent' WHERE id=?", (alice,))
    await db.commit()
    assert not await self_service_capabilities(
        db, identity(alice, "admin", ("projects.self.create",))
    )


async def test_agents_require_membership_and_current_task_delegation(db):
    alice, bob, a, b = await setup_owners(db)
    pid, foreign = await project(db, alice, a), await project(db, bob, b, 502)
    cur = await db.execute(
        "INSERT INTO principals(kind,username) VALUES ('agent','worker')"
    )
    agent = cur.lastrowid
    task = await db.execute(
        "INSERT INTO tasks(title,project_id) VALUES ('work',?)", (pid,)
    )
    await db.commit()
    ident = identity(agent, "agent", ("tasks.read", "tasks.human_gate"))
    grant = Delegation(
        agent,
        pid,
        task.lastrowid,
        1,
        frozenset({"tasks.read", "tasks.human_gate"}),
        datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert not (
        await decide(
            db, ident, task_id=task.lastrowid, permission="tasks.read", delegation=grant
        )
    ).allowed
    await db.execute(
        "INSERT INTO project_memberships(project_id,principal_id,role) VALUES (?,?,'member')",
        (pid, agent),
    )
    await db.commit()
    assert (
        await decide(
            db, ident, task_id=task.lastrowid, permission="tasks.read", delegation=grant
        )
    ).allowed
    assert not (
        await decide(db, ident, task_id=task.lastrowid, permission="tasks.read")
    ).allowed
    for bad in (
        replace(grant, project_id=foreign),
        replace(grant, task_id=task.lastrowid + 1),
        replace(grant, auth_epoch=2),
        replace(grant, principal_id=bob),
        replace(grant, expires_at=datetime.now(timezone.utc) - timedelta(hours=1)),
        replace(grant, expires_at=datetime.now()),
        replace(grant, permissions=frozenset()),
    ):
        assert not (
            await decide(
                db,
                ident,
                task_id=task.lastrowid,
                permission="tasks.read",
                delegation=bad,
            )
        ).allowed
    assert not (
        await decide(
            db, ident, project_id=foreign, permission="tasks.read", delegation=grant
        )
    ).allowed
    assert not (
        await decide(
            db,
            ident,
            task_id=task.lastrowid,
            permission="tasks.human_gate",
            delegation=grant,
        )
    ).allowed
    await transfer_ownership(
        db, project_id=pid, owner_id=bob, account_link_id=b, expected_epoch=1
    )
    assert not (
        await decide(
            db, ident, task_id=task.lastrowid, permission="tasks.read", delegation=grant
        )
    ).allowed


async def test_key_scope_viewer_and_disabled_identity_are_intersections(db):
    alice, bob, a, _ = await setup_owners(db)
    pid = await project(db, alice, a)
    await db.execute(
        "INSERT INTO project_memberships(project_id,principal_id,role) VALUES (?,?,'viewer')",
        (pid, bob),
    )
    await db.commit()
    viewer = identity(bob, "operator", ("tasks.read", "tasks.update"))
    assert (await decide(db, viewer, project_id=pid, permission="tasks.read")).allowed
    assert not (
        await decide(db, viewer, project_id=pid, permission="tasks.update")
    ).allowed
    owner = identity(alice)
    owner.scopes = ("other-project",)
    assert not (
        await decide(db, owner, project_id=pid, permission="tasks.read")
    ).allowed
    owner.scopes = None
    owner.scopes_damaged = True
    assert not (
        await decide(db, owner, project_id=pid, permission="tasks.read")
    ).allowed
    await db.execute("UPDATE principals SET status='disabled' WHERE id=?", (bob,))
    await db.commit()
    assert not (
        await decide(db, viewer, project_id=pid, permission="tasks.read")
    ).allowed


async def test_upgrade_keeps_legacy_projects_unassigned_and_replay_is_safe(
    db, monkeypatch, tmp_path
):
    # Build the exact pre-feature schema, then upgrade it twice.
    old_steps = [
        (name, sql) for name, sql in database._MIGRATIONS if not name.endswith("_2350")
    ]
    fresh = await database.connect(str(tmp_path / "upgrade.db"))
    try:
        await fresh.executescript(database._SCHEMA)
        with monkeypatch.context() as patch:
            patch.setattr(database, "_MIGRATIONS", old_steps)
            await database._migrate(fresh)
        await fresh.execute(
            "INSERT INTO projects(slug,name) VALUES ('legacy','Legacy')"
        )
        await fresh.commit()
        second = await database.connect(str(tmp_path / "upgrade.db"))
        try:
            await asyncio.gather(database._migrate(fresh), database._migrate(second))
        finally:
            await second.close()
        await database._migrate(fresh)
        assert not await database.fetchall(
            fresh, "SELECT * FROM private_project_security"
        )
        assert not await database.fetchall(fresh, "SELECT * FROM project_memberships")
        alice, _, a, _ = await setup_owners(fresh)
        pid = await project(fresh, alice, a)
        assert (
            await decide(
                fresh, identity(alice), project_id=pid, permission="tasks.read"
            )
        ).allowed
        assert not await database.fetchall(fresh, "PRAGMA foreign_key_check")
    finally:
        await fresh.close()


async def test_nested_failed_import_preserves_callers_work(db):
    alice, bob, a, b = await setup_owners(db)
    pid = await project(db, alice, a)
    await db.execute("UPDATE principals SET display_name='kept' WHERE id=?", (bob,))
    with pytest.raises(sqlite3.IntegrityError):
        await create_private_project(
            db,
            owner_id=bob,
            account_link_id=b,
            repository_id=-1,
            installation_id=42,
            name="bad",
            full_name="org/bad",
        )
    await db.commit()
    assert (
        await database.fetchall(
            db, "SELECT display_name FROM principals WHERE id=?", (bob,)
        )
    )[0][0] == "kept"
    assert (
        len(await database.fetchall(db, "SELECT * FROM private_project_security")) == 1
    )
    assert (
        await decide(db, identity(alice), project_id=pid, permission="tasks.read")
    ).allowed


@pytest.mark.parametrize("role", ["watcher", "ci_reader", "ci_auditor", "slot_runner"])
async def test_special_key_permission_ceiling_is_preserved(db, role):
    alice, _, a, _ = await setup_owners(db)
    pid = await project(db, alice, a)
    task = await db.execute(
        "INSERT INTO tasks(title,project_id) VALUES ('scoped',?)", (pid,)
    )
    await db.commit()
    key = identity(
        alice,
        role,
        ("tasks.read", "tasks.update", "projects.self.create", "projects.self.manage"),
    )
    grant = Delegation(
        alice,
        pid,
        task.lastrowid,
        1,
        frozenset({"tasks.update"}),
        datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert not key.has_permission("tasks.update")
    assert not (
        await decide(
            db, key, task_id=task.lastrowid, permission="tasks.update", delegation=grant
        )
    ).allowed
    assert not await self_service_capabilities(db, key)


@pytest.mark.parametrize(
    "role", ["agent", "steward", "watcher", "ci_reader", "ci_auditor", "slot_runner"]
)
async def test_machine_key_on_human_principal_still_needs_delegation(db, role):
    alice, _, a, _ = await setup_owners(db)
    pid = await project(db, alice, a)
    key = identity(
        alice, role, ("tasks.read", "tasks.human_gate", "projects.self.create")
    )
    assert not (await decide(db, key, project_id=pid, permission="tasks.read")).allowed
    assert not (
        await decide(db, key, project_id=pid, permission="tasks.human_gate")
    ).allowed
    assert not await self_service_capabilities(db, key)


async def test_chat_pair_key_on_human_principal_is_not_a_human_actor(db):
    alice, _, a, _ = await setup_owners(db)
    pid = await project(db, alice, a)
    task = await db.execute(
        "INSERT INTO tasks(title,project_id) VALUES ('pair',?)", (pid,)
    )
    await db.commit()
    key = identity(
        alice, "admin", ("tasks.read", "tasks.human_gate", "projects.self.create")
    )
    key.chat_pair_kind = "implementer"
    key.chat_pair_task_id = task.lastrowid
    assert not (
        await decide(db, key, task_id=task.lastrowid, permission="tasks.read")
    ).allowed
    grant = Delegation(
        alice,
        pid,
        task.lastrowid,
        1,
        frozenset({"tasks.read", "tasks.human_gate"}),
        datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert (
        await decide(
            db, key, task_id=task.lastrowid, permission="tasks.read", delegation=grant
        )
    ).allowed
    assert not (
        await decide(
            db,
            key,
            task_id=task.lastrowid,
            permission="tasks.human_gate",
            delegation=grant,
        )
    ).allowed
    assert not await self_service_capabilities(db, key)
