"""Суждение без оснований или без уверенности не проходит как вердикт (#1327).

22.09.2026 первое одобрение стюарда на проде (#1271) записано с grounds=[] и
пустой confidence. Такое одобрение нельзя перепроверить, поэтому хаб
понижает его до эскалации — тем же приёмом и в том же месте, что
low_confidence, — а поданный вердикт остаётся в submitted_verdict.

AC-1: approve без оснований → escalate / no_grounds, submitted_verdict=approve.
AC-2: approve без confidence → escalate / no_confidence, submitted_verdict=approve.
AC-3: escalate без оснований принимается как есть: эскалация сама значит
      «судить не могу», требовать от неё фактов незачем.
"""

from __future__ import annotations

import aiosqlite

from hub import repository as repo
from hub.config import TokenIdentity
from hub.models import STEWARD_ESCALATE_REASONS, StewardJudgementSubmit
from hub.services.steward_judgement import record_steward_judgement
from tests.test_steward_shadow import _project, _task

_STEWARD = TokenIdentity("steward-bot", "steward", principal_id=42)


async def _record(db: aiosqlite.Connection, **fields):
    project_id = await _project(db, "grounds")
    task_id = await _task(db, project_id)
    body = {"generation": 1, "kind": "verdict", "model": "gpt-5.3-codex"}
    body.update(fields)
    view = await record_steward_judgement(
        db, task_id, StewardJudgementSubmit(**body), _STEWARD
    )
    row = await repo.get_steward_judgement(db, task_id, 1, "verdict")
    assert row is not None
    return view, row


async def test_an_approval_without_grounds_is_recorded_as_an_escalation(
    db: aiosqlite.Connection,
):
    """AC-1: approve, confidence=high, grounds=[] — случай #1271 без одной детали."""
    view, row = await _record(db, verdict="approve", confidence="high", grounds=[])
    assert view.verdict == "escalate"
    assert view.escalate_reason == "no_grounds"
    assert view.submitted_verdict == "approve"
    assert row["verdict"] == "escalate"
    assert row["escalate_reason"] == "no_grounds"
    assert row["submitted_verdict"] == "approve"
    assert "no_grounds" in STEWARD_ESCALATE_REASONS


async def test_a_return_without_grounds_is_recorded_as_an_escalation(
    db: aiosqlite.Connection,
):
    """AC-1, вторая половина: возврат без оснований понижается так же."""
    view, _ = await _record(
        db, verdict="changes_requested", confidence="high", grounds=[]
    )
    assert view.verdict == "escalate"
    assert view.escalate_reason == "no_grounds"
    assert view.submitted_verdict == "changes_requested"


async def test_an_approval_without_confidence_is_recorded_as_an_escalation(
    db: aiosqlite.Connection,
):
    """AC-2: основания названы, уверенности нет."""
    view, row = await _record(
        db, verdict="approve", grounds=[{"source": "ci_pinned_sha"}]
    )
    assert view.verdict == "escalate"
    assert view.escalate_reason == "no_confidence"
    assert view.submitted_verdict == "approve"
    assert row["verdict"] == "escalate"
    assert row["escalate_reason"] == "no_confidence"
    assert row["submitted_verdict"] == "approve"
    assert "no_confidence" in STEWARD_ESCALATE_REASONS


async def test_a_grounded_confident_approval_stays_an_approval(
    db: aiosqlite.Connection,
):
    """Контроль: правило не задевает обоснованное одобрение."""
    view, _ = await _record(
        db,
        verdict="approve",
        confidence="medium",
        grounds=[{"source": "ci_pinned_sha"}],
    )
    assert view.verdict == "approve"
    assert view.escalate_reason == ""


async def test_an_escalation_needs_no_grounds(db: aiosqlite.Connection):
    """AC-3: эскалация без оснований и без уверенности принята как есть."""
    view, row = await _record(
        db, verdict="escalate", escalate_reason="precondition_failed", grounds=[]
    )
    assert view.verdict == "escalate"
    assert view.escalate_reason == "precondition_failed"
    assert view.submitted_verdict == "escalate"
    assert row["escalate_reason"] == "precondition_failed"
