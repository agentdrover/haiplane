#!/usr/bin/env python3
"""CI check: the MCP catalog stays inside its committed budget (#780).

Serializes the real ``tools/list`` — the same bytes a client receives — and
compares it with ``docs/agent-context/mcp-catalog-budget.json``. Over budget
exits non-zero and prints which tools moved and by how much, so the red check
names the change instead of only its total.

    uv run python scripts/mcp_catalog_budget.py            # check
    uv run python scripts/mcp_catalog_budget.py --json     # machine-readable
    uv run python scripts/mcp_catalog_budget.py --update   # re-freeze budget

``--update`` is the deliberate act of setting the ceiling: it rewrites the file
so the change shows up in a diff and goes through review, which is the only
reason a budget is worth having.

Ceilings carry declared headroom (``--headroom-pct``, 10 by default) so that
ordinary delivery of a tool fits underneath and never has to touch this file —
an exactly-frozen budget made every tool-adding branch edit the same five
numbers, and any two such branches then conflicted by construction (#829).
The measured values are stored next to the ceilings so every run can print how
much headroom is left.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
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
    load_measured,
)


MEASURED_KEYS = (
    "tools",
    "description_chars",
    "schema_chars",
    "model_visible_chars",
    "max_tool_chars",
)


def _ceiling(value: int, headroom_pct: float) -> int:
    """Round a measured value up into a ceiling with declared headroom."""
    return int(math.ceil(value * (1 + headroom_pct / 100)))


def _write_budget(
    snapshot: dict, path: Path, measured_at: str, headroom_pct: float
) -> None:
    existing = {}
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
    measured = {key: snapshot[key] for key in MEASURED_KEYS}
    doc = {
        "_comment": existing.get("_comment", []),
        "measured_at": measured_at,
        "headroom_pct": headroom_pct,
        # What the catalog actually was when the ceiling was set. Kept beside
        # the ceilings so "how much headroom is left" is arithmetic on
        # recorded facts rather than a number someone remembers.
        "measured": measured,
        "budgets": {
            key: _ceiling(measured[key], headroom_pct) for key in MEASURED_KEYS
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
    parser.add_argument(
        "--headroom-pct",
        type=float,
        default=None,
        help=(
            "headroom above the measured catalog when writing ceilings "
            "(default: keep the file's value, or 10)"
        ),
    )
    parser.add_argument("--budget-file", default=str(BUDGET_PATH))
    args = parser.parse_args()

    path = Path(args.budget_file)
    snapshot = await catalog_snapshot()

    if args.update:
        existing_stamp, existing_headroom = "", 10.0
        if path.exists():
            existing_doc = json.loads(path.read_text(encoding="utf-8"))
            existing_stamp = existing_doc.get("measured_at", "")
            existing_headroom = float(existing_doc.get("headroom_pct", 10.0))
        headroom = (
            args.headroom_pct if args.headroom_pct is not None else existing_headroom
        )
        _write_budget(snapshot, path, args.measured_at or existing_stamp, headroom)
        print(f"Ceilings set in {path}: measured catalog + {headroom}% headroom")
        return 0

    budgets = load_budget(path)
    if not budgets:
        print(
            f"No budget found in {path}. Run with --update to freeze the "
            "current catalog as the budget.",
            file=sys.stderr,
        )
        return 2

    result = check_budget(snapshot, budgets, load_baseline(path), load_measured(path))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_report(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
