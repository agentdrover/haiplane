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
    assert "hub_decide_task" in payload["next_action"]


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
    secret = "sk-live-do-not-echo"
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
    assert secret not in text
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
