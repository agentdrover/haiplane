"""MCP in-process Bearer propagation for REST calls from tools."""

from __future__ import annotations

from hub.mcp_internal_auth import (
    bearer_context_get,
    bearer_context_reset,
    bearer_context_set,
)
from hub.mcp_server import _hub_token


def test_hub_token_prefers_env_over_context(monkeypatch):
    monkeypatch.setenv("OPENCLAW_HUB_TOKEN", "env-secret")
    h = bearer_context_set("ctx-secret")
    try:
        assert _hub_token() == "env-secret"
    finally:
        bearer_context_reset(h)


def test_hub_token_uses_context_when_env_empty(monkeypatch):
    monkeypatch.delenv("OPENCLAW_HUB_TOKEN", raising=False)
    h = bearer_context_set("ctx-only")
    try:
        assert _hub_token() == "ctx-only"
    finally:
        bearer_context_reset(h)


def test_context_reset_restores_previous():
    outer = bearer_context_set("first")
    try:
        inner = bearer_context_set("second")
        try:
            assert bearer_context_get() == "second"
        finally:
            bearer_context_reset(inner)
        assert bearer_context_get() == "first"
    finally:
        bearer_context_reset(outer)
