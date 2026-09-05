"""Every finding the author is asked about ends in a named outcome, not in
silence (#911, #1085).

Measured before this existed: 47 confirmed findings over seven days and zero
judgements. A finding nobody answers does not become false — it becomes
invisible, and the defect it named ships. Worse, the two numbers that would
tell us whether machine review is worth its price (precision and
``tokens_per_fixed``) cannot be computed at all without a denominator, so the
practice pays for reviews it cannot evaluate. The same silence later hid the
unresolved section: five deep runs left six real defects there and none in
``findings_confirmed``, and the ledger asked about none of them.

The gate asks the AUTHOR, at the moment of resubmission, what became of each
finding the previous submission was sent back over — confirmed or unresolved.
That is the one moment when the answer is cheap: the author has just been in
the code.

**The author's account is not the human's judgement.** It is stored in its own
table and never counted as a disposition — see the ``create_finding_outcomes``
migration for why that separation is load-bearing rather than tidy.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.models import (
    CONFIRMED_OUTCOMES,
    OUTCOMES_LEAVING_WORK,
    UNRESOLVED_OUTCOMES,
    FindingOutcome,
    FindingOutcomeItem,
)
from hub.services.finding_identity import finding_uids, unresolved_uids

#: The two sections of a report an author can be asked about (#1085).
KIND_CONFIRMED = "confirmed"
KIND_UNRESOLVED = "unresolved"

#: Позиция в ``findings_confirmed`` — обещание колонки ``finding_index``.
#: У неразрешённой записи такой позиции нет: она лежит в другом списке. Писать
#: туда её индекс значило бы указать на чужую находку, поэтому пишется -1 —
#: то же значение, которым схема помечает «индекс не назван».
NO_CONFIRMED_INDEX = -1


async def open_findings(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> list[dict[str, Any]]:
    """Confirmed and unresolved findings of THIS generation not yet closed.

    ``generation`` is the one being resubmitted OVER — the submission whose
    report sent the work back. The report for the submission being made does
    not exist yet, so asking about it would be asking about the future.

    Both sections of that report are owed an answer (#1085). Confirmed is the
    defect the adjudicators agreed on; unresolved is the one they split on.
    Asking only about confirmed is how six real defects from five paid runs
    left no row in the ledger.

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
        confirmed = _section(review, "findings_confirmed")
        unresolved = _section(review, "unresolved")
        if not confirmed and not unresolved:
            continue
        answered = {
            str(dict(r)["finding_uid"])
            for r in await repo.list_finding_outcomes(db, int(review["id"]))
        }
        review_id = int(review["id"])
        for index, (uid, finding) in enumerate(zip(finding_uids(confirmed), confirmed)):
            if uid in answered:
                continue
            entry = finding if isinstance(finding, dict) else {}
            out.append(
                {
                    "review_id": review_id,
                    "finding_index": index,
                    "finding_uid": uid,
                    "finding_kind": KIND_CONFIRMED,
                    "title": str(entry.get("title") or ""),
                    "severity": str(entry.get("severity") or ""),
                    "file": str(entry.get("file") or ""),
                }
            )
        for uid, finding in zip(unresolved_uids(unresolved), unresolved):
            if uid in answered:
                continue
            entry = finding if isinstance(finding, dict) else {}
            out.append(
                {
                    "review_id": review_id,
                    "finding_index": NO_CONFIRMED_INDEX,
                    "finding_uid": uid,
                    "finding_kind": KIND_UNRESOLVED,
                    "title": str(entry.get("title") or ""),
                    # An unresolved record carries neither severity nor file —
                    # the schema has no such fields. Empty here is the absence
                    # itself, not a value that went missing.
                    "severity": "",
                    "file": "",
                    "why": str(entry.get("why") or ""),
                }
            )
    return out


def _section(review: dict[str, Any], column: str) -> list[Any]:
    """One stored findings list, or an empty one when it cannot be read.

    Reports written before a column existed store nothing at all, and a report
    that fails to parse is not a report with findings: in both cases the honest
    answer is "no findings here", never an exception on a read path the gate
    runs on every submission.
    """
    try:
        parsed = json.loads(review.get(column) or "[]")
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


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
                f"of this submission, and not an open unresolved one either: "
                f"the id is derived from the finding's own content, so check "
                f"it against the report"
            )
        _refuse_foreign_dictionary(rows[0], item)
        answered.add(item.finding_uid)
        writes.extend((found, item) for found in rows)
    still_open = [
        found for found in open_items if str(found["finding_uid"]) not in answered
    ]
    return writes, still_open


def _refuse_foreign_dictionary(found: dict[str, Any], item: FindingOutcomeItem) -> None:
    """Каждый раздел отвечает своими словами (#1085).

    Словарь confirmed отвечает на вопрос «что вы сделали с дефектом, о котором
    ДОГОВОРИЛИСЬ». У неразрешённой находки договорённости нет по определению —
    адъюдикаторы разошлись, — и ``false_positive`` про неё записал бы, что гейт
    находку подтвердил, а автор объявил ложной. Гейт не подтверждал ничего.
    Молчаливый перевод одного слова в другое стёр бы ровно то различие, ради
    которого второй словарь и заведён, поэтому чужое слово отклоняется вслух.
    """
    kind = str(found.get("finding_kind") or KIND_CONFIRMED)
    allowed = UNRESOLVED_OUTCOMES if kind == KIND_UNRESOLVED else CONFIRMED_OUTCOMES
    if item.outcome in allowed:
        return
    words = ", ".join(sorted(o.value for o in allowed))
    section = (
        "неразрешённой (адъюдикаторы не сошлись, судит автор)"
        if kind == KIND_UNRESOLVED
        else "подтверждённой"
    )
    raise ValueError(
        f"исход '{item.outcome.value}' не из словаря {section} находки "
        f"{item.finding_uid}: назовите одно из {words}"
    )


def refusal_text(open_items: list[dict[str, Any]]) -> str:
    """Name the findings, never just the count.

    A refusal that says "you have 9 unanswered findings" makes the author go
    looking for which nine. The gate already knows.

    The two sections are listed apart (#1085) because they are answered with
    different words, and one list under one dictionary would hand the author a
    vocabulary that fits half of it.
    """
    confirmed = [i for i in open_items if i.get("finding_kind") != KIND_UNRESOLVED]
    unresolved = [i for i in open_items if i.get("finding_kind") == KIND_UNRESOLVED]
    parts: list[str] = []
    if confirmed:
        parts.append(
            f"не закрыты подтверждённые находки предыдущей сдачи "
            f"({len(confirmed)}): {_listed(confirmed)}. На каждую назовите "
            "исход — fixed, false_positive, wont_fix или deferred — и одну "
            "строку почему для всего, кроме fixed."
        )
    if unresolved:
        parts.append(
            f"не закрыты неразрешённые находки предыдущей сдачи "
            f"({len(unresolved)}): {_listed(unresolved)}. Их адъюдикаторы не "
            "рассудили, поэтому судит автор: real_fixed, real_deferred, "
            "not_a_defect или not_judged — и одну строку почему для всего, "
            "кроме real_fixed."
        )
    return (
        " ".join(parts)
        + " Находка без исхода не становится неверной, она становится невидимой."
    )


def _listed(items: list[dict[str, Any]]) -> str:
    """Поимённо, до десяти, с хвостом счётом.

    Severity печатается только там, где она бывает: у неразрешённой записи
    такого поля нет в схеме, и «severity не назван» про неё читалось бы как
    забытое поле, а не как поле, которого не существует.
    """
    chunks: list[str] = []
    for i in items[:10]:
        title = i["title"] or "без заголовка"
        if i.get("finding_kind") == KIND_UNRESOLVED:
            chunks.append(f"{title} (uid {i['finding_uid']})")
            continue
        severity = i["severity"] or "severity не назван"
        chunks.append(f"[{severity}] {title} (uid {i['finding_uid']})")
    more = f" и ещё {len(items) - 10}" if len(items) > 10 else ""
    return "; ".join(chunks) + more


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
    finding and ``fixed`` closes it, and neither leaves work behind. The
    unresolved dictionary splits the same way (#1085): ``real_deferred`` and
    ``not_judged`` leave the defect where it is — the second one is the author
    saying they never looked, which is the answer most in need of a follow-up —
    while ``real_fixed`` and ``not_a_defect`` close the row.

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
            finding_kind=str(found.get("finding_kind") or KIND_CONFIRMED),
        )
        if item.outcome not in OUTCOMES_LEAVING_WORK:
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
    verb = {
        FindingOutcome.wont_fix: "решено не чинить",
        FindingOutcome.real_deferred: "отложено на отдельную работу",
        FindingOutcome.not_judged: "не разбиралась",
    }.get(item.outcome, "отложено на отдельную работу")
    unresolved = str(found.get("finding_kind") or "") == KIND_UNRESOLVED
    origin = (
        "Находку никто не рассудил: адъюдикаторы разошлись, и это суждение "
        "АВТОРА, а не подтверждение гейта.\n\n"
        if unresolved
        else ""
    )
    title = (found["title"] or "находка без заголовка")[:200]
    draft_id = await repo.create_task(
        db,
        title=title,
        description=(
            f"Находка машинного ревью задачи #{task_id}, которую сдача не "
            f"закрыла: {verb}.\n\n"
            f"{origin}"
            f"Слова автора: {item.note.strip()}\n\n"
            + (
                ""
                if unresolved
                # У неразрешённой записи этих полей нет в схеме вовсе, и
                # «не назван» про них прочиталось бы как забытое поле.
                else f"severity: {found['severity'] or 'не назван'}\n"
                f"файл: {found['file'] or 'не назван'}\n"
            )
            + f"finding_uid: {found['finding_uid']}\n\n"
            "Заведено автоматически на сдаче (#911), чтобы находка не исчезла "
            "вместе с исходом, который её не закрыл. Драфт, а не открытая "
            "задача: планирует работу человек."
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
