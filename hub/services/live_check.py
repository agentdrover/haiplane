"""Evidence that someone watched the work behave after it shipped (#813, #811).

On 21.08.2026 three defects — #801, #802, #803 — passed an APPROVED review and
a green CI and were found only by running against production. None of them was
visible in a diff: one needed a request to the live API, one a real call to an
external command, one an actually-closed pull request. The process had no place
to record that such a run had happened, so a task that was exercised and a task
that nobody ever touched looked exactly alike on the board.

This module stores that evidence. Three decisions shape it:

1. **The hub does not run the check.** It keeps what someone else observed.
   Executing task-supplied commands on the production box was ruled out on
   31.07.2026, and building a second place where that happens would trade a
   reporting gap for a much worse one.

2. **"Done" costs two facts, not one.** A record claiming a check must say what
   was run and what was seen. "Checked, all good" is the shape a formal stamp
   takes, and the feature named that risk first — so the guard lives in the
   schema rather than in a request to be diligent.

3. **Evidence belongs to a deployment.** It is stored against a sha, and an
   unknown sha stays empty instead of being guessed: an observation of some
   other build says nothing about this one.
"""

from __future__ import annotations

import aiosqlite
from fastapi import HTTPException

from hub import repository as repo
from hub.mcp_envelope import enrich_error_payload
from hub.models import LiveCheckRecord, LiveCheckView

DONE = "done"
NOT_APPLICABLE = "not_applicable"
OUTCOMES = frozenset({DONE, NOT_APPLICABLE})


def _refuse(reason: str, message: str, hint: str) -> HTTPException:
    return HTTPException(
        422,
        detail=enrich_error_payload(
            {
                "reason": reason,
                "actor_hint": "agent",
                "message": message,
                "hint": hint,
            }
        ),
    )


# #837: recorded, but the hub could not confirm the code was deployed. Kept
# distinct from an empty value, which means the row predates the check.
UNVERIFIED = "unknown"


def live_check_view(row: aiosqlite.Row | dict) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "task_id": data["task_id"],
        "sha": data.get("sha") or "",
        "outcome": data.get("outcome") or DONE,
        "probe": data.get("probe") or "",
        "observation": data.get("observation") or "",
        "reason": data.get("reason") or "",
        "recorded_by": data.get("recorded_by"),
        "recorded_agent": data.get("recorded_agent") or "",
        "created_at": data.get("created_at") or "",
        # #837: whether this observation was checked against what production
        # runs. Empty for rows written before the check existed.
        "deploy_state": data.get("deploy_state") or "",
    }


async def record_live_check(
    db: aiosqlite.Connection,
    task_id: int,
    body: LiveCheckRecord,
    *,
    agent: str,
    principal_id: int | None,
) -> LiveCheckView:
    """Store one observation of this task's behaviour in production."""
    task = await repo.get_task(db, task_id)
    if not task:
        raise HTTPException(404, "task not found")

    outcome = (body.outcome or DONE).strip().lower()
    if outcome not in OUTCOMES:
        raise _refuse(
            "invalid_outcome",
            f"outcome must be one of {sorted(OUTCOMES)}, got '{outcome}'",
            "done = it was observed working; not_applicable = there is nothing "
            "to observe, and that needs a reason.",
        )

    probe = (body.probe or "").strip()
    observation = (body.observation or "").strip()
    reason = (body.reason or "").strip()

    if outcome == DONE and not (probe and observation):
        raise _refuse(
            "incomplete_evidence",
            "a live check needs both what you ran and what you saw",
            "Two fields, on purpose: 'checked, all good' is what a stamp looks "
            "like. Give the command or request in probe and its result in "
            "observation.",
        )
    if outcome == NOT_APPLICABLE and not reason:
        raise _refuse(
            "missing_reason",
            "not_applicable needs a reason",
            "'Nothing to observe' is a claim about the task — say why, so a "
            "reader can disagree with it.",
        )

    # What shipped is what should be observed. The caller may name the sha
    # explicitly (a check can happen long after the deploy); otherwise the
    # merge the delivery gate recorded is the best answer the hub has, and
    # when it has none the field stays empty rather than invented.
    sha = (body.sha or "").strip() or await repo.merge_sha_for_task(db, task_id)

    # #837: "verified in production" about code that is not in production is
    # the most expensive kind of evidence — it looks stronger than every other
    # block on the card and is false. On 21.08.2026 nothing stopped it: task
    # #823 was completed with its PR merged into develop, the deploy job was
    # skipped, and only opening GitHub's logs revealed that the panel being
    # "checked" did not exist yet.
    #
    # Three answers, and only ONE of them refuses. Unknown is recorded, not
    # blocked: an installation without delivery facts knows nothing about
    # production, and refusing there would turn ignorance into a gate — the
    # same line #839 draws for an empty release table.
    deploy_state = ""
    if outcome == DONE:
        from hub.services.delivery_state import IN_PROD, NOT_IN_PROD, delivery_state

        delivery = await delivery_state(db, task_id)
        deploy_state = str(delivery.get("state") or "")
        if deploy_state == NOT_IN_PROD:
            raise _refuse(
                "not_deployed_yet",
                f"эту работу нельзя наблюдать в проде: {delivery.get('reason', '')}",
                "Запишите not_applicable с причиной, если наблюдать нечего, "
                "либо повторите запись после выката — свидетельство о "
                "нераскатанном коде выглядит сильнее всех прочих блоков и "
                "при этом ложно.",
            )
        if deploy_state != IN_PROD:
            deploy_state = UNVERIFIED

    check_id = await repo.insert_live_check(
        db,
        task_id=task_id,
        sha=sha,
        outcome=outcome,
        probe=probe,
        observation=observation,
        reason=reason,
        recorded_by=principal_id,
        recorded_agent=agent,
        deploy_state=deploy_state,
    )
    await repo.add_task_update(
        db,
        task_id,
        agent,
        "status",
        (
            f"Живая проверка ({outcome}): {probe or reason} → "
            f"{observation or 'наблюдать нечего'}"
            + (f" [sha {sha[:12]}]" if sha else " [sha неизвестен]")
            + (" [не сверено с выкатом]" if deploy_state == UNVERIFIED else "")
        ),
    )
    await db.commit()
    rows = await repo.list_live_checks(db, task_id)
    stored = next((r for r in rows if dict(r)["id"] == check_id), rows[0])
    return LiveCheckView(**live_check_view(stored))


async def list_checks(
    db: aiosqlite.Connection, task_id: int, *, limit: int = 50
) -> list[LiveCheckView]:
    rows = await repo.list_live_checks(db, task_id, limit=limit)
    return [LiveCheckView(**live_check_view(row)) for row in rows]
