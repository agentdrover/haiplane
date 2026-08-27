"""Tests for actionable error payloads (#172)."""

from __future__ import annotations

from hub.actionable_errors import (
    hierarchy_error_detail,
    normalize_api_error_detail,
    permission_denied_detail,
    session_owned_by_other_detail,
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


def test_session_owned_by_other_does_not_name_the_holder() -> None:
    payload = session_owned_by_other_detail(session_id="s-owned")
    assert payload["reason"] == "session_owned_by_other"
    assert payload["session_id"] == "s-owned"
    assert payload["suggested_tool"] == "hub_session_register"
    assert payload["actor_hint"] == "agent"
    assert "principal" in payload["message"]
    assert "s-owned" in payload["message"]


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


# ---- Every refusal declares who acts next (#548) --------------------------
#
# The old shape was a tuple of reasons forced to "human"; anything absent fell
# through to "agent", so the envelope told the caller we had just refused that
# it was still the responsible actor. That silent default shipped the same
# defect three times: agent_create_forbidden (#360), then self_review_forbidden
# and withdraw_agent_only (#548). Each reason gets its own test on purpose — a
# single loop over a list would let the next added reason ride along untested.


def test_withdraw_agent_only_points_at_the_human() -> None:
    # Raised by require_agent_caller, so the REFUSED caller is a human/admin
    # and hub_archive_task is theirs too. Naming the agent sent the human
    # looking for a bot to do something only they can do.
    from hub.actionable_errors import withdraw_agent_only_detail

    payload = withdraw_agent_only_detail()
    assert payload["actor_hint"] == "human"
    assert payload["suggested_tool"] == "hub_archive_task"


def test_self_review_keeps_agent_actor_but_forbids_the_same_principal() -> None:
    # Another agent principal may pass this verdict (#432), so "human" would be
    # a fresh lie in place of the old one. actor_hint cannot express "a
    # different principal of the same kind", so the constraint is its own field.
    from hub.actionable_errors import self_review_forbidden_detail

    payload = self_review_forbidden_detail("impl-bot")
    assert payload["actor_hint"] == "agent"
    assert payload["same_principal_forbidden"] is True
    assert "independent reviewer" in payload["next_action"].lower()


def test_done_report_human_decision_points_at_the_human() -> None:
    # needs_decision is resolved by hub_decide_task, which is human-only.
    from hub.actionable_errors import done_report_error_detail

    payload = done_report_error_detail(
        {"id": 1, "status": "needs_decision"},
        reason="human_decision_required",
        hint="h",
        required_status="running",
    )
    assert payload["actor_hint"] == "human"


def test_done_report_awaiting_ci_points_at_ci() -> None:
    # Nobody acts here — CI does. Calling this the agent's move invites a retry
    # loop against a conveyor that has not finished.
    from hub.actionable_errors import done_report_error_detail

    payload = done_report_error_detail(
        {"id": 1, "status": "ci_check"},
        reason="awaiting_ci_conveyor",
        hint="h",
        required_status="running",
    )
    assert payload["actor_hint"] == "ci"


def test_bad_input_reasons_stay_with_the_agent() -> None:
    # The inverse error matters too: these really are the caller's to fix, and
    # forcing them to "human" would park a self-serviceable problem on a person.
    from hub.actionable_errors import (
        hierarchy_error_detail,
        normalize_api_error_detail,
        pair_start_claim_mismatch_detail,
    )

    assert (
        pair_start_claim_mismatch_detail(task_id=1, holder="a", caller="b")[
            "actor_hint"
        ]
        == "agent"
    )
    assert (
        hierarchy_error_detail("requires parent of type epic, got task")["actor_hint"]
        == "agent"
    )
    assert normalize_api_error_detail("boom", status_code=422)["actor_hint"] == "agent"


def test_refusal_reports_no_transition_but_keeps_a_real_await() -> None:
    # The call changed nothing, so transition is always None. ``awaiting`` is a
    # different question: it describes the TASK's gate, not this call. The first
    # version of this change zeroed it for every refusal and an existing test
    # caught it — human_decision_required really does await a human.
    from hub.actionable_errors import (
        done_report_error_detail,
        normalize_api_error_detail,
        self_review_forbidden_detail,
    )

    for payload in (
        self_review_forbidden_detail("bot"),
        normalize_api_error_detail("boom", status_code=422),
        normalize_api_error_detail("nope", status_code=403),
    ):
        assert payload["transition"] is None
        # No status context on these, so nothing can be computed from it.
        assert payload["awaiting"] == "none"

    gated = done_report_error_detail(
        {"id": 1, "status": "needs_decision"},
        reason="human_decision_required",
        hint="h",
        required_status="running",
    )
    assert gated["transition"] is None
    assert gated["awaiting"] == "human_decision"


def test_undeclared_reason_warns_instead_of_passing_silently(caplog) -> None:
    # The point of the inversion: omission must be noisy. If this ever stops
    # warning, the next forgotten reason goes back to claiming "agent" quietly.
    import logging

    from hub.mcp_envelope import enrich_error_payload

    with caplog.at_level(logging.WARNING, logger="hub"):
        payload = enrich_error_payload({"reason": "brand_new_refusal", "hint": "h"})

    assert payload["actor_hint"]  # still answers something usable
    assert any("declares neither actor_hint" in r.message for r in caplog.records)
