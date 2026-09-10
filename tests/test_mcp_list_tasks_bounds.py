"""#1229: listing bounds must be visible in tools/list before the call.

REST already rejects limit>200, after_id<0 and an unknown mode. The published
MCP schema did not, so an agent learned the ceiling only from a 422.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import ValidationError

from hub import mcp_server as srv
from hub.app import app
from hub.mcp_server import hub_list_tasks

LIST_LIMIT_MAX = 200
LIST_AFTER_ID_MIN = 0
LIST_MODES = frozenset({"full", "summary"})
MCP_LIMIT_DEFAULT = 20
REST_LIMIT_DEFAULT = 50


def _int_branch(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("type") == "integer":
        return schema
    for variant in schema.get("anyOf", ()):
        if variant.get("type") == "integer":
            return variant
    raise AssertionError(f"no integer branch in {schema}")


def _allowed_modes(schema: dict[str, Any]) -> set[str]:
    if "enum" in schema:
        return set(schema["enum"])
    pattern = schema.get("pattern", "")
    if pattern == "^(full|summary)$":
        return set(LIST_MODES)
    raise AssertionError(f"mode schema is not a closed set: {schema}")


def _nullable(schema: dict[str, Any]) -> bool:
    if schema.get("type") == "null":
        return True
    return any(variant.get("type") == "null" for variant in schema.get("anyOf", ()))


@pytest.fixture(scope="module")
def list_tasks_schema() -> dict[str, Any]:
    async def load() -> dict[str, Any]:
        tools = {tool.name: tool.inputSchema for tool in await srv.mcp.list_tools()}
        return tools["hub_list_tasks"]

    return asyncio.run(load())


@pytest.fixture(scope="module")
def list_tasks_arg_model():
    tool = srv.mcp._tool_manager.get_tool("hub_list_tasks")
    assert tool is not None
    return tool.fn_metadata.arg_model


@pytest.fixture(scope="module")
def rest_list_params() -> dict[str, dict[str, Any]]:
    spec = app.openapi()
    params = spec["paths"]["/api/tasks"]["get"]["parameters"]
    return {item["name"]: item["schema"] for item in params}


def test_list_tools_publishes_list_tasks_bounds(list_tasks_schema: dict[str, Any]):
    props = list_tasks_schema["properties"]
    limit = props["limit"]
    after_id = props["after_id"]
    mode = props["mode"]

    assert limit["default"] == MCP_LIMIT_DEFAULT
    assert limit.get("maximum") == LIST_LIMIT_MAX
    assert "minimum" not in limit

    integer = _int_branch(after_id)
    assert integer.get("minimum") == LIST_AFTER_ID_MIN
    assert _nullable(after_id)

    assert _allowed_modes(mode) == set(LIST_MODES)


def test_rest_and_mcp_share_list_bounds_not_defaults(
    list_tasks_schema: dict[str, Any],
    rest_list_params: dict[str, dict[str, Any]],
):
    rest_limit = rest_list_params["limit"]
    rest_after = rest_list_params["after_id"]
    rest_mode = rest_list_params["mode"]
    mcp_limit = list_tasks_schema["properties"]["limit"]
    mcp_after = list_tasks_schema["properties"]["after_id"]
    mcp_mode = list_tasks_schema["properties"]["mode"]

    assert rest_limit["default"] == REST_LIMIT_DEFAULT
    assert mcp_limit["default"] == MCP_LIMIT_DEFAULT
    assert rest_limit.get("maximum") == mcp_limit.get("maximum") == LIST_LIMIT_MAX
    assert "minimum" not in rest_limit
    assert "minimum" not in mcp_limit

    assert _int_branch(rest_after).get("minimum") == LIST_AFTER_ID_MIN
    assert _int_branch(mcp_after).get("minimum") == LIST_AFTER_ID_MIN
    assert _nullable(rest_after) and _nullable(mcp_after)

    assert _allowed_modes(rest_mode) == _allowed_modes(mcp_mode) == set(LIST_MODES)


def test_mcp_schema_accepts_boundary_and_rejects_guesses(list_tasks_arg_model):
    list_tasks_arg_model.model_validate(
        {"limit": LIST_LIMIT_MAX, "after_id": 0, "mode": "summary"}
    )
    with pytest.raises(ValidationError):
        list_tasks_arg_model.model_validate({"limit": LIST_LIMIT_MAX + 1})
    with pytest.raises(ValidationError):
        list_tasks_arg_model.model_validate({"after_id": -1})
    with pytest.raises(ValidationError):
        list_tasks_arg_model.model_validate({"mode": "invalid"})


@pytest.mark.asyncio
async def test_list_tasks_forwards_boundary_and_walks_cursor() -> None:
    pages = [
        {
            "tasks": [{"id": 4, "title": "A", "status": "open", "task_type": "task"}],
            "next_cursor": 4,
        },
        {
            "tasks": [{"id": 2, "title": "B", "status": "open", "task_type": "task"}],
            "next_cursor": None,
        },
    ]
    mock_get = AsyncMock(side_effect=pages)
    with patch.object(srv, "_api_get", mock_get):
        first = await hub_list_tasks(limit=LIST_LIMIT_MAX, after_id=0, mode="summary")
        second = await hub_list_tasks(limit=LIST_LIMIT_MAX, after_id=4, mode="summary")

    first_url = urlparse(mock_get.await_args_list[0].args[0])
    second_url = urlparse(mock_get.await_args_list[1].args[0])
    first_qs = parse_qs(first_url.query)
    second_qs = parse_qs(second_url.query)
    assert first_qs["limit"] == [str(LIST_LIMIT_MAX)]
    assert first_qs["after_id"] == ["0"]
    assert first_qs["mode"] == ["summary"]
    assert second_qs["after_id"] == ["4"]
    assert first.structuredContent["next_cursor"] == 4
    assert second.structuredContent["next_cursor"] is None


async def test_rest_still_rejects_the_same_guesses(client):
    over = await client.get("/api/tasks?limit=201")
    negative = await client.get("/api/tasks?after_id=-1")
    bad_mode = await client.get("/api/tasks?mode=invalid")
    assert over.status_code == 422
    assert negative.status_code == 422
    assert bad_mode.status_code == 422
