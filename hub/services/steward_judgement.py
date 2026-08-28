"""Record a steward judgement. No transition, no events (#1022)."""

from __future__ import annotations

import json

from fastapi import HTTPException

from hub import repository as repo
from hub.actionable_errors import (
    steward_closed_vocabulary_detail,
    steward_escalate_reason_required_detail,
    steward_judgement_exists_detail,
    steward_unknown_finding_uid_detail,
    steward_verdict_required_detail,
)
from hub.config import TokenIdentity
from hub.models import (
    STEWARD_CLOSURE_TYPES,
    STEWARD_CONFIDENCE,
    STEWARD_ESCALATE_REASONS,
    STEWARD_GROUND_SOURCES,
    STEWARD_JUDGEMENT_KINDS,
    STEWARD_VERDICTS,
    StewardJudgementSubmit,
    StewardJudgementView,
)
from hub.services.finding_identity import finding_uids


def _require_member(field: str, got: str, allowed: tuple[str, ...]) -> None:
    if got not in allowed:
        raise HTTPException(
            422,
            detail=steward_closed_vocabulary_detail(field, got, allowed),
        )


async def record_steward_judgement(
    db,
    task_id: int,
    body: StewardJudgementSubmit,
    identity: TokenIdentity,
) -> StewardJudgementView:
    """Validate and store a judgement. The task status is not touched."""
    row = await repo.get_task(db, task_id)
    if row is None:
        raise HTTPException(404, "task not found")

    submitted_verdict = (body.verdict or "").strip()
    if not submitted_verdict:
        raise HTTPException(
            422,
            detail=steward_verdict_required_detail(),
        )
    _require_member("kind", body.kind, STEWARD_JUDGEMENT_KINDS)
    _require_member("verdict", submitted_verdict, STEWARD_VERDICTS)
    confidence = (body.confidence or "").strip()
    if confidence:
        _require_member("confidence", confidence, STEWARD_CONFIDENCE)
    for ground in body.grounds:
        _require_member("ground.source", ground.source, STEWARD_GROUND_SOURCES)
    for closure in body.closures:
        _require_member("closure.type", closure.type, STEWARD_CLOSURE_TYPES)

    escalate_reason = (body.escalate_reason or "").strip()
    if confidence == "low":
        effective_verdict = "escalate"
        effective_reason = "low_confidence"
    else:
        effective_verdict = submitted_verdict
        if effective_verdict == "escalate":
            if not escalate_reason:
                raise HTTPException(
                    422,
                    detail=steward_escalate_reason_required_detail(
                        STEWARD_ESCALATE_REASONS
                    ),
                )
            _require_member(
                "escalate_reason", escalate_reason, STEWARD_ESCALATE_REASONS
            )
            effective_reason = escalate_reason
        else:
            if escalate_reason:
                _require_member(
                    "escalate_reason", escalate_reason, STEWARD_ESCALATE_REASONS
                )
            effective_reason = escalate_reason

    if body.closures:
        await _refuse_unknown_closure_uids(db, task_id, body.generation, body.closures)

    inserted = await repo.insert_steward_judgement(
        db,
        task_id=task_id,
        generation=body.generation,
        kind=body.kind,
        submitted_verdict=submitted_verdict,
        verdict=effective_verdict,
        confidence=confidence,
        escalate_reason=effective_reason,
        grounds=json.dumps([g.model_dump() for g in body.grounds], ensure_ascii=False),
        findings=json.dumps(body.findings, ensure_ascii=False),
        closures=json.dumps(
            [c.model_dump() for c in body.closures], ensure_ascii=False
        ),
        model=body.model,
        tokens_spent=body.tokens_spent,
        duration_ms=body.duration_ms,
        submitted_by=identity.username[:100],
        principal_id=identity.principal_id,
    )
    if inserted is None:
        raise HTTPException(
            409,
            detail=steward_judgement_exists_detail(task_id, body.generation, body.kind),
        )
    await db.commit()
    saved = await repo.get_steward_judgement_by_id(db, inserted)
    assert saved is not None
    return StewardJudgementView(**dict(saved))


async def _refuse_unknown_closure_uids(
    db, task_id: int, generation: int, closures
) -> None:
    reports = await repo.machine_reviews_of_generation(db, task_id, generation)
    known: set[str] = set()
    for report in reports:
        raw = report["findings_confirmed"]
        if isinstance(raw, str):
            try:
                entries = json.loads(raw or "[]")
            except ValueError:
                entries = []
        else:
            entries = raw or []
        if not isinstance(entries, list):
            continue
        known.update(finding_uids(entries))
    for closure in closures:
        if closure.finding_uid not in known:
            raise HTTPException(
                422,
                detail=steward_unknown_finding_uid_detail(closure.finding_uid),
            )
