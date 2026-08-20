"""Cursor Cloud Agents API client (#756): spec-shaped requests, honest Nones.

The client's whole contract is degradation: no key → no network call; any
error → None + one warning; the request body follows the v1 spec so the
dispatcher (#757) never builds payloads itself.
"""

from __future__ import annotations

import httpx
import pytest

from hub import config
from hub.integrations import cursor_cloud


@pytest.fixture
def _configured(monkeypatch):
    monkeypatch.setattr(config, "CURSOR_API_KEY", "key-under-test")
    monkeypatch.setattr(config, "CURSOR_API_URL", "https://api.cursor.test")


class _Recorder:
    """Captures the outgoing request and plays back a canned response."""

    def __init__(self, response: httpx.Response | Exception):
        self.response = response
        self.request: httpx.Request | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _patch_transport(monkeypatch, recorder: _Recorder) -> None:
    original_init = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(recorder.handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


async def test_unconfigured_client_degrades(monkeypatch, client):
    # AC-1 (#756): no key → None without touching the network, and the
    # health surface says so.
    monkeypatch.setattr(config, "CURSOR_API_KEY", "")

    health = (await client.get("/health")).json()
    assert health["cursor_cloud_configured"] is False

    # Only now poison the transport: the ASGI test client above rides the
    # same httpx.AsyncClient, so the tripwire must come after it.
    def _explode(*_a, **_k):
        raise AssertionError("no network call may happen without a key")

    monkeypatch.setattr(httpx.AsyncClient, "request", _explode)

    assert not cursor_cloud.is_configured()
    assert await cursor_cloud.list_models() is None
    assert await cursor_cloud.get_run("bc-x", "run-y") is None
    assert (
        await cursor_cloud.create_review_agent(
            repo_url="https://github.com/o/r",
            starting_ref="task-1/x",
            model_id="grok-4",
            prompt_text="review",
            hub_mcp_url="https://hub/mcp",
            reviewer_token="tok",
        )
        is None
    )


async def test_review_agent_request_matches_spec(monkeypatch, _configured):
    # AC-2 (#756): the body follows the v1 spec — repos/startingRef,
    # model.id, hub MCP with the REVIEWER bearer, no auto-PR.
    recorder = _Recorder(httpx.Response(200, json={"agent": {"id": "bc-1"}}))
    _patch_transport(monkeypatch, recorder)

    result = await cursor_cloud.create_review_agent(
        repo_url="https://github.com/mrPDA/spike",
        starting_ref="task-9/branch",
        model_id="grok-4",
        prompt_text="review per skill v8",
        hub_mcp_url="https://agenthai.ru/mcp",
        reviewer_token="reviewer-token",
    )

    assert result == {"agent": {"id": "bc-1"}}
    assert recorder.request is not None
    assert recorder.request.method == "POST"
    assert str(recorder.request.url) == "https://api.cursor.test/v1/agents"
    assert recorder.request.headers["Authorization"] == "Bearer key-under-test"
    import json

    body = json.loads(recorder.request.content)
    assert body["repos"] == [
        {"url": "https://github.com/mrPDA/spike", "startingRef": "task-9/branch"}
    ]
    assert body["model"] == {"id": "grok-4"}
    assert body["autoCreatePR"] is False
    assert body["workOnCurrentBranch"] is False
    (mcp,) = body["mcpServers"]
    assert mcp["url"] == "https://agenthai.ru/mcp"
    assert mcp["headers"]["Authorization"] == "Bearer reviewer-token"


async def test_api_errors_degrade_to_none(monkeypatch, _configured):
    # AC-3 (#756): HTTP errors, transport failures and junk bodies all
    # collapse to None — never an exception across the boundary.
    for canned in (
        httpx.Response(500, text="boom"),
        httpx.Response(403, json={"error": "nope"}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["list", "not", "object"]),
        httpx.ConnectTimeout("slow"),
    ):
        recorder = _Recorder(canned)
        _patch_transport(monkeypatch, recorder)
        assert await cursor_cloud.list_models() is None
        assert await cursor_cloud.get_usage("bc-1", "run-1") is None
