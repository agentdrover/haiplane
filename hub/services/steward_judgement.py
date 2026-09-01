"""Record a steward judgement. No transition (#1022); events are the audit (#1023)."""

from __future__ import annotations

import json
import logging

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
from hub.services.gate_events import (
    STEWARD_APPLIED,
    STEWARD_ESCALATED,
    STEWARD_JUDGEMENT,
)


log = logging.getLogger(__name__)


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
    expected_generation: int | None = None,
) -> StewardJudgementView:
    """Validate and store a judgement. The task status is not touched.

    ``expected_generation`` is the pin the caller's session carries (#1120).
    When it is set, a judgement about any other generation is refused: a run
    ordered for one submission does not get to rule on the next one just
    because the author resubmitted while it was thinking.
    """
    row = await repo.get_task(db, task_id)
    if row is None:
        raise HTTPException(404, "task not found")
    if expected_generation is not None:
        # The SAME guard the evidence door uses (#1120): one rule, two
        # entrances. A judgement filed for another generation — or for one
        # that stopped being current while the run was thinking — is refused
        # here, not silently stored beside the live submission.
        from hub.services.steward_evidence import pinned_generation

        pinned_generation(identity, dict(row), body.generation)

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
    await repo.add_task_update(
        db,
        task_id,
        identity.username,
        "status",
        f"Steward judgement recorded: {body.kind} {effective_verdict}.",
        principal_id=identity.principal_id,
        author_kind="steward",
    )
    payload = {
        "kind": body.kind,
        "verdict": effective_verdict,
        "generation": body.generation,
    }
    await repo.insert_event(
        db,
        kind=STEWARD_JUDGEMENT,
        task_id=task_id,
        actor="steward",
        payload=payload,
    )
    follow_up = (
        STEWARD_ESCALATED if effective_verdict == "escalate" else STEWARD_APPLIED
    )
    await repo.insert_event(
        db,
        kind=follow_up,
        task_id=task_id,
        actor="steward",
        payload=payload,
    )
    await _close_the_order(db, task_id, body.generation, body.kind)
    await db.commit()
    saved = await repo.get_steward_judgement_by_id(db, inserted)
    if saved is None:
        raise RuntimeError(
            f"steward judgement {inserted} missing after insert for task #{task_id}"
        )
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


async def _close_the_order(db, task_id: int, generation: int, kind: str) -> None:
    """The judgement arrived — the slot it was ordered for is done (#1106).

    Without this the order sits open until its deadline and closes as
    ``run_timeout``: the work is finished and the state says "still waiting".
    That difference matters twice — the daily cap counts an occupied slot,
    and the evidence door (#1075) stays open on an order nobody is filling.

    Best effort by contract: the judgement is already stored, and a slot that
    fails to close is a wrong state, not a lost answer. It closes on the next
    poller tick by deadline anyway.
    """
    try:
        from hub.services.steward_dispatch import RUN_JUDGED, close_run, open_run

        order = await open_run(db, task_id, generation, kind)
        if order is not None:
            await close_run(db, order, RUN_JUDGED, f"суждение записано: {kind}")
    except Exception as exc:  # noqa: BLE001 — the judgement stands regardless
        # WITH the reason (#1106 review). A swallowed failure that names
        # nothing is indistinguishable from "there was no order to close",
        # and the two need different answers: one is normal, the other is a
        # slot that will now expire by deadline as if nobody judged.
        log.warning(
            "steward order not closed after judgement: task #%s gen %s kind %s: %s",
            task_id,
            generation,
            kind,
            exc,
            exc_info=True,
        )
