"""Workflow discoverability: hierarchy rules and lifecycle map (#175)."""

from __future__ import annotations

from typing import Any

from hub.models import HIERARCHY_RULES, TaskType

# Key status transitions with triggering tool and actor role.
# Universal Review Gate (#306): hub_report_done completes a task ONLY when
# the current submission has an APPROVED review (or auto_review=false);
# otherwise the done report is a submission and routes to review/ci_check,
# or to needs_decision at the review-cycle limit.
LIFECYCLE_TRANSITIONS: list[dict[str, str | None]] = [
    {
        "from": "draft",
        "to": "open",
        "tool": "hub_approve_task",
        "actor": "human",
        "gate": "dor",
    },
    {"from": "draft", "to": "rejected", "tool": "hub_reject_task", "actor": "human"},
    {"from": "open", "to": "claimed", "tool": "hub_claim_task", "actor": "agent"},
    {"from": "open", "to": "running", "tool": "hub_start_task", "actor": "human"},
    {"from": "open", "to": "running", "tool": "hub_pair_start", "actor": "agent"},
    {"from": "claimed", "to": "running", "tool": "hub_pair_start", "actor": "agent"},
    {"from": "claimed", "to": "open", "tool": "hub_release_task", "actor": "agent"},
    {
        "from": "claimed",
        "to": "open",
        "tool": "chat_pair_reaper",
        "actor": "ci",
    },
    {
        "from": "running",
        "to": "open",
        "tool": "chat_pair_reaper",
        "actor": "ci",
    },
    {
        "from": "running",
        "to": "review",
        "tool": "hub_submit_for_review",
        "actor": "agent",
        "gate": "review",
    },
    {
        "from": "review",
        "to": "running",
        "tool": "hub_submit_review",
        "actor": "agent",
        "gate": "review",
    },
    # #1647: задача-состояние (result_kind=state). Её APPROVED завершает задачу
    # той же записью (via=state_approved), без done-отчёта, ветки и доставки;
    # ставит его только человек и только на названное поколение.
    {
        "from": "review",
        "to": "completed",
        "tool": "hub_submit_review",
        "actor": "human",
        "gate": "review",
    },
    {
        "from": "running",
        "to": "completed",
        "tool": "hub_report_done",
        "actor": "agent",
        "gate": "review",
    },
    {
        "from": "running",
        "to": "review",
        "tool": "hub_report_done",
        "actor": "agent",
        "gate": "review",
    },
    {"from": "running", "to": "ci_check", "tool": "hub_report_done", "actor": "agent"},
    {
        "from": "running",
        "to": "needs_decision",
        "tool": "hub_report_done",
        "actor": "agent",
    },
    {
        "from": "running",
        "to": "needs_info",
        "tool": "hub_ask_question",
        "actor": "agent",
    },
    {
        "from": "needs_info",
        "to": "open",
        "tool": "hub_answer_question",
        "actor": "human",
    },
    {
        "from": "running",
        "to": "pending_report",
        "tool": "hub_report_done",
        "actor": "agent",
    },
    # #1362: only after merge_failed on a conflict with the base branch —
    # every other needs_decision cause stays with hub_decide_task.
    {
        "from": "needs_decision",
        "to": "review",
        "tool": "hub_submit_for_review",
        "actor": "agent",
        "gate": "review",
    },
    {
        "from": "pending_report",
        "to": "review",
        "tool": "hub_report_done",
        "actor": "agent",
        "gate": "review",
    },
    {
        "from": "ci_check",
        "to": "review",
        "tool": "ci_poller",
        "actor": "ci",
        "gate": "ci",
    },
    {
        "from": "needs_decision",
        "to": "completed",
        "tool": "hub_decide_task",
        "actor": "human",
        "gate": "decision",
    },
    {
        "from": "needs_decision",
        "to": "fix_requested",
        "tool": "hub_decide_task",
        "actor": "human",
        "gate": "decision",
    },
    {
        "from": "pending_report",
        "to": "completed",
        "tool": "hub_report_done",
        "actor": "agent",
        "gate": "review",
    },
]

HUMAN_ONLY_TOOLS: tuple[str, ...] = (
    "hub_approve_task",
    "hub_reject_task",
    "hub_decide_task",
    "hub_force_complete_task",
    "hub_answer_question",
    "hub_start_task",
)

# Tools an agent token never sees in tools/list and cannot call (#1624): the six
# human lifecycle gates. The REST routes stay the last defence (403); hiding is
# for the context an agent pays on every turn. hub_create_project is human-only
# too but deliberately not here — out of scope of #1624.
AGENT_HIDDEN_TOOLS: frozenset[str] = frozenset(HUMAN_ONLY_TOOLS)

# Where a human does what the hidden tool did. Agent-facing text names this
# route, never the tool: the agent cannot call the tool, a person can open the
# route (UI task card, `oc-hub`, or curl with a human token).
HUMAN_ROUTES: dict[str, str] = {
    "hub_approve_task": "POST /api/tasks/{id}/approve",
    "hub_reject_task": "POST /api/tasks/{id}/reject",
    "hub_decide_task": "POST /api/tasks/{id}/decide",
    "hub_force_complete_task": "POST /api/tasks/{id}/force-complete",
    "hub_answer_question": "POST /api/tasks/{id}/answer",
    "hub_start_task": "POST /api/tasks/{id}/start",
}


def human_route(tool: str, task_id: int | str | None = None) -> str:
    """The REST route of a human gate, with ``{id}`` filled when known."""
    route = HUMAN_ROUTES[tool]
    return route.replace("{id}", str(task_id)) if task_id is not None else route


AGENT_COMPLETION_TOOL = "hub_report_done"

LIFECYCLE_MAP_HEADER = "## Workflow reference"


def hierarchy_edges() -> list[dict[str, str | None]]:
    """Machine-readable parent→child rules derived from HIERARCHY_RULES."""
    return [
        {
            "child": child.value,
            "parent": parent.value if parent else None,
        }
        for child, parent in HIERARCHY_RULES.items()
    ]


def workflow_reference_dict() -> dict[str, Any]:
    """Compact machine-readable workflow schema for agents."""
    return {
        "hierarchy": hierarchy_edges(),
        "transitions": LIFECYCLE_TRANSITIONS,
        "gates": {
            "dor": {
                "applies_at": "draft",
                "tool": "hub_approve_task",
                "actor": "human",
            },
            "ci": {"status": "ci_check", "actor": "ci", "tool": "ci_poller"},
            "review": {
                "status": "review",
                "tool": "hub_submit_review",
                "actor": "agent",
                "rule": "no completed without current APPROVED review "
                "(auto_review=false is the explicit opt-out)",
            },
            "machine_review": {
                "status": "review",
                "tool": "hub_submit_machine_review",
                "actor": "agent",
                "rule": "when policy requires it (#382): run the "
                "multi-agent harness (hub_get_skill 'machine-review-cycle') "
                "and submit the report BEFORE the human verdict; "
                "HAIPLANE_MACHINE_REVIEW=require "
                "blocks the verdict without "
                "a current report, default 'warn' only surfaces the gap",
            },
            "decision": {
                "status": "needs_decision",
                "tool": "hub_decide_task",
                "actor": "human",
            },
        },
        "human_only_tools": list(HUMAN_ONLY_TOOLS),
        "agent_completion_tool": AGENT_COMPLETION_TOOL,
    }


def hierarchy_rules_prose() -> str:
    """Single-line summary aligned with HIERARCHY_RULES."""
    parts: list[str] = []
    for child in (TaskType.epic, TaskType.feature, TaskType.task, TaskType.subtask):
        parent = HIERARCHY_RULES[child]
        if parent is None:
            parts.append(f"{child.value} (root)")
        else:
            parts.append(f"{child.value}→parent:{parent.value}")
    return ", ".join(parts)


def lifecycle_map_lines() -> list[str]:
    """Markdown lines appended to hub_my_context digest (mode=full)."""
    lines = [
        LIFECYCLE_MAP_HEADER,
        f"Hierarchy: {hierarchy_rules_prose()}.",
        (
            "Gates: DoR at draft (human: POST /api/tasks/{id}/approve); "
            "CI at ci_check (poller); "
            "Review — Universal Review Gate: no completed without a current "
            "APPROVED review (auto_review=false is the explicit opt-out); "
            "Decision at needs_decision (human: POST /api/tasks/{id}/decide)."
        ),
        # Two actors, not one chain (#988). The gate line above used to read
        # "hub_submit_for_review → hub_get_review_brief → hub_submit_review",
        # which is a four-step recipe for the submitting agent — i.e. the
        # shape of a self-review. The brief and the verdict belong to someone
        # else, and hub_submit_review refuses a verdict from the implementer.
        (
            "Author lane: hub_submit_for_review (pair; takes branch, model, "
            "accept_areas, and hands back a wait_baseline) or hub_report_done "
            "— either is a "
            "submission while no current APPROVED review exists. Then wait "
            "(awaiting=review). After APPROVED puts the task back in running: "
            "hub_report_done → completed."
        ),
        (
            "Reviewer lane, NOT the assigned agent: hub_get_review_brief → "
            "hub_submit_review. A verdict from the task's own implementer is "
            "refused."
        ),
        (
            "State tasks (result_kind=state, #1647): no branch, PR or CI. Submit "
            "with the evidence field of hub_submit_for_review — one record per "
            "AC: ac_id, action, observed, target, observed_at. Only a human "
            "APPROVES, naming expected_generation, and that completes the task "
            "(via=state_approved); hub_report_done is refused for them."
        ),
        f"Agent completion: {AGENT_COMPLETION_TOOL} only (hub_task_update kind=done = deprecated alias).",
        "Human gates are not in your tool list: a person acts through the hub "
        "UI, oc-hub or the REST route named below; ask for it, do not retry.",
        "Transitions:",
    ]
    for transition in LIFECYCLE_TRANSITIONS:
        gate = transition.get("gate")
        gate_suffix = f" [{gate}]" if gate else ""
        tool = str(transition["tool"])
        # A human step is written as its route: the agent cannot call the tool.
        actor = "human" if tool in AGENT_HIDDEN_TOOLS else str(transition["actor"])
        via = HUMAN_ROUTES.get(tool, tool)
        lines.append(
            f"  {transition['from']}→{transition['to']}: {via} ({actor}){gate_suffix}"
        )
    return lines


def mcp_workflow_instruction_section() -> str:
    """A pointer to the lifecycle map, not a second copy of it (#988).

    The instruction used to carry the hierarchy, the gate list and ten
    from→to→tool transitions — all of which hub_my_context(mode=full) already
    prints under "Workflow reference". Every session paid for the same map
    twice, and a client that repeats server instructions per tool paid for it
    once per tool. What stays here is what a caller cannot look up without
    already knowing where to look: where the map lives, and that review has
    two actors.
    """
    return (
        " Lifecycle map — hierarchy, gates and the full transition list: "
        "hub_my_context(mode=full), section 'Workflow reference'. "
        "Review is two actors: the author submits (hub_submit_for_review or "
        "hub_report_done) and waits; a DIFFERENT agent runs "
        "hub_get_review_brief and hub_submit_review."
    )


def build_mcp_instructions() -> str:
    """Full MCP server instructions including workflow discoverability."""
    return (
        "MCP server for Haiplane Hub — project state, tasks, proposals, decisions. "
        "Agent canonical task completion: hub_report_done only. "
        "hub_task_update kind=done is "
        "a deprecated alias of hub_report_done with the same response contract. "
        "Human gates (approve, reject, decide, force-complete, answer, start) "
        "are not tools here: a person uses the UI, oc-hub or "
        "REST POST /api/tasks/{id}/<gate>. "
        "Lifecycle mutation tools return JSON with message plus envelope fields: status, "
        "awaiting (none|human_decision|ci|review), transition {from,to}|null, next_action, "
        "actor_hint (agent|human|ci|none). Every response also includes instance "
        "(prod|local) and base_url echoing HAIPLANE_HUB_URL. "
        "Structured errors use "
        "the same envelope plus reason and hint." + mcp_workflow_instruction_section()
    )
