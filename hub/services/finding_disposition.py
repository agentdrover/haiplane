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


async def record_finding_dispositions(
    db: aiosqlite.Connection,
    task_id: int,
    items: list[FindingDispositionItem],
    *,
    decided_by: str,
) -> dict[str, Any]:
    """Store the gate's judgement of the CURRENT report's confirmed findings.

    Raises ``LookupError`` when the task or its report is missing and
    ``ValueError`` when an index points outside the findings that report
    actually carries — a disposition for finding #7 of a five-finding report is
    not a partial success, it means the caller judged something else.
    """
    if await repo.get_task(db, task_id) is None:
        raise LookupError(f"task #{task_id} not found")
    row = await repo.get_latest_machine_review(db, task_id)
    if row is None:
        raise LookupError(f"task #{task_id} has no machine review to judge")
    review = dict(row)
    try:
        confirmed = json.loads(review.get("findings_confirmed") or "[]")
    except ValueError:
        confirmed = []
    if not isinstance(confirmed, list):
        confirmed = []

    for item in items:
        if item.finding_index >= len(confirmed):
            raise ValueError(
                f"finding_index={item.finding_index} is outside the "
                f"{len(confirmed)} confirmed finding(s) of review "
                f"#{review['id']}"
            )

    for item in items:
        entry = confirmed[item.finding_index]
        title = str(entry.get("title") or "") if isinstance(entry, dict) else ""
        await repo.upsert_finding_disposition(
            db,
            review_id=int(review["id"]),
            task_id=task_id,
            submission_generation=int(review["submission_generation"] or 0),
            finding_index=item.finding_index,
            finding_title=title[:300],
            disposition=item.disposition.value,
            note=item.note,
            decided_by=decided_by,
        )
    judged = len(await repo.list_finding_dispositions(db, int(review["id"])))
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
