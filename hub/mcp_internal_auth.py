"""Inbound identity for MCP: the Bearer to reuse, and who is calling."""

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


# Who is calling, for telemetry only (#780). Usage records must name a
# principal and a role — "which role uses which tool" is half of the case for
# a core surface — and a tool function has no request to read that from. The
# identity is a snapshot of what AuthMiddleware already resolved: the token
# itself stays in the bearer var above and is never copied here.
_identity: contextvars.ContextVar[tuple[int | None, str, bool] | None] = (
    contextvars.ContextVar("hub_mcp_identity", default=None)
)


def identity_context_set(
    principal_id: int | None, role: str, is_human: bool = False
) -> Any:
    """Stash caller (principal_id, role, is_human); returns handle for the reset.

    ``is_human`` is ``TokenIdentity.is_human`` as resolved after the role gate
    (#1624): a watcher or steward is never human, a custom role holding
    ``tasks.human_gate`` is. The bare role cannot say that, so the flag rides
    along. Everything that does not set it is the agent view.
    """
    return _identity.set((principal_id, role or "", bool(is_human)))


def identity_context_reset(handle: Any) -> None:
    _identity.reset(handle)


def identity_context_get() -> tuple[int | None, str]:
    """Caller identity for the current MCP call, or (None, "") outside one."""
    ident = _identity.get()
    return (ident[0], ident[1]) if ident else (None, "")


def identity_is_human() -> bool:
    """True only for a call whose protected identity is human (#1624).

    No context (stdio, open mode, a direct call from code) is False: the
    agent view is the default and the human view has to be earned.
    """
    ident = _identity.get()
    return bool(ident and ident[2])
