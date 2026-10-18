"""The author's answers beside a finding the reviewer did not mark (#1830).

«Без разметки ревьюера» и «автор ответил» — разные сущности. Ответ автора
лежит в finding_outcomes, не становится диспозицией (#876) и ни одна метрика
ревью его не читает; здесь он только ЧИТАЕТСЯ и называется словами — одними и
теми же для записи вердикта и для карточки.
"""

from __future__ import annotations

from typing import Any

from hub import repository as repo_module
from hub.db import fetchall


def undisposed_findings(machine_review) -> list[Any]:
    """The confirmed findings of a CURRENT report that nobody judged.

    ``finding_index`` indexes ``findings_confirmed`` — the same contract
    record_finding_dispositions enforces when it refuses an index outside the
    report. Zero dispositions means nobody judged them, never that they were
    all fine (#549).
    """
    if machine_review is None or not getattr(machine_review, "is_current", False):
        return []
    judged = {d.finding_index for d in machine_review.dispositions}
    # #1244: a finding a later foreign observation refuted by name is not
    # "unanswered" — the answer is the live check it links to.
    observed = {c.finding_uid for c in machine_review.observation_closures}
    return [
        finding
        for i, finding in enumerate(machine_review.findings_confirmed)
        if i not in judged and finding.finding_uid not in observed
    ]


async def _latest_answers(db, task_id: int, uids: list[str]) -> dict[str, dict]:
    """The latest confirmed-section answer per uid, over every report of the task.

    ONE query for all uids, however many findings the report carries. Same
    order as ``finding_outcome_for_uid``: newest generation, then newest row.
    """
    if not uids:
        return {}
    marks = ",".join("?" for _ in uids)
    rows = await fetchall(
        db,
        "SELECT * FROM finding_outcomes WHERE task_id=? AND finding_kind='confirmed' "  # nosec B608 - placeholders only
        f"AND finding_uid IN ({marks}) "
        "ORDER BY submission_generation DESC, rowid DESC",
        (task_id, *uids),
    )
    latest: dict[str, dict] = {}
    for row in rows:
        latest.setdefault(str(row["finding_uid"]), dict(row))
    return latest


async def attach_author_answers(db, view) -> None:
    """Load the author's answers to the confirmed findings (#1830).

    The ONE loader: the verdict record and the card both read it, so they show
    the same thing. An answer given to THIS report wins; otherwise the latest
    answer to the same uid from an earlier report of the task is used and marked
    historical (``finding_outcome_for_uid``, as the deferred-repeat check does).
    Only the confirmed vocabulary. Nothing is written and nothing here is a
    disposition: precision reads finding_dispositions only.
    """
    from hub.models import AuthorAnswerView

    view.author_answers = []
    view.undisposed_author_lines = []
    if not getattr(view, "is_current", False) or not view.findings_confirmed:
        return
    own = {
        str(dict(r)["finding_uid"]): dict(r)
        for r in await repo_module.list_finding_outcomes(db, int(view.id))
        if (dict(r).get("finding_kind") or "confirmed") == "confirmed"
    }
    unanswered = [
        f.finding_uid for f in view.findings_confirmed if f.finding_uid not in own
    ]
    history = await _latest_answers(db, int(view.task_id), unanswered)
    for finding in view.findings_confirmed:
        uid = finding.finding_uid
        row = own.get(uid) or history.get(uid)
        if row is None:
            continue
        review_id = int(row.get("review_id") or 0)
        view.author_answers.append(
            AuthorAnswerView(
                finding_uid=uid,
                outcome=str(row.get("outcome") or ""),
                linked_task_id=row.get("linked_task_id"),
                review_id=review_id,
                generation=int(row.get("submission_generation") or 0),
                historical=review_id != int(view.id),
            )
        )
    # Stamped here so the card reads the very lines the verdict record is
    # written from (AC-2): one computation, two readers.
    view.undisposed_author_lines = undisposed_lines(view)


def author_answer_text(answer) -> str:
    """One author's answer as a sentence; the same words on the card and in the record."""
    if answer.linked_task_id:
        text = f"ответ автора: {answer.outcome} → #{answer.linked_task_id}"
    elif answer.outcome == "deferred":
        # The auto-draft's number is not promised: the outcome does not flow
        # back to the draft, and linked_task_id is not guessed.
        text = "ответ автора: deferred без ссылки (авто-драфт)"
    else:
        text = f"ответ автора: {answer.outcome}"
    if answer.historical:
        text += f" (ответ отчёту #{answer.review_id}, поколение {answer.generation})"
    return text


def undisposed_lines(machine_review) -> list[str]:
    """Per undisposed finding: its title and what the author said, or that nobody did.

    Empty when no author answered any of them — then the old sentence stands.
    """
    undisposed = undisposed_findings(machine_review)
    answers = {a.finding_uid: a for a in getattr(machine_review, "author_answers", [])}
    if not any(f.finding_uid in answers for f in undisposed):
        return []
    lines = []
    for finding in undisposed:
        answer = answers.get(finding.finding_uid)
        said = author_answer_text(answer) if answer else "ответа автора нет"
        lines.append(f"{finding.finding_uid[:8]} «{finding.title[:80]}»: {said}")
    return lines


def undisposed_note(
    confirmed: int, undisposed: int, lines: list[str] | None = None
) -> str:
    """The sentence a verdict carries when it overrode findings nobody answered.

    Written for the RECORD, not the screen: the screen is read once and the
    record is what someone reads a week later asking why this shipped.
    ``lines`` (see :func:`undisposed_lines`) separate "the reviewer did not mark
    it" from "the author answered it"; "никто не сказал" is kept for a finding
    with neither.
    """
    if not undisposed:
        return ""
    if not lines:
        return (
            f"ОДОБРЕНО ПРИ НЕРАЗМЕЧЕННЫХ НАХОДКАХ: текущий отчёт ревью несёт "
            f"{confirmed} подтверждённых, из них {undisposed} без диспозиции — "
            "никто не сказал, чем они оказались. Вердикт записан; находки "
            "остаются неотвеченными."
        )
    silent = sum(1 for line in lines if line.endswith("ответа автора нет"))
    tail = f" По {silent} из них никто не сказал, чем они оказались." if silent else ""
    return (
        f"ОДОБРЕНО ПРИ НЕРАЗМЕЧЕННЫХ НАХОДКАХ: текущий отчёт ревью несёт "
        f"{confirmed} подтверждённых, из них {undisposed} без диспозиции "
        f"(без разметки ревьюера: {undisposed}). " + "; ".join(lines) + "."
        f"{tail} Вердикт записан; разметки ревьюера по ним нет."
    )
