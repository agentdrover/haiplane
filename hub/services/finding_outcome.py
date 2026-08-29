"""Every confirmed finding ends in a named outcome, not in silence (#911).

Measured before this existed: 47 confirmed findings over seven days and zero
judgements. A finding nobody answers does not become false — it becomes
invisible, and the defect it named ships. Worse, the two numbers that would
tell us whether machine review is worth its price (precision and
``tokens_per_fixed``) cannot be computed at all without a denominator, so the
practice pays for reviews it cannot evaluate.

The gate asks the AUTHOR, at the moment of resubmission, what became of each
finding the previous submission was sent back over. That is the one moment when
the answer is cheap: the author has just been in the code.

**The author's account is not the human's judgement.** It is stored in its own
table and never counted as a disposition — see the ``create_finding_outcomes``
migration for why that separation is load-bearing rather than tidy.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.models import FindingOutcome, FindingOutcomeItem
from hub.services.finding_identity import finding_uids


async def open_findings(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> list[dict[str, Any]]:
    """Confirmed findings of THIS generation that their author has not closed.

    ``generation`` is the one being resubmitted OVER — the submission whose
    report sent the work back. The report for the submission being made does
    not exist yet, so asking about it would be asking about the future.

    One ROW PER REPORT, not per uid. The profile ladder (#879) can leave a lite
    run and a deep run on one submission, and both may carry the same defect —
    which, being derived from content, carries the same ``finding_uid`` (#1007).
    Keyed by uid alone, the second report's row would vanish from this list and
    its finding would stay unanswered forever: the very disappearance being
    measured. The author still answers ONCE per defect; it is the storage that
    fans out, in :func:`plan_outcomes`.
    """
    reports = await repo.machine_reviews_of_generation(db, task_id, generation)
    out: list[dict[str, Any]] = []
    for row in reports:
        review = dict(row)
        try:
            confirmed = json.loads(review.get("findings_confirmed") or "[]")
        except ValueError:
            confirmed = []
        if not isinstance(confirmed, list) or not confirmed:
            continue
        answered = {
            str(dict(r)["finding_uid"])
            for r in await repo.list_finding_outcomes(db, int(review["id"]))
        }
        for index, (uid, finding) in enumerate(zip(finding_uids(confirmed), confirmed)):
            if uid in answered:
                continue
            entry = finding if isinstance(finding, dict) else {}
            out.append(
                {
                    "review_id": int(review["id"]),
                    "finding_index": index,
                    "finding_uid": uid,
                    "title": str(entry.get("title") or ""),
                    "severity": str(entry.get("severity") or ""),
                    "file": str(entry.get("file") or ""),
                }
            )
    return out


def plan_outcomes(
    open_items: list[dict[str, Any]], items: list[FindingOutcomeItem]
) -> tuple[list[tuple[dict[str, Any], FindingOutcomeItem]], list[dict[str, Any]]]:
    """What WOULD be written, and what would still be owed. Writes nothing.

    Split from the writing on purpose. The gate sits among other gates that can
    still refuse the submission after it — the surface check and the diff rules
    both raise 422 — and every write here lands on the process-wide connection
    that :func:`hub.db.get_db` hands out. A refusal does not roll that back, so
    recording first meant a rejected submission left its outcomes behind: the
    author's corrected, COMPLETE second attempt then failed with "finding X is
    not open", because their own discarded attempt had closed it. Nothing is
    written until every gate has agreed.

    One answer per uid closes that defect in EVERY report of the generation.
    The author is answering about a defect, not about a report, and the ladder
    can describe one defect twice.
    """
    by_uid: dict[str, list[dict[str, Any]]] = {}
    for found in open_items:
        by_uid.setdefault(str(found["finding_uid"]), []).append(found)
    writes: list[tuple[dict[str, Any], FindingOutcomeItem]] = []
    answered: set[str] = set()
    for item in items:
        rows = by_uid.get(item.finding_uid)
        if rows is None:
            # Addressing a finding this generation does not carry is not a
            # partial success: the author answered about something else.
            raise ValueError(
                f"finding {item.finding_uid} is not an open confirmed finding "
                f"of this submission"
            )
        answered.add(item.finding_uid)
        writes.extend((found, item) for found in rows)
    still_open = [
        found for found in open_items if str(found["finding_uid"]) not in answered
    ]
    return writes, still_open


def refusal_text(open_items: list[dict[str, Any]]) -> str:
    """Name the findings, never just the count.

    A refusal that says "you have 9 unanswered findings" makes the author go
    looking for which nine. The gate already knows.
    """
    listed = "; ".join(
        f"[{i['severity'] or 'severity не назван'}] {i['title'] or 'без заголовка'}"
        f" (uid {i['finding_uid']})"
        for i in open_items[:10]
    )
    more = f" и ещё {len(open_items) - 10}" if len(open_items) > 10 else ""
    return (
        f"не закрыты подтверждённые находки предыдущей сдачи ({len(open_items)}): "
        f"{listed}{more}. На каждую назовите исход — fixed, false_positive, "
        "wont_fix или deferred — и одну строку почему для всего, кроме fixed. "
        "Находка без исхода не становится неверной, она становится невидимой."
    )


async def apply_outcomes(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    writes: list[tuple[dict[str, Any], FindingOutcomeItem]],
    *,
    reported_by: str,
) -> list[int]:
    """Store what :func:`plan_outcomes` decided, once nothing can refuse it.

    Returns the ids of the defect drafts created. ``wont_fix`` and ``deferred``
    both mean the defect is still in the code after this submission, so both
    leave something a person can schedule; ``false_positive`` disputes the
    finding and ``fixed`` closes it, and neither leaves work behind.

    The draft is a DRAFT on purpose. An outcome is one agent's sentence about
    its own work, and turning that into scheduled work without a human would
    let an author create the backlog that judges it.
    """
    drafts: list[int] = []
    spawned_for: set[str] = set()
    for found, item in writes:
        await repo.upsert_finding_outcome(
            db,
            review_id=int(found["review_id"]),
            task_id=task_id,
            submission_generation=generation,
            finding_uid=item.finding_uid,
            finding_index=int(found["finding_index"]),
            finding_title=str(found["title"]),
            outcome=item.outcome.value,
            note=item.note.strip(),
            linked_task_id=item.linked_task_id,
            reported_by=reported_by,
        )
        if item.outcome not in (FindingOutcome.wont_fix, FindingOutcome.deferred):
            continue
        if item.linked_task_id is not None:
            # The work already exists and the author named it. Opening a second
            # place to track one defect is how a backlog stops being a list of
            # what is left to do.
            continue
        if item.finding_uid in spawned_for:
            # One defect, one draft — the ladder can hand us the same finding
            # from two reports, and it is still one thing to schedule.
            continue
        spawned_for.add(item.finding_uid)
        drafts.append(
            await _spawn_defect_draft(db, task_id, found, item, reported_by=reported_by)
        )
    return drafts


async def _spawn_defect_draft(
    db: aiosqlite.Connection,
    task_id: int,
    found: dict[str, Any],
    item: FindingOutcomeItem,
    *,
    reported_by: str,
) -> int:
    """A defect draft carrying the finding, its author's reason and its origin."""
    verb = (
        "решено не чинить"
        if item.outcome is FindingOutcome.wont_fix
        else "отложено на отдельную работу"
    )
    title = (found["title"] or "находка без заголовка")[:200]
    draft_id = await repo.create_task(
        db,
        title=title,
        description=(
            f"Находка машинного ревью задачи #{task_id}, которую сдача не "
            f"закрыла: {verb}.\n\n"
            f"Слова автора: {item.note.strip()}\n\n"
            f"severity: {found['severity'] or 'не назван'}\n"
            f"файл: {found['file'] or 'не назван'}\n"
            f"finding_uid: {found['finding_uid']}\n\n"
            "Заведено автоматически на сдаче (#911), чтобы находка не исчезла "
            "вместе с решением её не чинить. Драфт, а не открытая задача: "
            "планирует работу человек."
        ),
        runtime="auto",
        source="agent",
        assigned_agent=reported_by,
        rationale=(
            f"Исход находки на сдаче задачи #{task_id}: {item.outcome.value}. "
            "Дефект остаётся в коде после этой сдачи."
        ),
        status="draft",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(
        db,
        draft_id,
        work_type="bug",
        found_in="review",
        caused_by_task_id=task_id,
    )
    return draft_id
