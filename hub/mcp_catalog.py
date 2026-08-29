"""What the MCP catalog actually costs, and the budget that holds it there.

Every agent turn pays for the tool catalog before it pays for anything else:
each published tool ships a description and an input schema, and the server
ships one instruction block on top.

**How much the instruction costs depends on the client, and that was assumed
rather than measured (#815).** Epic #776 put the catalog at 142160 characters
and attributed 95940 of them (67.5%) to the server instruction being repeated
once per tool. The repetition is not something the hub does: no tool
description contains the instruction — the check for that is a test. Whether a
client repeats it when composing the model's context is the client's business,
and it differs:

* **Claude Code presents it once**, as a single "MCP Server Instructions"
  section, with tool descriptions carrying only their own text. Measured from
  inside a live session (#815).
* **Cursor presents it once too** — one ``serverUseInstructions`` at server
  level, and ``GetMcpTools`` returning one ``serverDescription`` followed by
  tool descriptions that start with their own text. Measured from inside a
  live Cursor session and recorded on #815.

Both target clients therefore pay descriptions + schemas + one instruction, and
``model_visible_chars`` counts exactly that. The per-tool repetition the epic
assumed is still reported as
``model_visible_chars_if_instruction_per_tool``: no client has been seen doing
it, but the field costs nothing and a third client is not obliged to behave
like the two that were measured.

Three more decisions matter here:

* **A budget is a ceiling with declared headroom, not an exact freeze
  (#829).** An exact freeze meant every task that added a tool had to edit
  this one file, so any two such branches conflicted by construction — on
  #815 that cost three resubmission rounds, none of which touched the work
  being reviewed. A ceiling with headroom keeps the file untouched by
  ordinary delivery, and a file nobody edits cannot serialize anybody.
  Headroom is the price: it is declared, and every run prints how much of it
  is left, because slack that nobody watches is how a check quietly stops
  checking.

* **The budget is measured from the real catalog, never from source.** The
  numbers come from ``tools/list`` as a client receives it, so a docstring
  edit, a new parameter and a new tool all land in the same measurement, and
  none of them can grow the surface while a source-level count stays flat.

* **A budget is a decision, not a limit that drifts.** Exceeding the ceiling
  fails the check and prints which tools moved; raising the ceiling means
  editing the committed budget file in a reviewed change. That is the whole
  mechanism: growth stays possible and stops being silent.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BUDGET_PATH = HERE.parent / "docs" / "agent-context" / "mcp-catalog-budget.json"

# The limit that actually BINDS a contract field, and the only one an author
# needs before planning work (#1071).
#
# There are two guards and they are not the same. ``budgets`` in the file is a
# ceiling with a percentage headroom (10%), which catches slow drift over
# months. WORKING_FREEZE is tighter and catches growth the same day. Whoever
# adds a field hits the second one — and until now it lived only inside
# tests/test_mcp_catalog_budget.py, while docs/agent-context advertised the
# looser number. On 2026-08-29 that cost a docstring rewrite for 7 characters
# of room that the budget file said was 3374 (#911).
#
# It lives here so the test, the report and the published file read ONE value.
# Moving it is a deliberate act: a freeze may only go DOWN, and the history in
# tests/test_mcp_catalog_budget.py (``WORKING_FREEZE_HISTORY``) is what pins
# the direction — ``test_a_freeze_never_moves_up`` reads it, and
# ``test_the_freeze_history_records_the_current_value`` stops the history from
# being left behind. An earlier draft of this comment named a test that did
# not exist, which is the same defect this task is about: a promise in prose
# with nothing behind it.
WORKING_FREEZE = {
    "description_chars": 36383,
    "max_tool_chars": 6404,
}

# WHERE THE BINDING LIMIT IS DECLARED. One string, so the refusal, the report
# and the published note cannot drift into naming different places — the
# refusal used to send authors to a constant in the test file, which is both
# the wrong place now and an editable alias: raising it there would have
# detached the live check from this value without a single test going red.
FREEZE_SOURCE = (
    "WORKING_FREEZE in hub/mcp_catalog.py, published as 'working_freeze' in "
    "docs/agent-context/mcp-catalog-budget.json"
)


def declared_headroom(path: Path | None = None) -> dict[str, int]:
    """The slack a freeze was GIVEN: freeze minus the recorded measurement.

    Answers "how much of the working headroom has been spent since the freeze
    was set", so it deliberately does not move when the live catalog does.
    Derived rather than written down: the distance used to be prose beside the
    constant ("live 35383 + 1000 working headroom") and was wrong in both
    halves — the same commit recorded live as 36332, and the real distance
    from the file's ``measured`` is 324. Nothing checked either, so nothing
    caught them (#1071).

    NOT the number to plan work by. That is :func:`room_left`.
    """
    measured = load_measured(path)
    return {
        key: freeze - measured[key]
        for key, freeze in WORKING_FREEZE.items()
        if key in measured
    }


def room_left(snapshot: Mapping[str, Any]) -> dict[str, int]:
    """Characters an author may still add before the freeze refuses — from LIVE.

    The number that will actually stop you, and therefore the one to plan by.
    Keeping only :func:`declared_headroom` would have repeated this task's own
    defect one order of magnitude smaller: an author reading 324 while the
    live catalog leaves 92 plans a field that does not fit, exactly as reading
    the file's 3374 did on #911.
    """
    return {key: freeze - int(snapshot[key]) for key, freeze in WORKING_FREEZE.items()}


def freeze_refusal(metric: str, live: int) -> str:
    """The one refusal text, shared by the test that fails and the report.

    Says what binds, by how much it was missed, where the number is declared
    and what to do — and warns off ``--update``, which is a full re-freeze:
    it rewrites ``measured`` to the live catalog, recomputes the percentage
    ceilings from it (RAISING them when the catalog has grown) and rewrites
    ``baseline_tools``.
    """
    freeze = WORKING_FREEZE[metric]
    return (
        f"{metric}: the catalog is {live}, over the working freeze {freeze} by "
        f"{live - freeze + 1}. This is the limit that binds — the 'budgets' "
        "ceiling carries a percentage headroom, catches slow drift and will "
        f"NOT stop you today. Declared in {FREEZE_SOURCE}. Pay for the new "
        "text with a trim; lowering the freeze after a trim is a deliberate "
        "act that adds a line to WORKING_FREEZE_HISTORY. Do NOT reach for "
        "--update: it re-freezes measured, the percentage ceilings and "
        "baseline_tools all at once."
    )


# Fields a budget file may set. Anything else is a typo, and a typo in a
# budget file is a budget that silently never applied.
# How much of the DECLARED HEADROOM may be eaten before the report starts
# saying so. Measured against the headroom rather than against the ceiling on
# purpose: with a 10% ceiling every healthy catalog sits at ~91% of it on day
# one, so a ceiling-relative warning would fire from the first run and teach
# everyone to ignore it. Headroom-relative starts at 0% the moment a ceiling
# is set and answers the question that matters — how much slack has been spent
# since someone last decided. Not a failure: the point is to see it going
# while there is still time to choose.
HEADROOM_WARN_PCT = 75.0

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

    ``model_visible_chars`` counts the server instruction once, which is what
    both measured clients actually do (#815). The per-tool repetition the epic
    assumed is still reported, as
    ``model_visible_chars_if_instruction_per_tool``: it is the ceiling for a
    client nobody has measured yet, and dropping it would leave the surface
    with no upper bound at all.
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
        # The client-independent part: what every client pays whatever it does
        # with the instruction.
        "catalog_chars": description_chars + schema_chars,
        "duplicated_instruction_chars": instruction_chars * len(entries),
        "model_visible_chars": description_chars + schema_chars + instruction_chars,
        "model_visible_chars_if_instruction_per_tool": description_chars
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


def load_measured(path: Path | None = None) -> dict[str, int]:
    """The catalog as it was when the ceilings were set (#829).

    Without it a report can still say how much room is left, but not how much
    of the slack has been spent — and the second number is the one that says
    whether anybody needs to act.
    """
    target = path or BUDGET_PATH
    if not target.exists():
        return {}
    data = json.loads(target.read_text(encoding="utf-8"))
    measured = data.get("measured") if isinstance(data, dict) else None
    if not isinstance(measured, dict):
        return {}
    return {str(key): int(value) for key, value in measured.items()}


def load_baseline(path: Path | None = None) -> dict[str, int]:
    """Per-tool sizes at the LAST deliberate freeze, for the diff on a breach.

    Not a snapshot of every commit: ordinary delivery no longer touches this
    file (#829), so the diff answers "what has moved since someone last
    decided the ceiling" — which is the question a breach actually raises.
    """
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
    measured: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Compare a snapshot against the budget and say what moved.

    A breach answers with the per-tool diff rather than a single total: "the
    catalog grew by 4100 characters" is a number nobody can act on, while
    "hub_refine_task +1200, hub_dispatch_jobs is new" names the change and its
    author.
    """
    breaches: list[dict[str, Any]] = []
    headroom: list[dict[str, Any]] = []
    for key in BUDGET_KEYS:
        limit = budgets.get(key)
        if limit is None:
            continue
        actual = int(snapshot.get(key, 0))
        ceiling = int(limit)
        if actual > ceiling:
            breaches.append(
                {
                    "metric": key,
                    "budget": ceiling,
                    "actual": actual,
                    "over_by": actual - ceiling,
                }
            )
        # Reported whether or not anything breached: headroom bought the
        # mergeability, and the only thing that keeps it honest is seeing it
        # shrink (#829).
        base = (measured or {}).get(key)
        # Одной веткой вместо двух условных выражений: "span > 0" подразумевало
        # наличие base, но говорило об этом только автору.
        if base is None:
            span = 0
            used_pct = None
        else:
            span = ceiling - int(base)
            used_pct = (
                round(100 * max(0, actual - int(base)) / span, 1) if span > 0 else None
            )
        headroom.append(
            {
                "metric": key,
                "actual": actual,
                "ceiling": ceiling,
                "measured": int(base) if base is not None else None,
                "remaining": ceiling - actual,
                "used_pct": used_pct,
            }
        )

    unknown = sorted(set(budgets) - set(BUDGET_KEYS))
    current = {
        entry["name"]: int(entry["total_chars"])
        for entry in snapshot.get("tools_list", [])
    }
    baseline_tools = baseline or {}
    diff: list[dict[str, Any]] = []
    for name in sorted(set(current) | set(baseline_tools)):
        before = baseline_tools.get(name)
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
        "headroom": headroom,
        "unknown_budget_keys": unknown,
        "tool_diff": diff,
        "snapshot": {
            key: snapshot.get(key)
            for key in (
                "tools",
                "description_chars",
                "schema_chars",
                "instruction_chars",
                "catalog_chars",
                "duplicated_instruction_chars",
                "model_visible_chars",
                "model_visible_chars_if_instruction_per_tool",
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
        f"(instruction {snap['instruction_chars']} counted once — measured "
        f"client behaviour; a client repeating it per tool would pay "
        f"{snap['model_visible_chars_if_instruction_per_tool']})"
    )
    freeze_rows = room_left(snap)
    lines.append(
        "Working freeze — the limit that BINDS (the ceilings below are looser "
        "by design):"
    )
    for metric in sorted(freeze_rows):
        left = freeze_rows[metric]
        state = f"{left} left" if left > 0 else f"OVER by {1 - left}"
        lines.append(
            f"   {metric}: {snap[metric]} / {WORKING_FREEZE[metric]} ({state})"
        )
    over = [m for m, left in freeze_rows.items() if left <= 0]
    for metric in sorted(over):
        lines.append(freeze_refusal(metric, int(snap[metric])))
    if result["unknown_budget_keys"]:
        lines.append(
            "Unknown budget keys (a typo here is a budget that never applied): "
            + ", ".join(result["unknown_budget_keys"])
        )
    if result.get("headroom"):
        lines.append("Headroom left under each ceiling:")
        for row in result["headroom"]:
            used = row["used_pct"]
            marker = "  ⚠" if used is not None and used >= HEADROOM_WARN_PCT else "   "
            spent = f"{used}% of headroom spent" if used is not None else "no baseline"
            lines.append(
                f"{marker} {row['metric']}: {row['actual']} / {row['ceiling']} "
                f"({spent}, {row['remaining']} left)"
            )
        tight = [
            r["metric"]
            for r in result["headroom"]
            if r["used_pct"] is not None and r["used_pct"] >= HEADROOM_WARN_PCT
        ]
        if tight:
            lines.append(
                "Headroom nearly gone on: "
                + ", ".join(tight)
                + " — decide now whether to trim the surface or raise the "
                "ceiling, rather than at the next red check."
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
