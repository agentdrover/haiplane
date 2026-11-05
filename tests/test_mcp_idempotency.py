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


async def test_propose_remains_json_text_with_operation_echo(monkeypatch):
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
    assert isinstance(result, str)
    assert json.loads(result)["operation_id"] == OP
    assert json.loads(result)["replayed"] is True
    assert post.await_args.kwargs["extra_headers"] == {
        "Idempotency-Key": OP,
        "X-Client-Request-Id": "req-2",
    }
    assert "operation_id" not in post.await_args.args[1]
    assert post.await_args.args[1]["client_request_id"] == "req-2"


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
