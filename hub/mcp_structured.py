"""Structured MCP tool outputs (schema_version + structuredContent payloads)."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field
from mcp.types import CallToolResult, TextContent

MCP_STRUCTURED_SCHEMA_VERSION = "1"


class HubCreateTaskStructured(BaseModel):
    schema_version: str = Field(default=MCP_STRUCTURED_SCHEMA_VERSION)
    task: dict[str, Any]


class HubRefineTaskStructured(BaseModel):
    schema_version: str = Field(default=MCP_STRUCTURED_SCHEMA_VERSION)
    task_id: int
    updated_columns: dict[str, Any] = Field(default_factory=dict)
    refine_result: dict[str, Any] | None = None
    no_op: bool = False


class HubTaskStatusStructured(BaseModel):
    schema_version: str = Field(default=MCP_STRUCTURED_SCHEMA_VERSION)
    task: dict[str, Any]


HubCreateTaskResult = Annotated[CallToolResult, HubCreateTaskStructured]
HubRefineTaskResult = Annotated[CallToolResult, HubRefineTaskStructured]
HubTaskStatusResult = Annotated[CallToolResult, HubTaskStatusStructured]


def structured_tool_result(summary: str, payload: BaseModel) -> CallToolResult:
    """Return MCP CallToolResult with human text and machine-readable structuredContent."""
    return CallToolResult(
        content=[TextContent(type="text", text=summary)],
        structuredContent=payload.model_dump(mode="json"),
    )
