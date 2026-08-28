"""hub_my_context: the declared limit covers the whole response (#834).

The defect these tests exist for: ``mode=summary`` capped only the
human-readable digest, while ``structuredContent`` — 99% of the volume —
travelled uncapped. A measured call returned 231 034 characters against the
4 000 the docstring promised, and later 375 095.

Every size assertion here therefore measures the SERIALIZED response, text
part plus structuredContent together. A test that measures the digest alone
passes on the very defect it is supposed to catch.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.types import CallToolResult, TextContent

from hub.mcp_server import hub_my_context


SUMMARY_LIMIT = 4000

# What /api/tasks?mode=summary actually returns (hub/services/dashboard.py::
# list_tasks). Mirrored here so the fake API cannot be more generous than the
# real one.
SUMMARY_CARD_FIELDS = (
    "id",
    "title",
    "status",
    "task_type",
    "parent_id",
    "priority",
    "readiness_score",
    "dor_passed",
)


def _size(result: CallToolResult) -> int:
    """Cost of the whole response: text part plus structuredContent."""
    text = "".join(b.text for b in result.content if isinstance(b, TextContent))
    payload = json.dumps(result.structuredContent or {}, ensure_ascii=False)
    return len(text) + len(payload)


def _text(result: CallToolResult) -> str:
    return "".join(b.text for b in result.content if isinstance(b, TextContent))


def _filler(chars: int) -> str:
    return ("экономия контекста " * ((chars // 19) + 1))[:chars]


def _heavy_task(task_id: int) -> dict[str, Any]:
    """A card the size of a real hub task — about 10 KB (measured on prod)."""
    prose = _filler(1200)
    return {
        "id": task_id,
        "title": f"Задача #{task_id} про ограничение объёма ответа",
        # Live, not completed: this fixture exists to measure the WEIGHT of a
        # card, and since #987 the general digest lists only non-final work —
        # a pile of completed rows would be filtered out and bound nothing.
        "status": "running",
        "task_type": "task",
        "parent_id": 779,
        "priority": "medium",
        "readiness_score": 96,
        "dor_passed": True,
        "description": prose,
        "problem_statement": prose,
        "business_value": prose,
        "technical_hints": prose,
        "rationale": prose,
        "user_story": _filler(300),
        "scope_in": [_filler(150) for _ in range(5)],
        "scope_out": [_filler(150) for _ in range(4)],
        "affected_areas": [f"hub/module_{i}.py" for i in range(6)],
        "acceptance_criteria": [
            {
                "id": f"AC-{i}",
                "given": _filler(200),
                "when": _filler(200),
                "then": _filler(200),
                "verifiable_by": "test",
            }
            for i in range(1, 6)
        ],
        "risks": [
            {"kind": "other", "severity": "medium", "description": _filler(300)}
            for _ in range(3)
        ],
    }


def _identity() -> dict[str, Any]:
    return {
        "username": "pda_claude",
        "role": "agent",
        "principal_id": 6,
        "workspace_mode": "worktree",
    }


def _fake_api(tasks: list[dict[str, Any]], ctx: dict[str, Any] | None = None):
    """Stand-in for _api_get that honours mode=summary the way the API does."""
    calls: list[str] = []

    async def _get(path: str) -> Any:
        calls.append(path)
        if path.startswith("/api/diagnostics/identity"):
            return _identity()
        if path.startswith("/api/tasks?"):
            query = parse_qs(urlparse(path).query)
            if query.get("mode", ["full"])[0] == "summary":
                return {
                    "tasks": [
                        {k: t.get(k) for k in SUMMARY_CARD_FIELDS} for t in tasks
                    ],
                    "next_cursor": None,
                }
            return list(tasks)
        if "/context" in path:
            assert ctx is not None, f"no context fixture for {path}"
            return ctx
        # #989: general digest may GET a live card just for worktree_path.
        # Compact list cards do not carry that field.
        if path.startswith("/api/tasks/") and "?" not in path:
            tid = int(path.rsplit("/", 1)[-1])
            match = next((t for t in tasks if t.get("id") == tid), None)
            if match is None:
                raise AssertionError(f"unexpected API call: {path}")
            return {
                "id": tid,
                "worktree_path": match.get("worktree_path") or "",
            }
        raise AssertionError(f"unexpected API call: {path}")

    _get.calls = calls  # type: ignore[attr-defined]
    return _get


def _heavy_context() -> dict[str, Any]:
    return {
        "task_id": 834,
        "context_text": _filler(2500),
        "breadcrumb": [{"id": 776, "title": _filler(80), "task_type": "epic"}],
        "siblings": [_heavy_task(700 + i) for i in range(4)],
        "children": [_heavy_task(800 + i) for i in range(3)],
        "progress": {"completed": 2, "total": 5, "percent": 40},
        "task": _heavy_task(834),
    }


@pytest.fixture
def many_tasks() -> list[dict[str, Any]]:
    return [_heavy_task(800 + i) for i in range(50)]


async def test_summary_bounds_the_whole_response(
    many_tasks: list[dict[str, Any]],
) -> None:
    """AC-1: the cap covers text + structuredContent, not just the digest."""
    fake = _fake_api(many_tasks)
    with patch("hub.mcp_server._api_get", new=AsyncMock(side_effect=fake)):
        out = await hub_my_context(mode="summary")

    assert _size(out) <= SUMMARY_LIMIT, (
        f"summary response is {_size(out)} chars, promised at most {SUMMARY_LIMIT}"
    )
    # Bounded, not gutted: the digest still answers what it exists to answer.
    text = _text(out)
    assert "Hub Context (no task)" in text
    assert "pda_claude" in text


async def test_full_mode_stays_unbounded(many_tasks: list[dict[str, Any]]) -> None:
    """AC-2: mode=full still drops nothing — every live row is returned.

    #987 changed what a full card costs, not how many arrive: the claimed list
    is fetched as compact cards on this path too, because the digest names id,
    title and status and the filter throws the rest away. Paying ~10 KB a row
    to discard it moved the cost to the server instead of removing it.
    """
    fake = _fake_api(many_tasks)
    with patch("hub.mcp_server._api_get", new=AsyncMock(side_effect=fake)):
        out = await hub_my_context()

    payload = out.structuredContent or {}
    assert len(payload["my_tasks"]) == 50
    assert "bounds" not in payload, "unbounded call must not claim to be bounded"
    claimed = [c for c in fake.calls if c.startswith("/api/tasks?")]  # type: ignore[attr-defined]
    assert claimed and all("mode=summary" in call for call in claimed), (
        "the uncapped path must not fetch full cards it is about to drop"
    )
    assert set(payload["my_tasks"][0]) == set(SUMMARY_CARD_FIELDS)


async def test_dropped_data_is_named(many_tasks: list[dict[str, Any]]) -> None:
    """AC-3: what did not fit is stated with counts, not silently missing."""
    fake = _fake_api(many_tasks)
    with patch("hub.mcp_server._api_get", new=AsyncMock(side_effect=fake)):
        out = await hub_my_context(mode="summary")

    payload = out.structuredContent or {}
    bounds = payload.get("bounds")
    assert bounds, "bounded response says nothing about being bounded"
    assert bounds["total"] == 50
    assert bounds["shown"] == len(payload["my_tasks"])
    assert bounds["shown"] < bounds["total"], "fixture must not fit whole"
    assert bounds["limit"] == SUMMARY_LIMIT
    # The agent reads the text first; the shortfall is named there too.
    assert f"{bounds['shown']}/{bounds['total']}" in _text(out)
    # The digest's own cap (20 lines) stops being silent as well — the two
    # halves may show different amounts, so each says what it counts.
    assert "(20 of 50 shown)" in _text(out)


async def test_task_branch_is_bounded_too() -> None:
    """AC-4: the task_id branch is capped as a whole, not just context_text."""
    fake = _fake_api([], ctx=_heavy_context())
    with patch("hub.mcp_server._api_get", new=AsyncMock(side_effect=fake)):
        out = await hub_my_context(834, mode="summary")

    assert _size(out) <= SUMMARY_LIMIT, (
        f"task context response is {_size(out)} chars, promised at most {SUMMARY_LIMIT}"
    )
    payload = out.structuredContent or {}
    assert payload["bounds"]["dropped"], "pruned context fields must be named"


async def test_explicit_max_chars_bounds_the_whole_response(
    many_tasks: list[dict[str, Any]],
) -> None:
    """An explicit max_chars is a budget for the response, not for its prose."""
    fake = _fake_api(many_tasks)
    with patch("hub.mcp_server._api_get", new=AsyncMock(side_effect=fake)):
        out = await hub_my_context(max_chars=1500)

    assert _size(out) <= 1500


async def test_a_budget_below_the_floor_still_answers(
    many_tasks: list[dict[str, Any]],
) -> None:
    """A limit smaller than the fixed payload shrinks everything it can.

    The floor — identity and the instance echo — is what the caller did not
    make droppable, so it stays; what a budget must never do is raise.
    """
    fake = _fake_api(many_tasks)
    with patch("hub.mcp_server._api_get", new=AsyncMock(side_effect=fake)):
        out = await hub_my_context(max_chars=50)

    payload = out.structuredContent or {}
    assert payload["my_tasks"] == []
    assert payload["bounds"]["shown"] == 0
    assert payload["bounds"]["text_truncated"] is True


async def test_bounded_call_asks_the_api_for_compact_cards(
    many_tasks: list[dict[str, Any]],
) -> None:
    """Cheap at the source: a capped call must not fetch 50 full cards first."""
    fake = _fake_api(many_tasks)
    with patch("hub.mcp_server._api_get", new=AsyncMock(side_effect=fake)):
        await hub_my_context(mode="summary")

    task_calls = [c for c in fake.calls if c.startswith("/api/tasks?")]  # type: ignore[attr-defined]
    assert task_calls and all("mode=summary" in c for c in task_calls)
