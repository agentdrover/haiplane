"""Tests for Hub instance echo (#174)."""

from __future__ import annotations

import json

import pytest

from hub import hub_instance
from hub.hub_instance import (
    hub_instance_label,
    instance_echo_fields,
    mutation_activity_detail,
)
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


def test_instance_from_prod_hub_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://agenthai.ru/mcp")
    fields = instance_echo_fields()
    assert fields["instance"] == "prod"
    assert fields["base_url"] == "https://agenthai.ru/mcp"


def test_instance_from_haiplane_hub_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://haiplane.com/mcp")
    fields = instance_echo_fields()
    assert fields["base_url"] == "https://haiplane.com/mcp"


def test_activity_detail_extra_cannot_override_instance_echo(monkeypatch) -> None:
    """#1689: ключи экземпляра в журнале — серверные, extra их не подменяет."""
    server = {"instance": "prod", "base_url": "https://hub.test", "server_id": "srv-1"}
    monkeypatch.setattr(hub_instance, "instance_echo_fields", lambda: dict(server))
    extra = {
        "instance": "local",
        "base_url": "http://evil.test",
        "server_id": "forged",
        "skill_fact": {"name": "s", "version": 3},
    }
    snapshot = json.loads(json.dumps(extra))

    detail = json.loads(mutation_activity_detail(extra))

    for key, value in server.items():
        assert detail[key] == value
    assert detail["skill_fact"] == {"name": "s", "version": 3}
    assert extra == snapshot
