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


def test_report_names_remaining_headroom():
    """AC-3 (#829): slack nobody watches is how a check stops checking.

    The report says how much of the declared headroom has been spent, not how
    full the ceiling is: with a 10% ceiling a healthy catalog sits at ~91% of
    it from the first run, and a warning that fires on day one is a warning
    everyone learns to skip.
    """
    snapshot = _fake_catalog({"hub_a": 100, "hub_b": 100})
    measured = {key: snapshot[key] for key in BUDGET_KEYS}
    ceilings = {key: int(value * 1.5) for key, value in measured.items()}

    fresh = check_budget(snapshot, ceilings, {}, measured)
    rows = {row["metric"]: row for row in fresh["headroom"]}
    assert fresh["ok"]
    assert all(row["used_pct"] == 0.0 for row in rows.values())
    assert rows["tools"]["remaining"] == ceilings["tools"] - measured["tools"]
    report = format_report(fresh)
    assert "of headroom spent" in report
    assert "⚠" not in report

    # The slack spent down to the ceiling: still green, and the report says so
    # out loud instead of waiting for the next change to turn red.
    grown = _fake_catalog({"hub_a": 100, "hub_b": 100, "hub_c": 100})
    eaten = check_budget(grown, ceilings, {}, measured)
    assert eaten["ok"]
    tools_row = {row["metric"]: row for row in eaten["headroom"]}["tools"]
    assert tools_row["used_pct"] == 100.0
    assert "⚠" in format_report(eaten)
    assert "Headroom nearly gone" in format_report(eaten)


def test_headroom_without_a_recorded_baseline_says_so_instead_of_guessing():
    """An older budget file has no `measured`; the report must not invent it."""
    snapshot = _fake_catalog({"hub_a": 100})
    ceilings = {key: snapshot[key] * 2 for key in BUDGET_KEYS}

    result = check_budget(snapshot, ceilings, {}, None)

    assert all(row["used_pct"] is None for row in result["headroom"])
    assert "no baseline" in format_report(result)


def test_two_tool_adding_branches_merge_without_conflict(tmp_path: Path):
    """AC-1 (#829): the budget file stops serializing parallel branches.

    Reproduces the incident from #815 in miniature: two branches off one
    commit, each adding a tool. Under an exact freeze both had to rewrite the
    ceilings and the merge conflicted every time. Under a ceiling with
    headroom neither branch touches the file, so there is nothing to conflict
    over — and the merged catalog still passes the check without a manual
    edit.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        ).stdout

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")

    base_catalog = _fake_catalog({"hub_a": 100, "hub_b": 100})
    measured = {key: base_catalog[key] for key in BUDGET_KEYS}
    budget_file = repo / "budget.json"
    budget_file.write_text(
        json.dumps(
            {
                "measured_at": "2026-08-21",
                "headroom_pct": 100.0,
                "measured": measured,
                "budgets": {key: value * 2 for key, value in measured.items()},
                "baseline_tools": {
                    entry["name"]: entry["total_chars"]
                    for entry in base_catalog["tools_list"]
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (repo / "tools.txt").write_text("hub_a\nhub_b\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "base")

    # Each branch delivers a tool. Neither has any reason to touch the budget:
    # the new tool fits under the ceiling.
    git("checkout", "-q", "-b", "branch-a")
    (repo / "tools.txt").write_text("hub_a\nhub_b\nhub_new_a\n", encoding="utf-8")
    git("commit", "-qam", "add hub_new_a")

    git("checkout", "-q", "main")
    git("checkout", "-q", "-b", "branch-b")
    (repo / "tools.txt").write_text("hub_a\nhub_b\nhub_new_b\n", encoding="utf-8")
    git("commit", "-qam", "add hub_new_b")

    merge = subprocess.run(
        ["git", "merge", "branch-a", "-m", "merge"],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert merge.returncode == 0 or "budget.json" not in merge.stdout + merge.stderr
    assert "budget.json" not in git("status", "--porcelain")

    # And the merged surface — both new tools — is still inside the ceilings,
    # so nobody has to edit the file after the merge either.
    merged = _fake_catalog(
        {"hub_a": 100, "hub_b": 100, "hub_new_a": 20, "hub_new_b": 20}
    )
    doc = json.loads(budget_file.read_text(encoding="utf-8"))
    result = check_budget(
        merged, doc["budgets"], doc["baseline_tools"], doc["measured"]
    )
    assert result["ok"], format_report(result)


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
