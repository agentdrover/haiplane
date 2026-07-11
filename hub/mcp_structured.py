"""Structured MCP tool outputs (schema_version + structuredContent payloads)."""

from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import BaseModel, Field
from mcp.types import CallToolResult, TextContent

from hub.hub_instance import with_instance_echo

MCP_STRUCTURED_SCHEMA_VERSION = "1"


class HubCreateTaskStructured(BaseModel):
    schema_version: str = Field(default=MCP_STRUCTURED_SCHEMA_VERSION)
    task: dict[str, Any]


class HubRefineTaskStructured(BaseModel):
    schema_version: str = Field(default=MCP_STRUCTURED_SCHEMA_VERSION)
    task_id: int
    # Request fields actually sent (the PATCH keys), so callers know exactly
    # what was applied without guessing from a TaskView diff.
    fields_set: list[str] = Field(default_factory=list)
    acceptance_criteria_count: int | None = None
    risks_count: int | None = None
    readiness_score: int | None = None
    dor_passed: bool | None = None
    no_op: bool = False
    # Full task as returned by REST /refine (TaskView), for machine consumers.
    task: dict[str, Any] | None = None


class HubTaskStatusStructured(BaseModel):
    schema_version: str = Field(default=MCP_STRUCTURED_SCHEMA_VERSION)
    task: dict[str, Any]


class HubRefineTasksStructured(BaseModel):
    schema_version: str = Field(default=MCP_STRUCTURED_SCHEMA_VERSION)
    # One entry per task: {task_id, fields_set, acceptance_criteria_count,
    # risks_count, readiness_score, dor_passed}.
    results: list[dict[str, Any]] = Field(default_factory=list)
    no_op: bool = False


class HubReadinessTreeStructured(BaseModel):
    schema_version: str = Field(default=MCP_STRUCTURED_SCHEMA_VERSION)
    # Full ReadinessTreeReport: {root_id, total, ready, not_ready, nodes[]}.
    report: dict[str, Any]


HubCreateTaskResult = Annotated[CallToolResult, HubCreateTaskStructured]
HubRefineTaskResult = Annotated[CallToolResult, HubRefineTaskStructured]
HubRefineTasksResult = Annotated[CallToolResult, HubRefineTasksStructured]
HubReadinessTreeResult = Annotated[CallToolResult, HubReadinessTreeStructured]
HubTaskStatusResult = Annotated[CallToolResult, HubTaskStatusStructured]


def structured_echo_result(summary: str, **payload: Any) -> CallToolResult:
    """Read-tool result (#248): human-readable text plus structuredContent
    carrying the machine payload as a real object — no JSON-inside-JSON.
    The text part keeps the {"message": ...} echo shape for backward
    compatibility with clients that parse it."""
    data = with_instance_echo(
        {"schema_version": MCP_STRUCTURED_SCHEMA_VERSION, **payload}
    )
    echo_text = json.dumps(with_instance_echo({"message": summary}), ensure_ascii=False)
    return CallToolResult(
        content=[TextContent(type="text", text=echo_text)],
        structuredContent=data,
    )


def structured_tool_result(summary: str, payload: BaseModel) -> CallToolResult:
    """Return MCP CallToolResult with human text and machine-readable structuredContent."""
    data = with_instance_echo(payload.model_dump(mode="json"))
    echo_text = json.dumps(
        with_instance_echo({"message": summary}),
        ensure_ascii=False,
    )
    return CallToolResult(
        content=[TextContent(type="text", text=echo_text)],
        structuredContent=data,
    )
