"""Read-only identity and health diagnostics for operators and agents."""

from __future__ import annotations

from hub import config
from hub.config import TokenIdentity, _AGENT_DEFAULT_PERMS, _HUMAN_DEFAULT_PERMS
from hub.models import HealthView, WhoamiView
from hub.version import get_app_version


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
    )
