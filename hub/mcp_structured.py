"""Structured MCP tool outputs (schema_version + structuredContent payloads)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Annotated, Any

from pydantic import BaseModel, Field
from mcp.types import CallToolResult, TextContent

from hub.hub_instance import with_instance_echo
from hub.services.tree_output import TRUNCATION_NOTICE, truncate_text

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


def _echo_parts(summary: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The exact two halves structured_echo_result would hand the client."""
    data = with_instance_echo(
        {"schema_version": MCP_STRUCTURED_SCHEMA_VERSION, **payload}
    )
    echo_text = json.dumps(with_instance_echo({"message": summary}), ensure_ascii=False)
    return echo_text, data


def echo_result_size(summary: str, payload: dict[str, Any]) -> int:
    """Serialized cost of a read-tool result: text part plus structuredContent.

    Measured on the same objects the client receives, because the halves are
    paid for together and a limit that covers only one of them is not a limit
    (#834).
    """
    echo_text, data = _echo_parts(summary, payload)
    return len(echo_text) + len(json.dumps(data, ensure_ascii=False))


def _path_get(payload: dict[str, Any], path: str) -> Any:
    node: Any = payload
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _path_edit(payload: dict[str, Any], path: str, value: Any, *, drop: bool) -> Any:
    """Return a copy of ``payload`` with ``path`` replaced or removed.

    Only the dicts along the path are copied — the untouched branches are
    shared, so a rejected trial costs nothing beyond its own spine.
    """
    key, _, rest = path.partition(".")
    if not isinstance(payload, dict) or key not in payload:
        return payload
    updated = dict(payload)
    if rest:
        updated[key] = _path_edit(updated[key], rest, value, drop=drop)
    elif drop:
        del updated[key]
    else:
        updated[key] = value
    return updated


def _bounds_line(bounds: dict[str, Any]) -> str:
    parts = [f"limit {bounds['limit']} chars for the whole response"]
    if "field" in bounds:
        # Named as a payload path, because the digest above may well list more
        # than the payload carries — each half says what it counts.
        parts.append(
            f"structuredContent.{bounds['field']} {bounds['shown']}/{bounds['total']}"
        )
    if bounds.get("dropped"):
        parts.append("dropped: " + ", ".join(bounds["dropped"]))
    return "[bounded] " + "; ".join(parts)


def fit_echo_result(
    summary: str,
    max_chars: int | None,
    *,
    shrink: str = "",
    drop_order: Sequence[str] = (),
    **payload: Any,
) -> CallToolResult:
    """structured_echo_result whose WHOLE serialization fits ``max_chars`` (#834).

    ``shrink`` names a list field (dotted path) that may lose its tail;
    ``drop_order`` names fields droppable outright, cheapest first, once
    trimming the list is not enough. What did not fit is stated in ``bounds``
    and in the text — silence about what was cut reads as "everything is
    here" (#519, #810).

    The floor is whatever the caller left undroppable: if that alone is over
    the limit, the response is as small as this function may make it and
    ``bounds`` still names every cut. It is a budget, not a guillotine.
    """
    if max_chars is None:
        return structured_echo_result(summary, **payload)

    items = _path_get(payload, shrink) if shrink else None
    if not isinstance(items, list):
        items, shrink = None, ""
    total = len(items) if items is not None else 0

    def render(
        keep: int, dropped: tuple[str, ...], text: str, truncated: bool
    ) -> tuple[str, dict[str, Any]]:
        data: dict[str, Any] = payload
        if items is not None:
            data = _path_edit(data, shrink, items[:keep], drop=False)
        for path in dropped:
            data = _path_edit(data, path, None, drop=True)
        if not dropped and keep == total and not truncated:
            return text, data
        bounds: dict[str, Any] = {"limit": max_chars}
        if shrink:
            bounds |= {"field": shrink, "shown": keep, "total": total}
        if dropped:
            bounds["dropped"] = list(dropped)
        if truncated:
            bounds["text_truncated"] = True
        return f"{text}\n{_bounds_line(bounds)}", {**data, "bounds": bounds}

    def fits(keep: int, dropped: tuple[str, ...], text: str, truncated: bool) -> bool:
        return echo_result_size(*render(keep, dropped, text, truncated)) <= max_chars

    # 1. Everything, then the largest tail of the list that still fits.
    if fits(total, (), summary, False):
        keep = total
    else:
        low, high = 0, total
        while low < high:
            mid = (low + high + 1) // 2
            if fits(mid, (), summary, False):
                low = mid
            else:
                high = mid - 1
        keep = low

    # 2. Whole fields, in the declared order, while the response is still over.
    dropped: tuple[str, ...] = ()
    for path in drop_order:
        if fits(keep, dropped, summary, False):
            break
        if _path_get(payload, path) is None:
            continue
        dropped += (path,)

    # 3. Last resort: the prose itself. Bisect on the text, because JSON
    #    escaping means its cost is not its length.
    if fits(keep, dropped, summary, False):
        text, data = render(keep, dropped, summary, False)
        return structured_echo_result(text, **data)
    low, high = 0, len(summary)
    while low < high:
        mid = (low + high + 1) // 2
        trimmed, _ = truncate_text(summary, mid)
        if fits(keep, dropped, trimmed, True):
            low = mid
        else:
            high = mid - 1
    text, _ = truncate_text(summary, low)
    if TRUNCATION_NOTICE not in text:
        text = TRUNCATION_NOTICE
    text, data = render(keep, dropped, text, True)
    return structured_echo_result(text, **data)


def structured_error_result(payload: dict[str, Any]) -> CallToolResult:
    """Отказ инструмента в том же виде, в каком приходит успех (#895).

    Текстовая часть — ровно та строка, которую FastMCP делал из возвращаемой
    ``str``: плоский JSON payload, БЕЗ echo-обёртки ``{"message": ...}``,
    которой заворачиваются успехи. Клиент, читающий текст, не видит разницы.

    ``isError`` намеренно не выставляется. Сегодня его нет, отказ разбирают по
    полю ``reason`` в payload, а клиенты трактуют ``isError`` как протокольный
    сбой и обрабатывают отдельным путём. Поставить его значило бы поменять
    поведение, тогда как задача — выпрямить объявленный тип. Решение владельца
    от 22.08.2026; если понадобится обратное, это отдельное изменение.
    """
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))
        ],
        structuredContent=payload,
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
