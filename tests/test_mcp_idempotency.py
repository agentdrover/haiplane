"""MCP operation transport metadata without changes to task JSON."""

import json
from unittest.mock import AsyncMock

import pytest

from hub import mcp_server as mcp

OP = "mcp-operation-2241"


@pytest.mark.parametrize("replayed", [False, True])
async def test_create_sends_key_and_projects_echo_metadata(monkeypatch, replayed):
    headers = {"Idempotency-Key": OP}
    if replayed:
        headers["Idempotent-Replayed"] = "true"
    post = AsyncMock(
        return_value=({"id": 42, "status": "open"}, 200 if replayed else 201, headers)
    )
    monkeypatch.setattr(mcp, "_post_with_headers", post)
    result = await mcp.hub_create_task(
        "MCP", operation_id=OP, client_request_id="req-1"
    )
    path, body = post.await_args.args
    assert path == "/api/tasks"
    assert body["client_request_id"] == "req-1" and "operation_id" not in body
    assert post.await_args.kwargs["extra_headers"] == {
        "Idempotency-Key": OP,
        "X-Client-Request-Id": "req-1",
    }
    assert result.meta == {"operation_id": OP, "replayed": replayed}
    assert result.structuredContent["task"] == {"id": 42, "status": "open"}


async def test_create_without_caller_alias_sends_operation_id_as_both(monkeypatch):
    post = AsyncMock(
        return_value=(
            {"id": 42, "status": "open"},
            201,
            {"Idempotency-Key": OP},
        )
    )
    monkeypatch.setattr(mcp, "_post_with_headers", post)
    await mcp.hub_create_task("MCP", operation_id=OP)
    body = post.await_args.args[1]
    assert body["client_request_id"] == OP
    assert post.await_args.kwargs["extra_headers"] == {
        "Idempotency-Key": OP,
        "X-Client-Request-Id": OP,
    }


async def test_propose_returns_meta_echo_and_keeps_text_envelope(monkeypatch):
    post = AsyncMock(
        return_value=(
            {"id": 42},
            200,
            {"Idempotency-Key": OP, "Idempotent-Replayed": "true"},
        )
    )
    monkeypatch.setattr(mcp, "_post_with_headers", post)
    result = await mcp.hub_propose_task(
        "Draft", "Description", operation_id=OP, client_request_id="req-2"
    )
    assert isinstance(result, mcp.CallToolResult)
    assert result.meta == {"operation_id": OP, "replayed": True}
    assert result.structuredContent["operation_id"] == OP
    assert result.structuredContent["replayed"] is True
    text = json.loads(result.content[0].text)
    assert text["operation_id"] == OP and text["replayed"] is True
    assert "Draft task #42 created" in text["message"]
    assert post.await_args.kwargs["extra_headers"] == {
        "Idempotency-Key": OP,
        "X-Client-Request-Id": "req-2",
    }
    assert "operation_id" not in post.await_args.args[1]
    assert post.await_args.args[1]["client_request_id"] == "req-2"


async def test_propose_alias_only_reports_the_server_echo(monkeypatch):
    """Review, major 9: the echo comes from the response, not the input."""
    server_id = "srv_" + "a" * 32
    post = AsyncMock(return_value=({"id": 44}, 201, {"Idempotency-Key": server_id}))
    monkeypatch.setattr(mcp, "_post_with_headers", post)
    result = await mcp.hub_propose_task(
        "Draft", "Description", client_request_id="req-only"
    )
    assert result.meta == {"operation_id": server_id, "replayed": False}
    assert result.structuredContent["operation_id"] == server_id


async def test_propose_meta_is_visible_through_the_published_mcp_entry(monkeypatch):
    from hub.mcp_server import mcp as server

    post = AsyncMock(return_value=({"id": 45}, 201, {"Idempotency-Key": OP}))
    monkeypatch.setattr(mcp, "_post_with_headers", post)
    result = await server.call_tool(
        "hub_propose_task", {"title": "Draft", "description": "Description"}
    )
    assert result.meta == {"operation_id": OP, "replayed": False}


@pytest.mark.parametrize(
    "key_kwargs",
    [{"operation_id": OP}, {"client_request_id": "req-defect"}],
)
async def test_propose_prod_defect_refuses_both_key_kinds(monkeypatch, key_kwargs):
    """Review, major 8: the prod-defect branch has no idempotency in T11a."""
    post = AsyncMock()
    monkeypatch.setattr(mcp, "_api_post", post)
    with pytest.raises(mcp.HubApiError) as caught:
        await mcp.hub_propose_task(
            "Defect", "Broken", defect_verify="check the fix", **key_kwargs
        )
    assert caught.value.payload["reason"] == "operation_not_idempotent"
    post.assert_not_awaited()


async def test_propose_operation_only_also_sends_alias(monkeypatch):
    post = AsyncMock(return_value=({"id": 43}, 201, {"Idempotency-Key": OP}))
    monkeypatch.setattr(mcp, "_post_with_headers", post)
    await mcp.hub_propose_task("Draft", "Description", operation_id=OP)
    assert post.await_args.args[1]["client_request_id"] == OP
    assert post.await_args.kwargs["extra_headers"] == {
        "Idempotency-Key": OP,
        "X-Client-Request-Id": OP,
    }


async def test_bulk_forwards_key_to_rest_for_explicit_refusal(monkeypatch):
    post = AsyncMock(return_value=[])
    monkeypatch.setattr(mcp, "_api_post", post)
    await mcp.hub_create_subtasks(1, [{"title": "child"}], operation_id=OP)
    assert post.await_args.args[0] == "/api/tasks/1/subtasks"
    assert post.await_args.kwargs["extra_headers"] == {"Idempotency-Key": OP}


@pytest.mark.parametrize("include", [False, True])
async def test_operation_status_projects_structured_echo(monkeypatch, include):
    data = {
        "operation_id": OP,
        "status": "completed",
        "effects": [{"state": "unknown"}],
    }
    get = AsyncMock(return_value=data)
    monkeypatch.setattr(mcp, "_api_get", get)
    result = await mcp.hub_operation_status(OP, include_result=include)
    get.assert_awaited_once_with(
        f"/api/operations/{OP}" + ("?include=result" if include else "")
    )
    assert result.structuredContent["operation_id"] == OP
    assert result.structuredContent["effects"] == [{"state": "unknown"}]


async def test_operation_key_in_transport_retry_advice(monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx.AsyncClient, "post", AsyncMock(side_effect=httpx.ReadTimeout(""))
    )
    with pytest.raises(mcp.HubApiError) as caught:
        await mcp._post_with_headers(
            "/api/tasks", {"title": "key"}, extra_headers={"Idempotency-Key": OP}
        )
    assert caught.value.payload["retry_safe"] is True
    with pytest.raises(mcp.HubApiError) as caught:
        await mcp._post_with_headers(
            "/api/tasks/1/subtasks", {}, extra_headers={"Idempotency-Key": OP}
        )
    assert caught.value.payload["retry_safe"] is False
