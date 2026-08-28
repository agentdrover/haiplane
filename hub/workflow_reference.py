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
            "Gates: DoR at draft (hub_approve_task, human); "
            "CI at ci_check (poller); "
            "Review — Universal Review Gate: no completed without a current "
            "APPROVED review (hub_submit_for_review → hub_get_review_brief → "
            "hub_submit_review; auto_review=false is the explicit opt-out); "
            "Decision at needs_decision (hub_decide_task, human)."
        ),
        f"Agent completion: {AGENT_COMPLETION_TOOL} only (hub_task_update kind=done = deprecated alias).",
        f"Human-only: {', '.join(HUMAN_ONLY_TOOLS)}.",
        "Transitions:",
    ]
    for transition in LIFECYCLE_TRANSITIONS:
        gate = transition.get("gate")
        gate_suffix = f" [{gate}]" if gate else ""
        lines.append(
            f"  {transition['from']}→{transition['to']}: "
            f"{transition['tool']} ({transition['actor']}){gate_suffix}"
        )
    return lines


def mcp_workflow_instruction_section() -> str:
    """Enriched workflow section for MCP server instructions (<4KB total with base)."""
    ref = workflow_reference_dict()
    hierarchy = ref["hierarchy"]
    hierarchy_text = "; ".join(
        f"{edge['child']} parent={edge['parent'] or 'none'}" for edge in hierarchy
    )
    gate_bits = [
        f"DoR→{ref['gates']['dor']['tool']} ({ref['gates']['dor']['actor']})",
        f"CI→ci_check ({ref['gates']['ci']['actor']})",
        "Review→hub_submit_review (agent; no completed without current APPROVED)",
        f"Decision→{ref['gates']['decision']['tool']} ({ref['gates']['decision']['actor']})",
    ]
    transition_bits = [
        f"{t['from']}→{t['to']} via {t['tool']} ({t['actor']})"
        for t in LIFECYCLE_TRANSITIONS[:10]
    ]
    return (
        " Hierarchy (HIERARCHY_RULES): "
        + hierarchy_text
        + ". Gates: "
        + "; ".join(gate_bits)
        + ". Key transitions: "
        + "; ".join(transition_bits)
        + ". Full transition list is in hub_my_context (mode=full) Workflow reference section."
    )


def build_mcp_instructions() -> str:
    """Full MCP server instructions including workflow discoverability."""
    return (
        "MCP server for Haiplane Hub — project state, tasks, proposals, decisions. "
        "Agent canonical task completion: hub_report_done only (not hub_decide_task, "
        "hub_force_complete_task, or hub_approve_task). hub_task_update kind=done is "
        "a deprecated alias of hub_report_done with the same response contract. "
        "Human-only tools: hub_decide_task, hub_approve_task, hub_reject_task, "
        "hub_force_complete_task, hub_answer_question (human token), hub_start_task. "
        "Lifecycle mutation tools return JSON with message plus envelope fields: status, "
        "awaiting (none|human_decision|ci|review), transition {from,to}|null, next_action, "
        "actor_hint (agent|human|ci|none). Every response also includes instance "
        "(prod|local) and base_url echoing HAIPLANE_HUB_URL. "
        "Structured errors use "
        "the same envelope plus reason and hint." + mcp_workflow_instruction_section()
    )
