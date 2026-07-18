"""Tests for task tree/context output limits (#110)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub.services.tree_output import (
    TRUNCATION_NOTICE,
    TreeOutputOptions,
    apply_tree_limits,
    render_task_tree,
    truncate_text,
)


def _deep_tree(depth: int, width: int = 1, _id: list[int] | None = None) -> dict:
    counter = _id if _id is not None else [1]
    node_id = counter[0]
    counter[0] += 1
    if depth <= 0:
        return {
            "id": node_id,
            "title": f"node-{node_id}",
            "task_type": "task",
            "status": "open",
            "children": [],
        }
    return {
        "id": node_id,
        "title": f"node-{node_id}",
        "task_type": "task",
        "status": "open",
        "children": [_deep_tree(depth - 1, width, counter) for _ in range(width)],
    }


def test_depth_limit_excludes_deeper_nodes():
    tree = _deep_tree(depth=5)
    limited, truncated = apply_tree_limits(tree, TreeOutputOptions(depth=2))
    assert truncated is True

    def max_depth(node: dict, current: int = 0) -> int:
        if not node.get("children"):
            return current
        return max(max_depth(child, current + 1) for child in node["children"])

    assert max_depth(limited) <= 2


def test_max_chars_appends_truncated_notice():
    text = "x" * 100
    trimmed, truncated = truncate_text(text, 40)
    assert truncated is True
    assert trimmed.endswith(TRUNCATION_NOTICE)
    assert len(trimmed) <= 40 + len(TRUNCATION_NOTICE)


def test_render_task_tree_without_limits_unchanged_shape():
    tree = _deep_tree(depth=2)
    rendered = render_task_tree(tree, TreeOutputOptions())
    assert rendered.truncated is False
    assert TRUNCATION_NOTICE not in rendered.text
    assert "#1" in rendered.text


@pytest.mark.asyncio
async def test_api_tree_depth_query(client: AsyncClient, db):
    epic_id = await repo.create_task(
        db,
        title="epic",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=False,
        task_type="epic",
        parent_id=None,
        priority="medium",
    )
    feature_id = await repo.create_task(
        db,
        title="feature",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=False,
        task_type="feature",
        parent_id=epic_id,
        priority="medium",
    )
    task_id = await repo.create_task(
        db,
        title="task",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=feature_id,
        priority="medium",
    )
    await repo.create_task(
        db,
        title="sub",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=False,
        task_type="subtask",
        parent_id=task_id,
        priority="medium",
    )
    await db.commit()

    full = await client.get(f"/api/tasks/{epic_id}/tree")
    assert full.status_code == 200
    assert full.json()["children"]

    limited = await client.get(f"/api/tasks/{epic_id}/tree?depth=1")
    assert limited.status_code == 200
    assert limited.headers.get("X-Hub-Truncated") == "true"
    assert limited.json()["children"]
    assert all(not child.get("children") for child in limited.json()["children"])


@pytest.mark.asyncio
async def test_api_context_without_params_unchanged(client: AsyncClient):
    resp = await client.post("/api/tasks", json={"title": "ctx"})
    task_id = resp.json()["id"]
    baseline = await client.get(f"/api/tasks/{task_id}/context")
    assert baseline.status_code == 200
    assert "truncated" not in baseline.json()["context_text"].lower()

    capped = await client.get(f"/api/tasks/{task_id}/context?max_chars=50")
    assert capped.status_code == 200
    assert TRUNCATION_NOTICE in capped.json()["context_text"]
