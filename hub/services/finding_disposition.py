"""What a machine-review finding turned out to be, once a human looked (#876).

Until now the only measure of review quality was the review itself.
``findings_confirmed`` and ``findings_rejected`` are one run's internal
adjudication — its verifiers agreeing with each other — and ``filtration_rate``
divides one by the other. That is a harness grading its own homework.

Nobody recorded what happened to a finding AFTER the gate. So:

* precision by profile and by model could not be computed at all, which is
  exactly the comparison the cheap-vs-expensive decision needs;
* ``tokens_per_confirmed`` priced findings that may never have been fixed, and
  a confirmed finding and a fixed one are different things.

Three rules shape this module:

1. **A human decides.** The agent whose work was reviewed cannot declare a
   finding false — that is the reviewed party grading its reviewer. Enforced by
   the route dependency, stated here so the reason survives the refactor.

2. **The boundary is written down, not intuited.** ``false_positive`` means the
   described defect is NOT in the code; ``wont_fix`` means it is there and we
   choose not to fix it. Precision depends on that line, so it is repeated at
   the buttons rather than left to whoever is clicking.

3. **Absence stays absence.** Reports written before this existed get no
   default disposition, and an index nobody judged is simply missing from the
   list. "Not stated" is an answer (#549).
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.models import FindingDispositionItem
from hub.services.finding_identity import finding_uids
from hub.services.gate_events import DISPOSITION_RECORDED


async def record_finding_dispositions(
    db: aiosqlite.Connection,
    task_id: int,
    items: list[FindingDispositionItem],
    *,
    decided_by: str,
    review_id: int | None = None,
) -> dict[str, Any]:
    """Store the gate's judgement of the CURRENT report's confirmed findings.

    ``review_id`` names WHICH report is being judged, and the queue (#1038) is
    the first caller that has to. The ladder (#879) can leave two reports on one
    generation, and both are current; the task card renders only the newest, so
    "the latest report" was an unambiguous answer everywhere until a page
    started showing the earlier one. Judging it without naming it wrote the
    verdict onto the newest report instead — marking a finding nobody read and
    leaving the one they did read in the queue. Omitted, the newest is still
    used, which is what every caller written before this meant.

    Raises ``LookupError`` when the task or its report is missing and
    ``ValueError`` when the address points at nothing this report carries — a
    disposition for finding #7 of a five-finding report is not a partial
    success, it means the caller judged something else.

    Addressing is by ``finding_uid`` (#1007); ``finding_index`` is still
    resolved for callers written before uids existed. Both are stored: the slot
    keys the row inside this report — which is immutable, so the slot is stable
    — and the uid is what identifies the same defect in the NEXT report, where
    the position will be different.
    """
    task = await repo.get_task(db, task_id)
    if task is None:
        raise LookupError(f"task #{task_id} not found")
    if review_id is None:
        row = await repo.get_latest_machine_review(db, task_id)
    else:
        row = await repo.get_machine_review(db, review_id)
        if row is not None and int(dict(row)["task_id"]) != task_id:
            # Naming another task's report is not a near miss to be resolved
            # into something reasonable — it is a judgement filed against work
            # nobody asked about.
            raise ValueError(
                f"report #{review_id} belongs to task "
                f"#{dict(row)['task_id']}, not #{task_id}"
            )
    if row is None:
        raise LookupError(f"task #{task_id} has no machine review to judge")
    review = dict(row)
    generation = int(dict(task).get("submission_generation") or 0)
    if int(review["submission_generation"]) != generation:
        # A superseded report describes code a resubmission already replaced.
        # Storing an answer about it would put a judgement in the metrics that
        # was never about the work as it stands.
        raise ValueError(
            f"report #{review['id']} is from submission "
            f"{review['submission_generation']}, the task is on {generation}"
        )
    try:
        confirmed = json.loads(review.get("findings_confirmed") or "[]")
    except ValueError:
        confirmed = []
    if not isinstance(confirmed, list):
        confirmed = []

    uids = finding_uids(confirmed)
    index_by_uid = {uid: idx for idx, uid in enumerate(uids)}

    resolved: list[int] = []
    for item in items:
        if item.finding_uid:
            index = index_by_uid.get(item.finding_uid)
            if index is None:
                raise ValueError(
                    f"finding_uid={item.finding_uid} is not among the "
                    f"{len(confirmed)} confirmed finding(s) of review "
                    f"#{review['id']} — the report may have been resubmitted "
                    "since you read it"
                )
        else:
            index = int(item.finding_index or 0)
            if index >= len(confirmed):
                raise ValueError(
                    f"finding_index={index} is outside the "
                    f"{len(confirmed)} confirmed finding(s) of review "
                    f"#{review['id']}"
                )
        resolved.append(index)

    for item, index in zip(items, resolved):
        entry = confirmed[index]
        title = str(entry.get("title") or "") if isinstance(entry, dict) else ""
        await repo.upsert_finding_disposition(
            db,
            review_id=int(review["id"]),
            task_id=task_id,
            submission_generation=int(review["submission_generation"] or 0),
            finding_index=index,
            finding_uid=uids[index],
            finding_title=title[:300],
            disposition=item.disposition.value,
            note=item.note,
            decided_by=decided_by,
        )
    judged = len(await repo.list_finding_dispositions(db, int(review["id"])))
    await repo.insert_event(
        db,
        kind=DISPOSITION_RECORDED,
        task_id=task_id,
        actor=decided_by,
        payload={
            "judged": judged,
            "confirmed_total": len(confirmed),
        },
    )
    await repo.add_task_update(
        db,
        task_id,
        decided_by,
        "status",
        f"Диспозиция находок машинного ревью: размечено {judged} из "
        f"{len(confirmed)} подтверждённых (#876).",
    )
    await db.commit()
    return {
        "review_id": int(review["id"]),
        "confirmed_total": len(confirmed),
        # Not a percentage: "judged 2 of 5" and "judged 5 of 5" are different
        # states of the same report, and a share hides which one this is.
        "judged": judged,
    }
