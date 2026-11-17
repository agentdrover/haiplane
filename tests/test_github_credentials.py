"""The credential boundary uses synthetic secrets and an injected provider."""

import asyncio
import json
import os
from dataclasses import replace

import pytest

from hub import db as database
from hub.config import TokenIdentity
from hub.integrations.github_credentials import (
    CredentialContext,
    CredentialError,
    OwnerProof,
    Secret,
    TokenResponse,
    command_environment,
    credential_origin,
)
from hub.repository_private_projects import create_private_project
from hub.services.github_connections import GithubConnections
from hub.services.github_vault import GithubVault


PERMS = (("contents", "read"), ("metadata", "read"))


class Provider:
    def __init__(self):
        self.refreshed = 0
        self.minted = []
        self.failure = False

    async def refresh_user(self, app_id, app_secret, old):
        self.refreshed += 1
        await asyncio.sleep(0)
        return Secret(
            json.dumps(
                {
                    "access_token": "synthetic-user-new",
                    "refresh_token": "synthetic-refresh-new",
                }
            )
        ), 9000.0

    async def verify_owner(self, context, grant, user_secret, app_secret):
        if self.failure:
            raise RuntimeError(
                "provider leaked synthetic-user-new and synthetic-install-token"
            )
        return OwnerProof(
            grant.github_user_id,
            context.repository_id,
            grant.installation_id,
            True,
            grant.installation_permissions,
        )

    async def mint(self, context, grant, app_secret):
        self.minted.append(context)
        await asyncio.sleep(0)
        return TokenResponse(
            Secret(f"synthetic-install-token-{context.project_id}"), 4600.0
        )


async def setup(db, db_dsn, tmp_path):
    def clock():
        return 1000.0

    vault = GithubVault(db_dsn, tmp_path / "keys" / "github.keys", clock=clock)
    await vault.bootstrap()
    provider = Provider()
    service = GithubConnections(db_dsn, vault, provider, clock=clock)
    await service.configure_app(
        1,
        "client-id",
        Secret(
            '{"private_key":"synthetic-key","client_secret":"synthetic-client-secret"}'
        ),
        dict(PERMS),
    )
    projects, identities, links = [], [], []
    for n in (1, 2):
        cur = await db.execute(
            "INSERT INTO principals(kind,username) VALUES ('human',?)", (f"owner{n}",)
        )
        owner = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO github_account_links(principal_id,github_user_id) VALUES (?,?)",
            (owner, 100 + n),
        )
        link = cur.lastrowid
        await db.commit()
        pid = await create_private_project(
            db,
            owner_id=owner,
            account_link_id=link,
            repository_id=200 + n,
            installation_id=50,
            name=f"p{n}",
            full_name=f"org/p{n}",
        )
        await service.set_user_credentials(
            link,
            1,
            Secret(
                '{"access_token":"synthetic-user-old","refresh_token":"synthetic-refresh-old"}'
            ),
            expires_at=999,
        )
        await service.set_grant(pid, 1, dict(PERMS), dict(PERMS))
        projects.append(pid)
        identities.append(
            TokenIdentity(
                f"owner{n}",
                "human",
                principal_id=owner,
                permissions=frozenset({"tasks.read", "tasks.update"}),
            )
        )
        links.append(link)
    contexts = [
        CredentialContext(
            project_id=pid,
            binding_id=pid,
            repository_id=201 + i,
            actor_id=identities[i].principal_id,
            purpose="read",
            auth_epoch=1,
            permissions=PERMS,
        )
        for i, pid in enumerate(projects)
    ]
    return vault, service, provider, contexts, identities, links


async def test_parallel_scoped_tokens_refresh_without_context_leak(
    db, db_dsn, tmp_path
):
    vault, service, provider, contexts, identities, _ = await setup(
        db, db_dsn, tmp_path
    )
    second = GithubConnections(
        db_dsn,
        GithubVault(db_dsn, vault.key_path, clock=lambda: 1000.0),
        provider,
        clock=lambda: 1000.0,
    )
    results = await asyncio.gather(
        *(
            s.credential(identities[i], contexts[i])
            for s in (service, second, service)
            for i in (0, 1)
        )
    )
    assert provider.refreshed == 2, (
        "one refresh per independent account, shared across broker instances"
    )
    assert len(provider.minted) == 2, (
        "one narrow token per context, shared across broker instances"
    )
    for index, lease in enumerate(results):
        ctx = contexts[index % 2]
        assert lease.context == ctx
        assert lease.token.reveal() == f"synthetic-install-token-{ctx.project_id}"
    with pytest.raises(CredentialError):
        await service.credential(identities[0], contexts[1])
    with pytest.raises(CredentialError):
        await service.credential(
            identities[0], replace(contexts[0], repository_id=contexts[1].repository_id)
        )
    assert "current_project" not in vars(service)


async def test_secret_values_absent_from_projections_logs_and_argv(
    db, db_dsn, tmp_path, caplog
):
    vault, service, provider, contexts, identities, _ = await setup(
        db, db_dsn, tmp_path
    )
    lease = await service.credential(identities[0], contexts[0])
    metadata = await service.status(identities[0], contexts[0].project_id)
    before = dict(os.environ)
    env = command_environment(
        lease,
        contexts[0],
        tmp_path / "runtime",
        {
            "PATH": "/usr/bin:/bin",
            "GH_TOKEN": "host-secret",
            "SSH_AUTH_SOCK": "host-socket",
            "AWS_SECRET_ACCESS_KEY": "host-secret",
        },
    )
    assert os.environ == before
    assert env["GH_TOKEN"] == lease.token.reveal()
    assert "SSH_AUTH_SOCK" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert env["GH_CONFIG_DIR"].startswith(str(tmp_path / "runtime"))
    argv = ["gh", "api", "repos/org/p1"]
    origin = credential_origin("org/p1")
    provider.failure = True
    with pytest.raises(CredentialError) as failure:
        await service.credential(identities[0], contexts[0])
    projection = (
        json.dumps(metadata)
        + repr(lease)
        + repr(lease.token)
        + str(failure.value)
        + caplog.text
        + repr(argv)
        + origin
    )
    for secret in (
        "synthetic-user-old",
        "synthetic-user-new",
        "synthetic-refresh-old",
        "synthetic-refresh-new",
        "synthetic-client-secret",
        "synthetic-install-token",
        "synthetic-key",
    ):
        assert secret not in projection
    assert "@" not in origin
    with pytest.raises(CredentialError):
        command_environment(lease, contexts[1], tmp_path / "wrong", {})
    for bad in (
        "https://user:token@github.com/org/repo",
        "../repo",
        "org/repo?token=x",
    ):
        with pytest.raises(CredentialError):
            credential_origin(bad)


async def test_missing_key_and_rotation_fail_closed(db, db_dsn, tmp_path):
    vault, service, provider, contexts, identities, _ = await setup(
        db, db_dsn, tmp_path
    )
    lease = await service.credential(identities[0], contexts[0])
    old = vault.key_path.read_bytes()
    await vault.rotate()
    assert vault.key_path.read_bytes() != old
    assert vault.key_path.stat().st_mode & 0o777 == 0o600
    assert "synthetic" not in vault.key_path.read_text()
    assert (
        await service.credential(identities[0], contexts[0])
    ).token.reveal() == lease.token.reveal()
    await vault.put("probe", "app", Secret("synthetic-rotation-probe"))
    assert (await vault.get("probe", "app")).reveal() == "synthetic-rotation-probe"
    saved = vault.key_path.with_suffix(".saved")
    vault.key_path.rename(saved)
    with pytest.raises(CredentialError, match="key_missing"):
        await service.credential(identities[0], contexts[0])
    with pytest.raises(CredentialError):
        await vault.bootstrap()
    assert not vault.key_path.exists(), "restored DB must not silently get a fresh key"
    state = await service.status(identities[0], contexts[0].project_id)
    assert state["status"] == "recovery_required"
    assert "key_path" not in state
    saved.rename(vault.key_path)
    assert (await vault.get("probe", "app")).reveal() == "synthetic-rotation-probe"
    rows = await database.fetchall(db, "SELECT ciphertext FROM github_credential_slots")
    assert rows and all(b"synthetic" not in bytes(r[0]) for r in rows)


async def test_rejected_permission_expansion_preserves_previous_grant(
    db, db_dsn, tmp_path
):
    _, service, _, contexts, identities, _ = await setup(db, db_dsn, tmp_path)
    with pytest.raises(CredentialError):
        await service.set_grant(
            contexts[0].project_id,
            1,
            dict(PERMS),
            {"contents": "write", "metadata": "read"},
        )
    assert (await service.credential(identities[0], contexts[0])).context == contexts[0]
    with pytest.raises(CredentialError):
        await service.credential(identities[0], replace(contexts[0], auth_epoch=2))
