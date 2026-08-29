"""Canonical Hub instance label and base URL for MCP echo (#174)."""

from __future__ import annotations

import json
import socket
from typing import Any, Literal
from urllib.parse import urlparse

from hub import config

InstanceLabel = Literal["prod", "local"]


def hub_hostname() -> str:
    """Server hostname — a config-independent instance fact (#452).

    Unlike ``base_url``/``instance`` (echoed from HAIPLANE_HUB_URL), the
    hostname cannot be faked by a misconfigured env var, so it disambiguates
    which machine actually served the response.
    """
    try:
        return socket.gethostname()
    except OSError:
        return ""


def hub_base_url() -> str:
    """Resolved public base URL for this Hub process."""
    explicit = (config.env_get("HUB_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    return f"http://{config.HUB_HOST}:{config.HUB_PORT}".rstrip("/")


def hub_instance_label(base_url: str | None = None) -> InstanceLabel:
    """Map base URL host to prod|local (public host vs loopback)."""
    url = base_url or hub_base_url()
    host = (urlparse(url).hostname or "").lower()
    if host in ("127.0.0.1", "localhost", "::1"):
        return "local"
    return "prod"


def instance_echo_fields() -> dict[str, str]:
    base = hub_base_url()
    return {
        "instance": hub_instance_label(base),
        "base_url": base,
        "server_id": hub_hostname(),
    }


def with_instance_echo(payload: dict[str, Any]) -> dict[str, Any]:
    """Merge instance/base_url into a response payload."""
    return {**payload, **instance_echo_fields()}


def mutation_activity_detail() -> str:
    """JSON detail for activity_log on task mutations."""
    return json.dumps(instance_echo_fields(), ensure_ascii=False)
