#!/usr/bin/env python3
"""CI check: the MCP catalog stays inside its committed budget (#780).

Serializes the real ``tools/list`` — the same bytes a client receives — and
compares it with ``docs/agent-context/mcp-catalog-budget.json``. Over budget
exits non-zero and prints which tools moved and by how much, so the red check
names the change instead of only its total.

    uv run python scripts/mcp_catalog_budget.py            # check
    uv run python scripts/mcp_catalog_budget.py --json     # machine-readable
    uv run python scripts/mcp_catalog_budget.py --update   # re-freeze budget

``--update`` is the deliberate act of raising the budget: it rewrites the file
so the change shows up in a diff and goes through review, which is the only
reason a budget is worth having.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.mcp_catalog import (  # noqa: E402
    BUDGET_PATH,
    catalog_snapshot,
    check_budget,
    format_report,
    load_baseline,
    load_budget,
)


def _write_budget(snapshot: dict, path: Path, measured_at: str) -> None:
    existing = {}
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
    doc = {
        "_comment": existing.get("_comment", []),
        "measured_at": measured_at,
        "budgets": {
            "tools": snapshot["tools"],
            "description_chars": snapshot["description_chars"],
            "schema_chars": snapshot["schema_chars"],
            "model_visible_chars": snapshot["model_visible_chars"],
            "max_tool_chars": snapshot["max_tool_chars"],
        },
        "baseline_tools": {
            entry["name"]: entry["total_chars"]
            for entry in sorted(snapshot["tools_list"], key=lambda e: e["name"])
        },
    }
    path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument(
        "--update",
        action="store_true",
        help="re-freeze the budget at the current catalog (reviewed decision)",
    )
    parser.add_argument(
        "--measured-at",
        default="",
        help="date stamp written by --update (default: keep the existing one)",
    )
    parser.add_argument("--budget-file", default=str(BUDGET_PATH))
    args = parser.parse_args()

    path = Path(args.budget_file)
    snapshot = await catalog_snapshot()

    if args.update:
        existing_stamp = ""
        if path.exists():
            existing_stamp = json.loads(path.read_text(encoding="utf-8")).get(
                "measured_at", ""
            )
        _write_budget(snapshot, path, args.measured_at or existing_stamp)
        print(f"Budget re-frozen at the current catalog in {path}")
        return 0

    budgets = load_budget(path)
    if not budgets:
        print(
            f"No budget found in {path}. Run with --update to freeze the "
            "current catalog as the budget.",
            file=sys.stderr,
        )
        return 2

    result = check_budget(snapshot, budgets, load_baseline(path))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_report(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
