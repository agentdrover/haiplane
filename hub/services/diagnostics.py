"""Read-only identity and health diagnostics for operators and agents."""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from hub import config
from hub.config import TokenIdentity, _AGENT_DEFAULT_PERMS, _HUMAN_DEFAULT_PERMS
from hub.config import WORKSPACE_REPO_LINK
from hub.hub_instance import instance_echo_fields
from hub.integrations.registry import plugins
from hub.models import HealthView, IdentityDiagnosticsView, WhoamiView
from hub.version import get_app_version

log = logging.getLogger("hub")


async def check_default_workspace_origin() -> bool | None:
    """Probe the default workspace's origin reachability, warning if broken (#455).

    The default ``_default`` workspace is set up manually (not via
    provision_project), so a broken deploy key leaves pair branches cut from a
    silently stale base. This makes that failure loud. Returns None when the
    workspace is not a git repository (nothing to probe).
    """
    repo = str(WORKSPACE_REPO_LINK)
    if not os.path.isdir(os.path.join(repo, ".git")):
        return None
    try:
        ok = await plugins.git_ops.origin_reachable(repo=repo, timeout=15)
    except Exception:
        ok = False
    if not ok:
        log.warning(
            "Default workspace %s cannot reach origin: pair branches would be "
            "cut from a possibly stale base. Fix the haiplane service git access "
            "(deploy key / ssh) — see docs/agent-deploy-runbook.md.",
            repo,
        )
    return ok


def _effective_permissions(identity: TokenIdentity) -> list[str]:
    if identity.permissions:
        return sorted(identity.permissions)
    if identity.role in ("admin", "super_admin"):
        return sorted(_HUMAN_DEFAULT_PERMS | {"admin.read"})
    if identity.role == "agent":
        return sorted(_AGENT_DEFAULT_PERMS)
    return sorted(_HUMAN_DEFAULT_PERMS)


def _public_auth_source(identity: TokenIdentity) -> str:
    source = identity.auth_source or "anonymous"
    if source == "db_api_key":
        return "db"
    return source


def build_whoami(identity: TokenIdentity) -> WhoamiView:
    perms = _effective_permissions(identity)
    return WhoamiView(
        username=identity.username,
        role=identity.role,
        permissions_summary=perms,
        permissions_count=len(perms),
        auth_source=_public_auth_source(identity),
        api_key_id=identity.api_key_id
        if identity.auth_source == "db_api_key"
        else None,
        principal_id=identity.principal_id,
        app_version=get_app_version(),
    )


async def _workspace_branch(repo: str) -> str:
    """Current branch of the server workspace; empty on any failure (#452)."""
    try:
        return (await plugins.git_ops.current_branch(repo=repo)) or ""
    except Exception:
        return ""


async def build_identity_diagnostics(
    identity: TokenIdentity, *, connected_via: str = ""
) -> IdentityDiagnosticsView:
    """Caller identity + honest instance/workspace state in one response (#452).

    ``connected_via`` is the base URL the client actually reached (request
    Host); when its host differs from the configured ``base_url`` host,
    ``config_mismatch`` is set so an operator is never misled about which
    instance served the call.
    """
    from hub.services.orchestration import worktree_per_task_enabled

    whoami = build_whoami(identity)
    inst = instance_echo_fields()
    workspace = str(WORKSPACE_REPO_LINK)
    branch = await _workspace_branch(workspace)
    workspace_mode = "worktree" if worktree_per_task_enabled() else "legacy"

    configured_host = (urlparse(inst["base_url"]).hostname or "").lower()
    connected_host = (urlparse(connected_via).hostname or "").lower()
    mismatch = bool(connected_host) and configured_host != connected_host

    return IdentityDiagnosticsView(
        username=whoami.username,
        role=whoami.role,
        principal_id=whoami.principal_id,
        auth_source=whoami.auth_source,
        permissions_count=whoami.permissions_count,
        instance=inst["instance"],
        base_url=inst["base_url"],
        server_id=inst.get("server_id", ""),
        connected_via=connected_via,
        config_mismatch=mismatch,
        workspace_path=workspace,
        workspace_branch=branch,
        workspace_mode=workspace_mode,
        app_version=whoami.app_version,
    )


def build_health() -> HealthView:
    auth_required = bool(config.HUB_TOKENS) and not config.HUB_AUTH_DISABLED
    return HealthView(
        status="ok",
        app_version=get_app_version(),
        bind_host=config.HUB_HOST,
        bind_port=config.HUB_PORT,
        auth_required=auth_required,
        auth_disabled=config.HUB_AUTH_DISABLED,
        env_tokens_configured=bool(config.HUB_TOKENS),
        vast_enabled=config.VAST_ENABLED,
        cursor_cloud_configured=bool(config.CURSOR_API_KEY),
    )
