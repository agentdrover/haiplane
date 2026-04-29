"""Inbound Bearer for MCP tools calling the Hub REST API in-process."""

from __future__ import annotations

import contextvars
from typing import Any

# Set by AuthMiddleware for /mcp/* when the client uses Authorization: Bearer …
# so hub_create_task and other tools reuse the same credential for httpx.
_internal_bearer: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "hub_mcp_internal_bearer", default=None
)


def bearer_context_set(token: str) -> Any:
    """Stash token; returns opaque handle for :func:`bearer_context_reset`."""
    return _internal_bearer.set(token)


def bearer_context_reset(handle: Any) -> None:
    _internal_bearer.reset(handle)


def bearer_context_get() -> str | None:
    return _internal_bearer.get()
