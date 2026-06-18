"""Limit and format task hierarchy output for MCP/REST consumers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

TreeOutputMode = Literal["full", "summary"]
TRUNCATION_NOTICE = "[truncated]"


@dataclass(frozen=True)
class TreeOutputOptions:
    depth: int | None = None
    max_nodes: int | None = None
    max_chars: int | None = None
    mode: TreeOutputMode = "full"

    def effective_limits(self) -> tuple[int | None, int | None]:
        depth = self.depth
        max_nodes = self.max_nodes
        if self.mode == "summary":
            if depth is None:
                depth = 2
            if max_nodes is None:
                max_nodes = 50
        return depth, max_nodes


@dataclass(frozen=True)
class TreeRenderResult:
    tree: dict[str, Any]
    text: str
    truncated: bool


def _prune_depth(
    node: dict[str, Any], current_depth: int, max_depth: int | None
) -> dict[str, Any]:
    if max_depth is not None and current_depth >= max_depth:
        return {**node, "children": []}
    children = [
        _prune_depth(child, current_depth + 1, max_depth)
        for child in node.get("children", [])
    ]
    return {**node, "children": children}


def _prune_nodes(node: dict[str, Any], remaining: list[int]) -> dict[str, Any] | None:
    if remaining[0] <= 0:
        return None
    remaining[0] -= 1
    kept_children: list[dict[str, Any]] = []
    for child in node.get("children", []):
        if remaining[0] <= 0:
            break
        pruned = _prune_nodes(child, remaining)
        if pruned is not None:
            kept_children.append(pruned)
    return {**node, "children": kept_children}


def apply_tree_limits(
    tree: dict[str, Any], options: TreeOutputOptions
) -> tuple[dict[str, Any], bool]:
    """Return a possibly pruned tree copy and whether any limit was applied."""
    depth_limit, node_limit = options.effective_limits()
    truncated = False
    result = tree

    if depth_limit is not None:
        before = _count_nodes(result)
        result = _prune_depth(result, 0, depth_limit)
        if _count_nodes(result) < before or _has_deeper_nodes(tree, depth_limit):
            truncated = True

    if node_limit is not None:
        remaining = [node_limit]
        pruned = _prune_nodes(result, remaining)
        if pruned is None:
            pruned = {**result, "children": []}
        if _count_nodes(pruned) < _count_nodes(result):
            truncated = True
        result = pruned

    return result, truncated


def _count_nodes(node: dict[str, Any]) -> int:
    return 1 + sum(_count_nodes(child) for child in node.get("children", []))


def _has_deeper_nodes(
    node: dict[str, Any], max_depth: int, current_depth: int = 0
) -> bool:
    if current_depth >= max_depth and node.get("children"):
        return True
    return any(
        _has_deeper_nodes(child, max_depth, current_depth + 1)
        for child in node.get("children", [])
    )


def format_tree_lines(node: dict[str, Any], indent: int = 0) -> list[str]:
    prefix = "  " * indent
    tt = node.get("task_type", "task")
    progress = node.get("progress")
    prog_str = ""
    if progress and progress.get("total", 0) > 0:
        prog_str = (
            f" ({progress['completed']}/{progress['total']} = {progress['percent']}%)"
        )
    lines = [
        f"{prefix}[{tt}] #{node['id']} {node['title']} — {node['status']}{prog_str}"
    ]
    for child in node.get("children", []):
        lines.extend(format_tree_lines(child, indent + 1))
    return lines


def truncate_text(text: str, max_chars: int | None) -> tuple[str, bool]:
    """Cap ``text`` to ``max_chars`` code points, appending a truncation notice.

    Length is measured in Unicode code points (``len(str)``), not UTF-8 bytes.
    """
    if max_chars is None or len(text) <= max_chars:
        return text, False
    if max_chars <= len(TRUNCATION_NOTICE) + 1:
        return TRUNCATION_NOTICE, True
    trimmed = text[: max_chars - len(TRUNCATION_NOTICE) - 1].rstrip()
    return f"{trimmed}\n{TRUNCATION_NOTICE}", True


def render_task_tree(
    tree: dict[str, Any], options: TreeOutputOptions
) -> TreeRenderResult:
    limited_tree, truncated = apply_tree_limits(tree, options)
    text = "\n".join(format_tree_lines(limited_tree))
    text, char_truncated = truncate_text(text, options.max_chars)
    combined = truncated or char_truncated
    if combined and TRUNCATION_NOTICE not in text:
        text = f"{text}\n{TRUNCATION_NOTICE}" if text else TRUNCATION_NOTICE
    return TreeRenderResult(
        tree=limited_tree,
        text=text,
        truncated=combined,
    )
