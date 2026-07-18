"""Tests for actionable error payloads (#172)."""

from __future__ import annotations

from hub.actionable_errors import (
    hierarchy_error_detail,
    normalize_api_error_detail,
    permission_denied_detail,
)


def test_permission_denied_archive_suggests_withdraw() -> None:
    payload = permission_denied_detail("tasks.archive")
    assert payload["reason"] == "permission_denied"
    assert payload["required_role"] == "human"
    assert payload["suggested_tool"] == "hub_withdraw_own_draft"
    assert payload["hint"]
    assert payload["actor_hint"] == "human"
    assert payload["awaiting"] == "none"
    assert "next_action" in payload


def test_hierarchy_parent_type_mismatch() -> None:
    payload = hierarchy_error_detail(
        "task requires parent of type feature, got task",
        task_type="task",
        parent_id=42,
    )
    assert payload["reason"] == "invalid_hierarchy"
    assert "feature" in payload["hint"]
    assert payload["suggested_tool"] == "hub_create_task"
    assert payload["required_parent_type"] == "feature"


def test_normalize_missing_permission_string() -> None:
    payload = normalize_api_error_detail(
        "missing permission: tasks.delete",
        status_code=403,
    )
    assert payload["reason"] == "permission_denied"
    assert payload["suggested_tool"] == "hub_withdraw_own_draft"
