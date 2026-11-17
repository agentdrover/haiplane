"""Internal GitHub credential boundary for future verified browser flows.

Configuration writers require trusted, already verified server input. They
are not public CRUD routes. User/App registration is supplied by later flows.
"""

from collections.abc import Callable
from contextlib import asynccontextmanager
import json
import time

from hub import db as database
from hub.config import TokenIdentity
from hub.integrations.github_credentials import (
    CredentialContext,
    CredentialError,
    CredentialLease,
    CredentialProvider,
    ResolvedGrant,
    Secret,
    permissions_of,
    permits,
    credential_origin,
)
from hub.services.github_vault import GithubVault
from hub.services import project_access


class GithubConnections:
    def __init__(
        self,
        dsn: str,
        vault: GithubVault,
        provider: CredentialProvider,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.dsn, self.vault, self.provider, self.clock = dsn, vault, provider, clock

    @asynccontextmanager
    async def _connection(self, *, write: bool = False):
        db = await database.connect(self.dsn)
        try:
            if write:
                async with database.write_transaction(db):
                    yield db
            else:
                yield db
        finally:
            await db.close()

    async def configure_app(
        self, app_id: int, client_id: str, secret: Secret, ceiling: dict[str, str]
    ) -> None:
        """Persist verified App material; caller owns administrator authorization."""
        permissions = permissions_of(ceiling)
        if app_id <= 0 or not client_id:
            raise CredentialError("invalid_app")
        try:
            payload = json.loads(secret.reveal())
            if not isinstance(payload, dict) or not all(
                isinstance(payload.get(k), str) and payload[k]
                for k in ("private_key", "client_secret")
            ):
                raise ValueError
            payload["client_id"] = client_id
        except (ValueError, TypeError):
            raise CredentialError("invalid_app") from None
        await self.vault.put(f"app:{app_id}", "app", Secret(json.dumps(payload)))
        async with self._connection(write=True) as db:
            await db.execute(
                "INSERT INTO github_app_credential_configs(app_id,client_id,permissions) VALUES (?,?,?) ON CONFLICT(app_id) DO UPDATE SET client_id=excluded.client_id,permissions=excluded.permissions,version=github_app_credential_configs.version+1",
                (app_id, client_id, json.dumps(dict(permissions))),
            )

    async def set_user_credentials(
        self, link_id: int, app_id: int, secret: Secret, *, expires_at: float
    ) -> None:
        """Only the verified OAuth callback/recovery flow may call this writer."""
        async with self._connection() as db:
            rows = await database.fetchall(
                db,
                "SELECT 1 FROM github_account_links l JOIN principals p ON p.id=l.principal_id JOIN github_app_credential_configs a ON a.app_id=? WHERE l.id=? AND l.status='active' AND p.status='active' AND p.kind='human' AND a.status='active'",
                (app_id, link_id),
            )
            if not rows:
                raise CredentialError("connection_unavailable")
        await self.vault.put(
            f"user:{app_id}:{link_id}", "oauth", secret, expires_at=expires_at
        )
        async with self._connection(write=True) as db:
            await db.execute(
                "INSERT OR IGNORE INTO github_user_credential_apps(account_link_id,app_id) VALUES (?,?)",
                (link_id, app_id),
            )

    async def set_grant(
        self,
        project_id: int,
        app_id: int,
        installation_permissions: dict[str, str],
        project_permissions: dict[str, str],
    ) -> None:
        """Replace a verified grant only after all three planes admit it."""
        install, cap = (
            permissions_of(installation_permissions),
            permissions_of(project_permissions),
        )
        async with self._connection(write=True) as db:
            rows = await database.fetchall(
                db,
                "SELECT permissions FROM github_app_credential_configs WHERE app_id=? AND status='active'",
                (app_id,),
            )
            if (
                not rows
                or not permits(permissions_of(json.loads(rows[0][0])), install)
                or not permits(install, cap)
            ):
                raise CredentialError("permission_expansion_refused")
            existing = await database.fetchall(
                db,
                "SELECT 1 FROM github_project_credential_grants WHERE project_id=?",
                (project_id,),
            )
            await db.execute(
                "INSERT INTO github_project_credential_grants(project_id,app_id,installation_permissions,project_permissions) VALUES (?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET app_id=excluded.app_id,installation_permissions=excluded.installation_permissions,project_permissions=excluded.project_permissions,version=github_project_credential_grants.version+1",
                (project_id, app_id, json.dumps(dict(install)), json.dumps(dict(cap))),
            )
            if existing:
                await db.execute(
                    "UPDATE private_project_security SET auth_epoch=auth_epoch+1 WHERE project_id=?",
                    (project_id,),
                )

    async def _snapshot(
        self,
        identity: TokenIdentity,
        ctx: CredentialContext,
        delegation: project_access.Delegation | None,
    ):
        if identity.principal_id != ctx.actor_id or ctx.binding_id != ctx.project_id:
            raise CredentialError("access_denied")
        async with self._connection() as db:
            decision = await project_access.decide(
                db,
                identity,
                project_id=ctx.project_id,
                task_id=ctx.task_id,
                permission="tasks.update" if ctx.is_write else "tasks.read",
                expected_epoch=ctx.auth_epoch,
                delegation=delegation,
            )
            if not decision.allowed:
                raise CredentialError("access_denied")
            rows = await database.fetchall(
                db,
                "SELECT b.*,l.github_user_id,g.app_id,g.installation_permissions,g.project_permissions,g.version AS grant_version,a.client_id,a.permissions AS app_permissions,a.version AS app_version "
                "FROM project_repository_bindings b JOIN private_project_security s ON s.project_id=b.project_id AND s.owner_id=b.owner_id "
                "JOIN github_account_links l ON l.id=b.account_link_id AND l.principal_id=s.owner_id "
                "JOIN principals p ON p.id=s.owner_id "
                "JOIN github_project_credential_grants g ON g.project_id=b.project_id "
                "JOIN github_app_credential_configs a ON a.app_id=g.app_id "
                "JOIN github_user_credential_apps u ON u.app_id=a.app_id AND u.account_link_id=l.id "
                "WHERE b.project_id=? AND b.repository_id=? AND s.auth_epoch=? AND b.status='active' AND l.status='active' AND p.status='active' AND p.kind='human' AND g.status='active' AND a.status='active'",
                (ctx.project_id, ctx.repository_id, ctx.auth_epoch),
            )
        if not rows:
            raise CredentialError("connection_unavailable")
        row = dict(rows[0])
        for plane in (
            "app_permissions",
            "installation_permissions",
            "project_permissions",
        ):
            if not permits(permissions_of(json.loads(row[plane])), ctx.permissions):
                raise CredentialError("permission_denied")
        credential_origin(row["full_name"])
        return row

    async def _resolve(
        self,
        identity: TokenIdentity,
        ctx: CredentialContext,
        delegation: project_access.Delegation | None,
    ) -> tuple[ResolvedGrant, Secret]:
        row = await self._snapshot(identity, ctx, delegation)
        app_slot = f"app:{row['app_id']}"
        user_slot = f"user:{row['app_id']}:{row['account_link_id']}"
        app, app_version = await self.vault.material(app_slot, "app")

        async def refresh(old: Secret | None):
            if old is None:
                raise CredentialError("reauthorization_required")
            return await self.provider.refresh_user(row["app_id"], app, old)

        await self.vault.refresh(user_slot, "oauth", refresh)
        user, user_version = await self.vault.material(user_slot, "oauth")
        versions = (
            row["app_version"],
            row["grant_version"],
            user_version,
            app_version,
        )
        grant = ResolvedGrant(
            row["app_id"],
            row["installation_id"],
            row["account_link_id"],
            row["github_user_id"],
            row["full_name"],
            row["client_id"],
            permissions_of(json.loads(row["installation_permissions"])),
            versions,
            app_slot,
            user_slot,
        )
        try:
            proof = await self.provider.verify_owner(ctx, grant, user, app)
        except CredentialError as error:
            raise CredentialError(str(error)) from None
        except Exception:
            raise CredentialError("provider_unavailable") from None
        if (
            (proof.github_user_id, proof.repository_id, proof.installation_id)
            != (grant.github_user_id, ctx.repository_id, grant.installation_id)
            or proof.admin is not True
            or not permits(proof.installation_permissions, ctx.permissions)
        ):
            raise CredentialError("delegation_denied")
        if await self._snapshot(identity, ctx, delegation) != row:
            raise CredentialError("access_changed")
        return grant, app

    async def credential(
        self,
        identity: TokenIdentity,
        ctx: CredentialContext,
        delegation: project_access.Delegation | None = None,
    ) -> CredentialLease:
        reason = "credential_unavailable"
        try:
            return await self._credential(identity, ctx, delegation)
        except CredentialError as error:
            reason = str(error)
        except Exception:
            pass
        # Outside the except block: the public error retains neither provider
        # exception context nor traceback frames containing plaintext material.
        raise CredentialError(reason)

    async def _credential(
        self,
        identity: TokenIdentity,
        ctx: CredentialContext,
        delegation: project_access.Delegation | None,
    ) -> CredentialLease:
        grant, app = await self._resolve(identity, ctx, delegation)
        slot = "installation:" + ctx.cache_key(grant.versions)

        async def mint(_old: Secret | None):
            response = await self.provider.mint(ctx, grant, app)
            if response.expires_at > self.clock() + 3700:
                raise CredentialError("invalid_provider_response")
            return response.token, response.expires_at

        token, expires = await self.vault.refresh(slot, "installation", mint)
        fresh, _ = await self._resolve(identity, ctx, delegation)
        if fresh != grant:
            raise CredentialError("access_changed")
        return CredentialLease(ctx, token, expires)

    async def status(
        self, identity: TokenIdentity, project_id: int
    ) -> dict[str, object]:
        """Allowlisted projection shared by future UI/API/MCP readers."""
        async with self._connection() as db:
            if not (
                await project_access.decide(
                    db, identity, project_id=project_id, permission="tasks.read"
                )
            ).allowed:
                raise CredentialError("access_denied")
            configured = await database.fetchall(
                db,
                "SELECT g.status,a.status AS app_status FROM github_project_credential_grants g JOIN github_app_credential_configs a ON a.app_id=g.app_id WHERE g.project_id=?",
                (project_id,),
            )
        try:
            await self.vault.check_key()
        except CredentialError:
            return {"status": "recovery_required", "action": "restore_operator_key"}
        state = (
            "not_configured"
            if not configured
            else "configured"
            if configured[0]["status"] == "active"
            and configured[0]["app_status"] == "active"
            else "unavailable"
        )
        return {
            "status": state,
            "credential_storage": "ready",
            "github_authorization": "unchecked",
            "project_id": project_id,
        }
