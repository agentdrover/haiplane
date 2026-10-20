"""#1696: предупреждения вердикта (lifecycle_hint) доходят до агента через MCP."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from hub import mcp_server

SHA_NOTE = "SHA-NOTE: вердикт на коммит aaaa, вершина bbbb"
UNDISPOSED_NOTE = "UNDISPOSED-NOTE: две находки без исхода"
INFLIGHT_NOTE = "INFLIGHT-NOTE: машинное ревью ещё идёт"


async def test_submit_review_returns_all_lifecycle_hints(monkeypatch) -> None:
    hint = "\n".join([SHA_NOTE, UNDISPOSED_NOTE, INFLIGHT_NOTE])

    async def _post(path, body):
        return {
            "id": 42,
            "status": "running",
            "project": {"id": 1, "slug": "default"},
            "lifecycle_hint": hint,
        }

    monkeypatch.setattr(mcp_server, "_api_post", _post)
    monkeypatch.setattr(
        mcp_server, "_read_task", AsyncMock(return_value={"status": "review"})
    )
    out = await mcp_server.hub_submit_review(
        42, verdict="changes_requested", expected_generation=1
    )
    payload = json.loads(out)
    for note in (SHA_NOTE, UNDISPOSED_NOTE, INFLIGHT_NOTE):
        assert note in payload["message"], note
    assert payload["project"] == "default"
    assert payload["status"] == "running"
    assert payload["transition"] == {"from": "review", "to": "running"}
    assert payload["next_action"] and payload["actor_hint"]


async def test_submit_review_without_hint_adds_nothing(monkeypatch) -> None:
    async def _post(path, body):
        return {"id": 42, "status": "running", "lifecycle_hint": None}

    monkeypatch.setattr(mcp_server, "_api_post", _post)
    monkeypatch.setattr(
        mcp_server, "_read_task", AsyncMock(return_value={"status": "review"})
    )
    out = await mcp_server.hub_submit_review(
        42, verdict="changes_requested", expected_generation=1
    )
    assert "Lifecycle" not in json.loads(out)["message"]
