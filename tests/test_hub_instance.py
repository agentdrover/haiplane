"""Tests for Hub instance echo (#174)."""

from __future__ import annotations

import json

import pytest

from hub.hub_instance import hub_instance_label, instance_echo_fields
from hub.mcp_envelope import format_echo_response, merge_mutation_response


def test_hub_instance_label_local() -> None:
    assert hub_instance_label("http://127.0.0.1:8080") == "local"
    assert hub_instance_label("http://localhost:8080") == "local"


def test_hub_instance_label_prod() -> None:
    assert hub_instance_label("https://agenthai.ru/mcp") == "prod"


def test_format_echo_response_includes_instance() -> None:
    payload = json.loads(format_echo_response("hello"))
    assert payload["message"] == "hello"
    assert payload["instance"] in ("prod", "local")
    assert payload["base_url"]


def test_merge_mutation_response_includes_instance() -> None:
    raw = merge_mutation_response(
        "ok",
        {
            "status": "open",
            "awaiting": "none",
            "transition": None,
            "next_action": "go",
            "actor_hint": "agent",
        },
    )
    payload = json.loads(raw)
    assert payload["instance"] in ("prod", "local")
    assert payload["base_url"]


def test_instance_from_openclaw_hub_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCLAW_HUB_URL", "https://agenthai.ru/mcp")
    fields = instance_echo_fields()
    assert fields["instance"] == "prod"
    assert fields["base_url"] == "https://agenthai.ru/mcp"
