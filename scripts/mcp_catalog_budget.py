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
    WORKING_FREEZE,
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
        # #1071: the limit that actually REFUSES a new contract field, published
        # here because this is the file agents read before planning. The
        # ceilings above are looser by design — a percentage headroom catches
        # slow drift — and reading only them is how work gets planned that does
        # not fit: on 2026-08-29 the file advertised 3374 characters of room
        # while the freeze left 92.
        "working_freeze": dict(WORKING_FREEZE),
        "working_headroom_note": (
            "ЧИТАЙ ЭТО ПЕРЕД ТЕМ, КАК ДОБАВЛЯТЬ ОПИСАНИЕ. Новое описание "
            "отвергает working_freeze, а не budgets: budgets — потолок с "
            "процентным запасом, он ловит долгий дрейф и сегодня вас НЕ "
            "остановит. Планируйте по остатку от ЖИВОГО каталога, а не по "
            "разности с measured: measured — состояние на момент последней "
            "заморозки, и разность с ним больше живого остатка. Живой остаток "
            "печатает scripts/mcp_catalog_budget.py первой же секцией. "
            "Заморозка двигается только вниз, это отдельное решение, и новое "
            "описание оплачивается подрезкой. Источник обоих чисел — "
            "hub/mcp_catalog.py; --update переписывает measured, budgets и "
            "baseline_tools целиком, это полная перезаморозка, а не правка "
            "одного ключа."
        ),
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
        print(
            f"FULL RE-FREEZE of {path}. Rewritten from the live catalog: "
            f"measured, the percentage ceilings (+{headroom}% headroom), "
            "baseline_tools and measured_at. Copied from hub/mcp_catalog.py: "
            "working_freeze. If you only meant to republish the working "
            "freeze, edit that one key instead — this run also moved the "
            "ceilings, and when the catalog has grown they moved UP."
        )
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
