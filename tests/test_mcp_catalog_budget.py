"""MCP catalog budget (#780, AC-3): the surface cannot grow silently.

The check is deliberately run here against the REAL catalog, not a fixture: a
budget verified against a hand-written tool list would stay green while the
published ``tools/list`` grew, which is the exact failure it exists to prevent.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from hub.mcp_catalog import (
    BUDGET_KEYS,
    BUDGET_PATH,
    catalog_snapshot,
    check_budget,
    format_report,
    load_baseline,
    load_budget,
    snapshot_from_tools,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "mcp_catalog_budget.py"


class _FakeTool:
    def __init__(self, name: str, description: str, schema: dict):
        self.name = name
        self.description = description
        self.inputSchema = schema


def _fake_catalog(sizes: dict[str, int], instructions: str = "xy") -> dict:
    tools = [
        _FakeTool(name, "d" * size, {"type": "object"}) for name, size in sizes.items()
    ]
    return snapshot_from_tools(tools, instructions=instructions)


async def test_published_catalog_is_within_its_committed_budget():
    """The state the repository must be in: green, measured from tools/list."""
    result = check_budget(await catalog_snapshot(), load_budget(), load_baseline())
    assert result["ok"], format_report(result)


async def test_budget_file_covers_every_budgeted_metric():
    budgets = load_budget()
    assert set(budgets) <= set(BUDGET_KEYS)
    assert set(budgets) == set(BUDGET_KEYS)
    baseline = load_baseline()
    snapshot = await catalog_snapshot()
    assert set(baseline) == {entry["name"] for entry in snapshot["tools_list"]}


async def test_model_visible_size_matches_measured_client_behaviour():
    """AC-2 (#815): count the instruction once, keep the worst case visible.

    Epic #776 assumed every client repeats the server instruction per tool and
    budgeted 158241 characters on that basis. One client was then actually
    looked at — Claude Code shows it once — so the budgeted number follows the
    measurement, and the assumed one stays in the snapshot under its own name
    instead of being deleted or quietly kept as the headline.
    """
    snapshot = _fake_catalog({"a": 10, "b": 20}, instructions="i" * 100)
    catalog = snapshot["description_chars"] + snapshot["schema_chars"]

    assert snapshot["catalog_chars"] == catalog
    assert snapshot["model_visible_chars"] == catalog + 100
    assert snapshot["duplicated_instruction_chars"] == 200
    assert snapshot["model_visible_chars_if_instruction_per_tool"] == catalog + 200


async def test_the_hub_itself_never_repeats_the_instruction_into_descriptions():
    """AC-1 (#815), server side: the repetition is not something the hub does.

    Whether a client repeats the instruction when composing context is the
    client's business and differs between them. What the hub publishes is
    checkable here, and it publishes the instruction exactly once.
    """
    from hub.mcp_server import mcp

    instructions = mcp.instructions or ""
    assert instructions, "the server publishes an instruction block"
    head = instructions[:80]
    tools = await mcp.list_tools()
    offenders = [t.name for t in tools if head in (t.description or "")]
    assert offenders == []


async def test_growth_over_budget_fails_and_names_the_tools():
    before = _fake_catalog({"hub_a": 100, "hub_b": 100})
    budgets = {
        "tools": before["tools"],
        "description_chars": before["description_chars"],
        "schema_chars": before["schema_chars"],
        "model_visible_chars": before["model_visible_chars"],
        "max_tool_chars": before["max_tool_chars"],
    }
    baseline = {entry["name"]: entry["total_chars"] for entry in before["tools_list"]}

    after = _fake_catalog({"hub_a": 100, "hub_b": 400, "hub_new": 50})
    result = check_budget(after, budgets, baseline)

    assert not result["ok"]
    metrics = {breach["metric"] for breach in result["breaches"]}
    assert {"tools", "description_chars", "model_visible_chars"} <= metrics

    diff = {row["tool"]: row for row in result["tool_diff"]}
    assert diff["hub_b"]["delta"] == 300
    assert diff["hub_new"]["before"] is None
    assert "hub_a" not in diff  # unchanged tools stay out of the noise

    report = format_report(result)
    assert "hub_b" in report and "hub_new" in report
    assert "OVER BUDGET" in report


async def test_removing_a_tool_is_reported_but_is_not_a_breach():
    before = _fake_catalog({"hub_a": 100, "hub_b": 100})
    budgets = {key: before[key] for key in BUDGET_KEYS}
    baseline = {e["name"]: e["total_chars"] for e in before["tools_list"]}

    result = check_budget(_fake_catalog({"hub_a": 100}), budgets, baseline)

    assert result["ok"]
    assert result["tool_diff"][0]["tool"] == "hub_b"
    assert result["tool_diff"][0]["after"] is None


async def test_typo_in_a_budget_key_is_a_failure_not_a_silent_no_op():
    snapshot = _fake_catalog({"hub_a": 10})
    result = check_budget(snapshot, {"descriptions_chars": 1}, {})
    assert not result["ok"]
    assert result["unknown_budget_keys"] == ["descriptions_chars"]
    assert "never applied" in format_report(result)


@pytest.mark.parametrize("flag", ["--json", ""])
def test_ci_script_exits_zero_on_the_committed_budget(flag: str):
    args = [sys.executable, str(SCRIPT)] + ([flag] if flag else [])
    proc = subprocess.run(args, capture_output=True, text=True, cwd=REPO_ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    if flag == "--json":
        assert json.loads(proc.stdout)["ok"] is True


def test_ci_script_exits_nonzero_when_the_catalog_is_over_budget(tmp_path: Path):
    """A red check, with the diff, is the whole point of the budget."""
    budget_file = tmp_path / "budget.json"
    original = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
    shrunk = dict(original)
    shrunk["budgets"] = {key: 1 for key in original["budgets"]}
    budget_file.write_text(json.dumps(shrunk), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--budget-file", str(budget_file)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert proc.returncode == 1
    assert "OVER BUDGET tools" in proc.stdout
    assert "raise the budget" in proc.stdout


def test_ci_script_reports_a_missing_budget_instead_of_passing(tmp_path: Path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--budget-file", str(tmp_path / "none.json")],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 2
    assert "No budget found" in proc.stderr
