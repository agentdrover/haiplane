"""Tests for mutation response envelope (#171)."""

from __future__ import annotations

import json

from mcp.types import CallToolResult, TextContent

from hub.mcp_envelope import (
    UNKNOWN_ARGUMENTS_KEY,
    attach_unknown_arguments,
    build_mutation_envelope,
    compute_awaiting,
    discarded_argument_names,
    enrich_error_payload,
)


def test_compute_awaiting_needs_decision() -> None:
    assert compute_awaiting("needs_decision") == "human_decision"


def test_build_mutation_envelope_completed_transition() -> None:
    env = build_mutation_envelope(
        {"status": "completed"},
        transition_from="pending_report",
        transition_to="completed",
    )
    assert env["status"] == "completed"
    assert env["awaiting"] == "none"
    assert env["actor_hint"] == "none"
    assert env["transition"] == {"from": "pending_report", "to": "completed"}


def test_enrich_error_payload_permission_without_status() -> None:
    payload = enrich_error_payload(
        {
            "reason": "permission_denied",
            "message": "missing permission: tasks.archive",
            "hint": "Use human token.",
            "required_role": "human",
            "suggested_tool": "hub_withdraw_own_draft",
        }
    )
    assert payload["status"] == "?"
    assert payload["awaiting"] == "none"
    assert payload["actor_hint"] == "human"
    assert "next_action" in payload
    assert payload["transition"] is None


def test_enrich_error_payload_human_decision() -> None:
    payload = enrich_error_payload(
        {
            "reason": "human_decision_required",
            "hint": "Task awaits hub_decide_task or human Decision Gate.",
            "required_status": "needs_decision",
            "current_status": "needs_decision",
        }
    )
    assert payload["awaiting"] == "human_decision"
    assert payload["actor_hint"] == "human"
    assert "/decide" in payload["next_action"]


def test_discarded_argument_names_are_sorted_and_exclude_declared() -> None:
    names = discarded_argument_names(
        {"task_id": 1, "descriptoin": "x", "limit": 3, "title": "ok"},
        {"task_id", "title"},
    )
    assert names == ["descriptoin", "limit"]


def test_discarded_argument_names_empty_when_all_declared() -> None:
    assert (
        discarded_argument_names({"task_id": 1, "title": "ok"}, {"task_id", "title"})
        == []
    )
    assert discarded_argument_names({}, {"task_id"}) == []
    assert discarded_argument_names(None, {"task_id"}) == []


def test_attach_unknown_arguments_names_json_text_and_skips_values() -> None:
    result = CallToolResult(
        content=[
            TextContent(
                type="text",
                text='{"message": "Nothing to refine", "no_op": true}',
            )
        ],
        structuredContent={"no_op": True, "fields_set": []},
    )
    attached = attach_unknown_arguments(result, ["descriptoin"])
    assert isinstance(attached, CallToolResult)
    text = attached.content[0].text
    assert "descriptoin" in text
    payload = json.loads(text)
    assert payload[UNKNOWN_ARGUMENTS_KEY] == ["descriptoin"]
    assert attached.structuredContent[UNKNOWN_ARGUMENTS_KEY] == ["descriptoin"]
    assert attached.structuredContent["no_op"] is True


def test_attach_unknown_arguments_is_a_no_op_when_nothing_was_discarded() -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text='{"message": "ok"}')],
        structuredContent={"schema_version": "1"},
    )
    attached = attach_unknown_arguments(result, [])
    assert attached is result


def test_attach_unknown_arguments_marks_echo_json_blocks() -> None:
    blocks = [
        TextContent(
            type="text", text='{"message": "Task #9 has no acceptance criteria."}'
        )
    ]
    attached = attach_unknown_arguments(blocks, ["limit"])
    text = attached[0].text
    payload = json.loads(text)
    assert payload[UNKNOWN_ARGUMENTS_KEY] == ["limit"]
    assert "message" in payload


# ---------------------------------------------------------------------------
# #1624: what the hub tells an agent never names a tool it cannot call
# ---------------------------------------------------------------------------

_NOT_FOR_AGENTS = (
    "hub_approve_task",
    "hub_reject_task",
    "hub_decide_task",
    "hub_force_complete_task",
    "hub_answer_question",
    "hub_start_task",
    "hub_approve_proposal",
    "hub_reject_proposal",
    "hub_submit_steward_judgement",
)

# Where a hidden tool's name MAY appear in a string literal of hub/, by file and
# why. Everything else is an agent-facing text and must name the human route.
_ALLOWED_LITERALS = {
    # the registry itself: names are the keys of what is hidden and where the
    # human acts, and the machine-readable transition table (REST, not a prompt)
    "hub/workflow_reference.py": "registry of hidden tools and the transition table",
    # detects these names in USER skill text (the user's words, not the hub's)
    "hub/skill_publish.py": "matches names in skills written by users",
}


def _agent_facing_samples() -> dict[str, str]:
    """The texts the hub's own code produces for an agent, from the real generators."""
    import asyncio

    from hub import actionable_errors as ae
    from hub.mcp_envelope import compute_next_action
    from hub.mcp_server import _human_only_refusal, mcp
    from hub.services import steward_advisor, steward_shadow
    from hub.workflow_reference import (
        AGENT_HIDDEN_TOOLS,
        build_mcp_instructions,
        lifecycle_map_lines,
    )

    out: dict[str, str] = {}
    out["instructions"] = build_mcp_instructions()
    out["lifecycle_map_lines"] = "\n".join(lifecycle_map_lines())

    tools = asyncio.run(mcp.list_tools_for("agent"))
    out["catalog"] = json.dumps(
        [{"n": t.name, "d": t.description, "s": t.inputSchema} for t in tools],
        ensure_ascii=False,
    )

    reasons = (
        None,
        "human_decision_required",
        "pair_start_required",
        "awaiting_ci_conveyor",
        "task_already_terminal",
        "invalid_status_for_done",
        "permission_denied",
        "human_only_gate",
        "forbidden",
    )
    statuses = (
        "draft",
        "open",
        "claimed",
        "running",
        "needs_decision",
        "needs_info",
        "pending_report",
        "review",
        "ci_check",
        "completed",
        "failed",
        "rejected",
        "?",
    )
    lines = []
    for status in statuses:
        for awaiting in ("none", "human_decision", "ci", "review"):
            for reason in reasons:
                lines.append(compute_next_action(status, awaiting, reason=reason))  # type: ignore[arg-type]
    out["envelope.next_action"] = "\n".join(lines)

    details: list[dict] = [
        ae.human_only_gate_detail(),
        ae.steward_verdict_required_detail(),
        ae.steward_closed_vocabulary_detail("f", "x", ["a"]),
        ae.steward_escalate_reason_required_detail(["a"]),
        ae.steward_unknown_finding_uid_detail("u"),
        ae.steward_judgement_exists_detail(1, 1, "verdict"),
        ae.steward_advisor_channel_detail("advisor", "steward"),
        ae.steward_advisor_not_ordered_detail(1, 1),
        ae.steward_gate_forbidden_detail("POST", "/x"),
        ae.watcher_gate_forbidden_detail("POST", "/x"),
        ae.chat_pair_task_not_open_detail(task_id=1, status="open"),
        ae.steward_run_required_detail(1, 1),
    ]
    for permission in ("tasks.human_gate", "tasks.decision", "tasks.delete"):
        details.append(ae.permission_denied_detail(permission))
    for reason in reasons[1:6]:
        for status in ("needs_decision", "running", "ci_check", "open"):
            details.append(
                ae.done_report_error_detail(
                    {"id": 1, "status": status},
                    reason=reason or "",
                    hint="h",
                    required_status="running",
                )
            )
    out["actionable_errors"] = json.dumps(details, ensure_ascii=False, default=str)

    for name in sorted(AGENT_HIDDEN_TOOLS):
        out[f"refusal.{name}"] = _human_only_refusal(name, {"task_id": 5})

    out["steward.prompt"] = steward_shadow._prompt(5, 1, "https://h", "DELIVERY")
    out["steward.advisor_prompt"] = steward_advisor._prompt(
        5, 1, "https://h", "DELIVERY"
    )
    return out


def _literal_hits() -> list[str]:
    """Hidden-tool names inside string literals of hub/, minus the explicit allowlist."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    hits: list[str] = []
    for path in sorted((root / "hub").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in _ALLOWED_LITERALS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for name in _NOT_FOR_AGENTS:
                    if name in node.value:
                        hits.append(f"{rel}:{node.lineno} names {name}")
    return hits


def test_agent_facing_texts_name_no_hidden_tools() -> None:
    """AC-4 (#1624): current texts only; the human route replaces the tool name."""
    samples = _agent_facing_samples()
    leaks = [
        f"{where}: {name}"
        for where, text in samples.items()
        for name in _NOT_FOR_AGENTS
        if name in text
    ]
    assert not leaks, leaks
    # the refusal names the way a human takes it, with the task id filled in
    assert "/api/tasks/5/approve" in samples["refusal.hub_approve_task"]
    # the human path is present where the tool name used to be
    assert "/decide" in samples["lifecycle_map_lines"]
    assert "/steward-judgement" in samples["steward.prompt"]
    assert "/steward-judgement" in samples["steward.advisor_prompt"]
    # every other literal of hub/ is covered by the source scan
    assert _literal_hits() == []
