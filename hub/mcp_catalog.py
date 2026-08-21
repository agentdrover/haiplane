"""What the MCP catalog actually costs, and the budget that holds it there.

Every agent turn pays for the tool catalog before it pays for anything else:
each published tool ships a description and an input schema, and the server
instruction is repeated by the client once per tool. Epic #776 measured that
tax at 142160 characters and could not say when it had grown, because nothing
was watching.

Two decisions matter here:

* **The budget is measured from the real catalog, never from source.** The
  numbers come from ``tools/list`` as a client receives it, so a docstring
  edit, a new parameter and a new tool all land in the same measurement, and
  none of them can grow the surface while a source-level count stays flat.

* **A budget is a decision, not a limit that drifts.** Exceeding it fails the
  check and prints which tools moved; raising it means editing the committed
  budget file in a reviewed change. That is the whole mechanism: growth stays
  possible and stops being silent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BUDGET_PATH = HERE.parent / "docs" / "agent-context" / "mcp-catalog-budget.json"

# Fields a budget file may set. Anything else is a typo, and a typo in a
# budget file is a budget that silently never applied.
BUDGET_KEYS: tuple[str, ...] = (
    "tools",
    "description_chars",
    "schema_chars",
    "model_visible_chars",
    "max_tool_chars",
)


def _schema_chars(schema: Any) -> int:
    if not schema:
        return 0
    return len(json.dumps(schema, ensure_ascii=False, sort_keys=True))


def snapshot_from_tools(tools: list[Any], *, instructions: str = "") -> dict[str, Any]:
    """Measure an already-fetched ``tools/list`` result.

    ``model_visible_chars`` counts the server instruction once per tool on
    purpose. That repetition is not a hub bug — it is how MCP clients present
    a server — and it was two thirds of the tax epic #776 found, so a
    measurement that omitted it would report the smaller, wrong number.
    """
    instruction_chars = len(instructions or "")
    entries: list[dict[str, Any]] = []
    for tool in tools:
        description = getattr(tool, "description", "") or ""
        schema = getattr(tool, "inputSchema", None)
        desc_chars = len(description)
        sch_chars = _schema_chars(schema)
        entries.append(
            {
                "name": getattr(tool, "name", ""),
                "description_chars": desc_chars,
                "schema_chars": sch_chars,
                "total_chars": desc_chars + sch_chars,
            }
        )
    entries.sort(key=lambda entry: (-entry["total_chars"], entry["name"]))
    description_chars = sum(entry["description_chars"] for entry in entries)
    schema_chars = sum(entry["schema_chars"] for entry in entries)
    return {
        "tools": len(entries),
        "description_chars": description_chars,
        "schema_chars": schema_chars,
        "instruction_chars": instruction_chars,
        "duplicated_instruction_chars": instruction_chars * len(entries),
        "model_visible_chars": description_chars
        + schema_chars
        + instruction_chars * len(entries),
        "max_tool_chars": max((entry["total_chars"] for entry in entries), default=0),
        "tools_list": entries,
    }


async def catalog_snapshot() -> dict[str, Any]:
    """Measure the live MCP catalog exactly as a client would receive it."""
    from hub.mcp_server import mcp

    tools = await mcp.list_tools()
    return snapshot_from_tools(list(tools), instructions=mcp.instructions or "")


def load_budget(path: Path | None = None) -> dict[str, Any]:
    """Read the committed budget. Missing file means no budget, not zero."""
    target = path or BUDGET_PATH
    if not target.exists():
        return {}
    data = json.loads(target.read_text(encoding="utf-8"))
    budgets = data.get("budgets") if isinstance(data, dict) else None
    return budgets if isinstance(budgets, dict) else {}


def load_baseline(path: Path | None = None) -> dict[str, int]:
    """Per-tool sizes recorded with the budget, for the diff on a breach."""
    target = path or BUDGET_PATH
    if not target.exists():
        return {}
    data = json.loads(target.read_text(encoding="utf-8"))
    baseline = data.get("baseline_tools") if isinstance(data, dict) else None
    if not isinstance(baseline, dict):
        return {}
    return {str(name): int(size) for name, size in baseline.items()}


def check_budget(
    snapshot: dict[str, Any],
    budgets: dict[str, Any],
    baseline: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Compare a snapshot against the budget and say what moved.

    A breach answers with the per-tool diff rather than a single total: "the
    catalog grew by 4100 characters" is a number nobody can act on, while
    "hub_refine_task +1200, hub_dispatch_jobs is new" names the change and its
    author.
    """
    breaches: list[dict[str, Any]] = []
    for key in BUDGET_KEYS:
        limit = budgets.get(key)
        if limit is None:
            continue
        actual = int(snapshot.get(key, 0))
        if actual > int(limit):
            breaches.append(
                {
                    "metric": key,
                    "budget": int(limit),
                    "actual": actual,
                    "over_by": actual - int(limit),
                }
            )

    unknown = sorted(set(budgets) - set(BUDGET_KEYS))
    current = {
        entry["name"]: int(entry["total_chars"])
        for entry in snapshot.get("tools_list", [])
    }
    base = baseline or {}
    diff: list[dict[str, Any]] = []
    for name in sorted(set(current) | set(base)):
        before = base.get(name)
        after = current.get(name)
        if before == after:
            continue
        diff.append(
            {
                "tool": name,
                "before": before,
                "after": after,
                "delta": (after or 0) - (before or 0),
            }
        )
    diff.sort(key=lambda row: -abs(row["delta"]))
    return {
        "ok": not breaches and not unknown,
        "breaches": breaches,
        "unknown_budget_keys": unknown,
        "tool_diff": diff,
        "snapshot": {
            key: snapshot.get(key)
            for key in (
                "tools",
                "description_chars",
                "schema_chars",
                "instruction_chars",
                "duplicated_instruction_chars",
                "model_visible_chars",
                "max_tool_chars",
            )
        },
    }


def format_report(result: dict[str, Any], *, limit: int = 15) -> str:
    """Human-readable check result for CI logs."""
    lines: list[str] = []
    snap = result["snapshot"]
    lines.append(
        f"MCP catalog: {snap['tools']} tools, "
        f"descriptions {snap['description_chars']}, "
        f"schemas {snap['schema_chars']}, "
        f"model-visible {snap['model_visible_chars']} chars "
        f"(server instruction {snap['instruction_chars']} × {snap['tools']} "
        f"= {snap['duplicated_instruction_chars']})"
    )
    if result["unknown_budget_keys"]:
        lines.append(
            "Unknown budget keys (a typo here is a budget that never applied): "
            + ", ".join(result["unknown_budget_keys"])
        )
    for breach in result["breaches"]:
        lines.append(
            f"OVER BUDGET {breach['metric']}: {breach['actual']} > "
            f"{breach['budget']} (+{breach['over_by']})"
        )
    if result["tool_diff"]:
        lines.append("Changed tools vs baseline:")
        for row in result["tool_diff"][:limit]:
            if row["before"] is None:
                lines.append(f"  + {row['tool']}: new, {row['after']} chars")
            elif row["after"] is None:
                lines.append(f"  - {row['tool']}: removed, was {row['before']} chars")
            else:
                lines.append(
                    f"  ~ {row['tool']}: {row['before']} → {row['after']} "
                    f"({row['delta']:+d})"
                )
        remaining = len(result["tool_diff"]) - limit
        if remaining > 0:
            lines.append(f"  … and {remaining} more")
    if result["ok"]:
        lines.append("Catalog within budget.")
    else:
        lines.append(
            "Catalog over budget. Trim the surface, or raise the budget in "
            f"{BUDGET_PATH.name} as a reviewed decision."
        )
    return "\n".join(lines)
