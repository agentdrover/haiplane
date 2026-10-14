"""Classification of EVERY field of the review brief for the MCP projection (#1682).

One table, one decision per field. A field of ``ReviewBrief`` (or of a nested
decision object) that is not here fails ``tests/test_review_brief_projection.py``
until someone decides which class it belongs to, so that a new decision field
cannot silently stay out of the default answer.

Classes:
  MANDATORY  anything that can flip a verdict, or that names a failure, an
             unknown state or the independence of a reviewer. Never truncated.
  BLOCK      lists that give way to the budget, with shown/omitted/total and a
             hint to the section that pages the rest.
  TEXT       statement-like text; the projection shows the first characters,
             section=statement the whole.
  FULL_ONLY  non-decision data, reachable through section=full only. Every
             entry names why it is not decision data.

A parent path WITHOUT child entries covers its children (the whole object is
carried as it is). A parent WITH child entries is split: each child is listed.
"""

from __future__ import annotations

MANDATORY = "MANDATORY"
BLOCK = "BLOCK"
TEXT = "TEXT"
FULL_ONLY = "FULL_ONLY"

#: path -> (class, reason). The reason is required for FULL_ONLY, optional else.
FIELD_CLASS: dict[str, tuple[str, str]] = {
    "task_id": (MANDATORY, ""),
    "title": (MANDATORY, ""),
    "status": (MANDATORY, ""),
    "project": (MANDATORY, ""),
    "locator_resolution": (MANDATORY, ""),
    "ac_test_results": (MANDATORY, ""),
    "ci_run_report": (MANDATORY, ""),
    "ci_integrity": (MANDATORY, ""),
    "ci_evidence": (MANDATORY, ""),
    "prepass": (MANDATORY, ""),
    "validation": (MANDATORY, ""),
    "live_check": (MANDATORY, ""),
    "statement_freshness": (MANDATORY, ""),
    "branch": (MANDATORY, ""),
    "diff_command": (MANDATORY, ""),
    "diff_base": (MANDATORY, ""),
    "base_merge": (MANDATORY, ""),
    "evidence_coverage": (MANDATORY, ""),
    "review_cycle": (MANDATORY, ""),
    "review_circle": (MANDATORY, ""),
    "profile_downgrade": (MANDATORY, ""),
    "verdict_route": (MANDATORY, ""),
    "catalogue_rules": (MANDATORY, ""),
    "submission_generation": (MANDATORY, ""),
    "submission_sha": (MANDATORY, ""),
    "current_branch_tip": (MANDATORY, ""),
    "sha_check": (MANDATORY, ""),
    "sha_check_reason": (MANDATORY, ""),
    "review_in_flight": (MANDATORY, ""),
    "current_generation_review": (MANDATORY, ""),
    "self_review_warning": (MANDATORY, ""),
    "stacking_warning": (MANDATORY, ""),
    "path_notices": (MANDATORY, ""),
    "result_kind": (MANDATORY, ""),
    "state_review": (MANDATORY, ""),
    "latest_review.verdict": (MANDATORY, ""),
    "latest_review.submission_generation": (MANDATORY, ""),
    "latest_review.is_current": (MANDATORY, ""),
    "latest_review.closed_by_decision": (MANDATORY, ""),
    "latest_review.self_approved": (MANDATORY, ""),
    "latest_review.findings": (MANDATORY, ""),
    "review_report.state": (MANDATORY, ""),
    "review_report.branch": (MANDATORY, ""),
    "review_report.submission_sha": (MANDATORY, ""),
    "review_report.diff_files": (MANDATORY, ""),
    "review_report.diff_lines": (MANDATORY, ""),
    "review_report.diff_note": (MANDATORY, ""),
    "review_report.machine_review": (MANDATORY, ""),
    "machine_review.id": (MANDATORY, ""),
    "machine_review.is_current": (MANDATORY, ""),
    "machine_review.model": (MANDATORY, ""),
    "machine_review.raw_count": (MANDATORY, ""),
    "machine_review.findings_confirmed": (MANDATORY, ""),
    "machine_review.findings_rejected": (MANDATORY, ""),
    "machine_review.incomplete": (MANDATORY, ""),
    "machine_review.incomplete_reason": (MANDATORY, ""),
    "machine_review.unresolved": (MANDATORY, ""),
    "machine_review.lost_dimensions": (MANDATORY, ""),
    "machine_review.profile": (MANDATORY, ""),
    "machine_review.submitted_by": (MANDATORY, ""),
    "machine_review.self_reviewed": (MANDATORY, ""),
    "machine_review.principal_id": (MANDATORY, ""),
    "machine_review.ci_integrity_verdict": (MANDATORY, ""),
    "machine_review.ci_integrity_explanation": (MANDATORY, ""),
    "machine_review.dispositions": (MANDATORY, ""),
    "machine_review.observation_closures": (MANDATORY, ""),
    "machine_review.deferred_repeats": (MANDATORY, ""),
    "machine_review.outcome": (MANDATORY, ""),
    "machine_review.outcome_label": (MANDATORY, ""),
    "machine_review.outcome_names_clean": (MANDATORY, ""),
    "machine_review.second_door_channel": (MANDATORY, ""),
    "machine_review.second_door_reason": (MANDATORY, ""),
    "acceptance_criteria": (BLOCK, ""),
    "call_sites": (BLOCK, ""),
    "description": (TEXT, ""),
    "scope_in": (TEXT, ""),
    "scope_out": (TEXT, ""),
    "scope_growth": (TEXT, ""),
    "out_of_scope_for_review": (TEXT, ""),
    "review_checklist": (TEXT, ""),
    "validation_commands": (TEXT, ""),
    "constraints": (TEXT, ""),
    "technical_hints": (TEXT, ""),
    "outcome_metric": (TEXT, ""),
    "outcome_indicator": (TEXT, ""),
    "outcome_deadline": (TEXT, ""),
    "outcome_revisit_condition": (TEXT, ""),
    "redesign_rationale": (TEXT, ""),
    "latest_submission_summary": (TEXT, ""),
    "outcome_status": (
        FULL_ONLY,
        "label of the outcome-hypothesis lifecycle; says nothing about this submission",
    ),
    "redesign_decision": (
        FULL_ONLY,
        "planning label set at DoR, not evidence about the work",
    ),
    "agent_fit": (FULL_ONLY, "planning label set at DoR, not evidence about the work"),
    "pr_number": (
        FULL_ONLY,
        "pointer only; branch and the pinned sha carry the identity",
    ),
    "timings": (
        FULL_ONLY,
        "assembly time of the brief itself (#1652), not data about the submission",
    ),
    "machine_review.task_id": (FULL_ONLY, "duplicate of task_id"),
    "machine_review.submission_generation": (
        FULL_ONLY,
        "is_current already says whether the report is of this generation",
    ),
    "machine_review.harness_skill": (
        FULL_ONLY,
        "how the review was run, not what it found",
    ),
    "machine_review.harness_version": (
        FULL_ONLY,
        "how the review was run, not what it found",
    ),
    "machine_review.agent_count": (FULL_ONLY, "cost of the run, not its findings"),
    "machine_review.tokens_spent": (FULL_ONLY, "cost of the run, not its findings"),
    "machine_review.duration_ms": (FULL_ONLY, "cost of the run, not its findings"),
    "machine_review.orchestrator": (
        FULL_ONLY,
        "cost/plumbing of the run, not its findings",
    ),
    "machine_review.provider_tokens": (
        FULL_ONLY,
        "billing of the run, not its findings",
    ),
    "machine_review.created_at": (
        FULL_ONLY,
        "timestamp of the report; is_current carries the freshness",
    ),
}


def fields_of(cls: str, prefix: str = "") -> list[str]:
    """Names (without ``prefix``) of the entries of one class under ``prefix``."""
    start = f"{prefix}." if prefix else ""
    return [
        path[len(start) :]
        for path, (klass, _reason) in FIELD_CLASS.items()
        if klass == cls and path.startswith(start) and "." not in path[len(start) :]
    ]
