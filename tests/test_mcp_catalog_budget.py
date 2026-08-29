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
    WORKING_FREEZE,
    working_headroom as catalog_working_headroom,
    BUDGET_KEYS,
    BUDGET_PATH,
    catalog_snapshot,
    check_budget,
    format_report,
    load_baseline,
    load_budget,
    load_measured,
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
    published = {entry["name"] for entry in snapshot["tools_list"]}
    # #829 redefined the baseline as the last DELIBERATE freeze rather than a
    # snapshot of every commit, and ordinary delivery is not supposed to touch
    # the file at all. Equality contradicted that by construction: adding a
    # tool turned this red and forced an edit to the very file #829 told
    # everyone to leave alone (found while delivering #487).
    #
    # Containment keeps what the check was actually for — a tool that vanishes
    # or gets renamed still breaks it, and that IS worth a deliberate edit —
    # while a newly added tool spends headroom instead of demanding a freeze.
    missing = set(baseline) - published
    assert not missing, (
        f"tools in the frozen baseline are gone from tools/list: {sorted(missing)}"
    )


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


# --- #988: the trim is measured, and the ceilings only ever go down ---------
#
# Pinned literals rather than a comparison with another branch: a test running
# on a branch cannot measure `develop`, and "beat whatever develop says" is not
# something a test can assert. These are the numbers measured on origin/develop
# on 2026-08-28, before the trim.
PRE_TRIM_CEILINGS = {
    "tools": 75,
    "description_chars": 40145,
    "schema_chars": 31390,
    "model_visible_chars": 73293,
    "max_tool_chars": 7120,
}
# Re-frozen 2026-08-28 (#1031) after the second trim: live was 36332 with only
# 163 characters left under the previous freeze, and one day had just spent 380
# of them (#1007 +281, #1013 +99). A freeze is a WORKING HEADROOM above the
# measured catalog — so a field added to a contract has room to land, and a
# second one is paid for by a trim rather than by raising the number. A freeze
# may only move down: the pre-trim values were 36495 (2026-08-21, live 38130)
# and 6472.
#
# #1071: the headroom is DERIVED, never retyped. It used to be prose beside the
# constant — "live 35383 + 1000 working headroom" — and the prose was wrong in
# both halves: the same commit reported live as 36332 (a 949 disagreement three
# lines apart), and the real distance from the recorded measurement is 324, not
# 1000. Nothing checked either claim, so nothing caught them.
#
# The cost landed on 2026-08-29 (#911). The FIRST contract field to arrive
# found 49 characters and had to be paid for by rewriting a docstring — while
# the policy above says the first one lands free and only the second one pays.
# An author who reads that policy plans work that will not fit.
#
# So the freeze stays a pinned literal (a test cannot measure another branch),
# but the headroom is arithmetic over recorded facts and is reported by the
# refusal itself. There is no second number left to disagree with the first.
# The values themselves now live in hub/mcp_catalog.py, next to the loader the
# report uses, so the number that binds is the number ``--update`` publishes
# into docs/agent-context — the file an author reads before planning. A copy
# here would be a second place to edit, and the two would part ways on the
# first re-freeze.
FROZEN_DESCRIPTION_CHARS = WORKING_FREEZE["description_chars"]
FROZEN_MAX_TOOL_CHARS = WORKING_FREEZE["max_tool_chars"]
# A freeze may only move DOWN. These pin that, the way PRE_TRIM_CEILINGS pins
# the ceilings: raising a freeze to fit new text is the act the whole guard
# exists to prevent, and it must not be possible to do it quietly (#1071).
WORKING_FREEZE_FLOOR = {"description_chars": 36383, "max_tool_chars": 6404}
PRE_TRIM_INSTRUCTION_PER_TOOL = 179598


def test_no_ceiling_rises() -> None:
    """AC-2: a shrink re-freezes downwards; raising a ceiling is a different act.

    The budget file exists so growth cannot happen quietly. A trim that leaves
    `measured` stale is the mirror image of that failure: the headroom grows
    instead, and the check stops constraining anything. So the file may be
    re-frozen — but never upwards in this task.
    """
    budgets = load_budget()
    for metric, ceiling in PRE_TRIM_CEILINGS.items():
        assert budgets[metric] <= ceiling, (
            f"{metric}: ceiling rose from {ceiling} to {budgets[metric]}"
        )


def test_the_declared_working_headroom_is_true() -> None:
    """AC-1: the promise is arithmetic over recorded facts, not prose.

    The policy above says a freeze sits a working headroom above the measured
    catalog so a contract field has room to land. That is only a promise if
    something checks it — #1031 stated it and set a freeze 324 above the
    recorded measurement while the comment claimed 1000, and no test noticed.

    This does not say WHAT the headroom should be: choosing that is moving a
    ceiling, which is a deliberate act and not this task's. It says the number
    is positive and knowable, so the next author reads a fact instead of a
    sentence somebody typed.
    """
    rooms = catalog_working_headroom()
    for metric, freeze in WORKING_FREEZE.items():
        room = rooms[metric]
        assert room > 0, (
            f"{metric}: the freeze {freeze} sits BELOW the recorded measurement "
            f"{load_measured()[metric]} — a freeze under the catalog it freezes "
            "refuses everything, including no change at all"
        )


async def test_description_chars_stay_under_freeze() -> None:
    """AC-3: the docstring trim is real, not a rounding error.

    Named without a number since #1031: the freeze moves down after every
    trim, and a name carrying the old figure would be wrong the moment it did.
    """
    snapshot = await catalog_snapshot()
    live = snapshot["description_chars"]
    # #1071 (AC-4): the refusal says how much room is left and where the limit
    # is declared. The previous message named neither, so an author who hit it
    # went looking for the number by grepping the test suite — measured on
    # 2026-08-29, that search cost several rewrites of a docstring.
    assert live < FROZEN_DESCRIPTION_CHARS, (
        f"catalog descriptions are {live}, over the freeze "
        f"{FROZEN_DESCRIPTION_CHARS} by {live - FROZEN_DESCRIPTION_CHARS + 1}. "
        "The freeze is FROZEN_DESCRIPTION_CHARS in "
        "tests/test_mcp_catalog_budget.py and is the limit that binds — the "
        "ceiling in docs/agent-context/mcp-catalog-budget.json is looser and "
        "will not stop you. Pay for the new text with a trim; raising the "
        "freeze is a separate, deliberate act."
    )


async def test_max_tool_chars_back_under_freeze() -> None:
    """AC-9: the fattest tool stops eating the last of the headroom.

    Before the trim the biggest tool was 7065 against a 7120 ceiling — 55
    characters left, which the report already flagged as "decide now".
    """
    snapshot = await catalog_snapshot()
    assert snapshot["max_tool_chars"] <= FROZEN_MAX_TOOL_CHARS
    budgets = load_budget()
    spent = snapshot["max_tool_chars"] / budgets["max_tool_chars"]
    assert spent < 0.95, f"still {spent:.1%} of the ceiling"


async def test_instruction_win_measured_on_its_own_metric() -> None:
    """AC-10: the instruction is not part of description_chars, so it cannot be
    claimed on that number.

    Counted once per session for the measured clients (#815), and once per tool
    for a client that copies server instructions into each tool. The second
    number is the one a shorter instruction moves.
    """
    snapshot = await catalog_snapshot()
    assert snapshot["instruction_chars"] not in (0, None)
    assert (
        snapshot["model_visible_chars_if_instruction_per_tool"]
        < PRE_TRIM_INSTRUCTION_PER_TOOL
    )
    # The instruction is not counted inside the descriptions it points at.
    assert snapshot["description_chars"] < snapshot["catalog_chars"]


async def test_no_empty_tool_descriptions() -> None:
    """AC-4: a trim may shorten a description; it may not delete one.

    Every published tool still describes itself, and the ones that take
    arguments still name them — the guard against trimming a tool down to its
    title.
    """
    snapshot = await catalog_snapshot()
    empty = [t["name"] for t in snapshot["tools_list"] if t["description_chars"] < 40]
    assert empty == [], f"tools shipped without a usable description: {empty}"

    from hub.mcp_server import (
        hub_list_tasks,
        hub_pair_start,
        hub_refine_task,
        hub_submit_for_review,
        hub_submit_machine_review,
        hub_submit_review,
    )

    for func, args in (
        (hub_refine_task, ("task_id", "scope_in", "acceptance_criteria", "risks")),
        (hub_pair_start, ("task_id", "assigned_agent", "session_id", "git_mode")),
        (hub_submit_for_review, ("task_id", "branch", "model", "accept_areas")),
        (hub_submit_review, ("task_id", "verdict", "findings")),
        (hub_submit_machine_review, ("task_id", "incomplete", "unresolved")),
        (hub_list_tasks, ("status", "claimed_by", "limit")),
    ):
        doc = func.__doc__ or ""
        assert "Args:" in doc, func.__name__
        for arg in args:
            assert f"{arg}:" in doc, f"{func.__name__} stopped describing {arg}"


def test_no_working_freeze_rises() -> None:
    """AC-2: a freeze may only move DOWN.

    The freeze is what stops a contract field from landing on borrowed room, so
    raising it to fit new text is exactly the act it exists to prevent. Pinned
    the way PRE_TRIM_CEILINGS pins the ceilings: a literal a change has to walk
    past on purpose, not a value that follows whatever the catalog became.
    """
    for metric, floor in WORKING_FREEZE_FLOOR.items():
        assert WORKING_FREEZE[metric] <= floor, (
            f"{metric}: working freeze rose from {floor} to "
            f"{WORKING_FREEZE[metric]} — pay for new text with a trim, or move "
            "this floor deliberately and say why"
        )


def test_the_binding_limit_is_readable_where_work_is_planned() -> None:
    """AC-3: the number that stops you is in the file you read before starting.

    docs/agent-context is the directory agents read as context. It advertised
    only the percentage ceiling — 3374 characters of room on 2026-08-29 — while
    the freeze left 92. An author planning a contract field read the number
    that would not stop them and planned work that did not fit (#911).
    """
    doc = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
    published = doc.get("working_freeze")
    assert published == WORKING_FREEZE, (
        "the budget file must publish the freeze that binds: "
        f"file says {published}, code says {WORKING_FREEZE}. "
        "Run scripts/mcp_catalog_budget.py --update after moving a freeze."
    )
    assert doc.get("working_headroom_note"), (
        "the file must say that the freeze, not the ceiling, is what refuses"
    )


def test_measured_records_the_freeze_not_the_present() -> None:
    """AC-5: measured lagging the live catalog is its MEANING, not a defect.

    It records what the catalog was when the ceiling was set, so "how much of
    the slack has been spent" is arithmetic over recorded facts. Requiring it
    to equal the live value would force --update on every change and bring back
    the churn #829 removed — three rounds of resubmission on #815, none of them
    about the work. Written as a test because I nearly required the opposite.
    """
    measured = load_measured()
    assert measured, "the file records what the catalog was at the last freeze"
    # No assertion that it equals the live catalog: that is the point.
    assert set(measured) >= set(WORKING_FREEZE), (
        "every freeze needs a recorded measurement to be a distance from"
    )
