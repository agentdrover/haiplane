"""Возврат на доработку по суждению судьи: применяет поллер, как approve (#2053).

``record_steward_judgement`` статус не двигает, а советник заказывается только
на approve. Без этого прохода ``changes_requested`` судьи никого не возвращал:
сдача стояла в review и (с #2052) лежала во входящих владельца, а автор не
получал ни находок, ни задачи.

Те же замки, что у approve: режим act, проект делегировал вердикт, поколение
суждения - живое, вердикта на него ещё нет, метка занимается условным UPDATE
(повтор и перезапуск хаба её видят). Чего здесь НЕТ, и это решение, а не
недосмотр:

* согласия советника. Он страхует необратимое - одобрение, которое
  доставляет код. Возврат автору обратим сам: ошибочный возврат стоит одного
  цикла, а не релиза, и платный заказ на него не оправдан;
* привратника применения и восьми признаков: они отвечают на вопрос «можно ли
  одобрить», а возврат ничего не одобряет;
* суточного потолка: он считает заказы, а суждение уже получено и оплачено.

Бюджет циклов общий: исчерпан - ``needs_decision`` существующим переходом
(``apply_judgement``).
"""

from __future__ import annotations

import logging

import aiosqlite

from hub import repository as repo
from hub.db import fetchall
from hub.services.result_kind import task_automation_not_applicable
from hub.services.steward_applied import (
    ESCALATED_TO_HUMAN,
    RETURNED,
    apply_judgement,
)

log = logging.getLogger(__name__)

#: Какое суждение подбирает проход: вердикт судьи нового контура с возвратом.
PICKED_CHANGES_SQL = (
    "j.kind='verdict' AND j.verdict='changes_requested' AND j.contour=2"
)

OUTCOME_RETURNED = "returned"
OUTCOME_ESCALATED = "escalated"
OUTCOME_SKIPPED = "skipped"
OUTCOME_FAILED = "failed"
_CLAIMED = "applying"


async def apply_judge_changes_requests(db: aiosqlite.Connection) -> int:
    """Вернуть автору каждую сдачу, где судья текущего поколения просил правок.

    Возвращает число записанных исходов. Вне режима act - ноль и ни одного
    следа: теневой судья ничего не двигает.
    """
    from hub.services.steward_advisor import _stamp_now
    from hub.services.steward_dispatch import _policy_wants_steward
    from hub.services.steward_shadow import effective_mode

    if await effective_mode(db) != "act":
        return 0
    rows = await fetchall(
        db,
        "SELECT j.id, j.task_id, j.generation FROM steward_judgements j "  # nosec B608 - constant fragments only
        "JOIN tasks t ON t.id = j.task_id "
        f"WHERE {PICKED_CHANGES_SQL} "
        "AND j.advisor_outcome='' AND t.status='review' "
        "AND t.submission_generation = j.generation "
        "AND (t.review_verdict_generation IS NULL "
        "     OR t.review_verdict_generation != j.generation)",
    )
    applied = 0
    for row in rows:
        item = dict(row)
        task_id, generation = int(item["task_id"]), int(item["generation"])
        if await task_automation_not_applicable(db, task_id):
            continue  # #1647: state-задачу стюард не двигает
        project = await repo.resolve_project_for_task(db, task_id)
        if not _policy_wants_steward(project, gate="verdict", shadow=False):
            continue
        stamp = await _stamp_now(db)
        cursor = await db.execute(
            "UPDATE steward_judgements SET advisor_outcome=?, advisor_claimed_at=? "
            "WHERE id=? AND advisor_outcome=''",
            (_CLAIMED, stamp, item["id"]),
        )
        await db.commit()
        if cursor.rowcount != 1:
            continue
        outcome, detail = await _apply_one(db, task_id, generation, (item["id"], stamp))
        if await _finish(db, item, stamp, outcome, detail):
            applied += 1
    return applied


async def _apply_one(
    db: aiosqlite.Connection, task_id: int, generation: int, claim: tuple[int, str]
) -> tuple[str, str]:
    try:
        result = await apply_judgement(db, task_id, generation, claim=claim)
    except Exception as exc:  # noqa: BLE001 - исход записывается в любом случае
        from hub.services.refusal_reason import RACE_CODES, code_of, reason_line

        log.warning("judge changes_requested not applied for #%s: %s", task_id, exc)
        # #2054, B-2: другой писатель успел первым (человек поставил вердикт,
        # сдача сдвинулась) - это не сбой применения: skipped, без тревоги.
        race = code_of(exc) in RACE_CODES
        return (OUTCOME_SKIPPED if race else OUTCOME_FAILED), reason_line(exc)
    if result[0] == RETURNED:
        return OUTCOME_RETURNED, result[1]
    return (
        OUTCOME_ESCALATED if result[0] == ESCALATED_TO_HUMAN else OUTCOME_SKIPPED
    ), result[1]


async def _finish(
    db: aiosqlite.Connection,
    item: dict,
    stamp: str,
    outcome: str,
    detail: str,
) -> bool:
    """Исход условен той же меткой; сбой называется вслух одной строкой в карточке."""
    final = await db.execute(
        "UPDATE steward_judgements SET advisor_outcome=?, advisor_outcome_detail=? "
        "WHERE id=? AND advisor_outcome=? AND advisor_claimed_at=?",
        (outcome, detail, item["id"], _CLAIMED, stamp),
    )
    if final.rowcount == 1 and outcome == OUTCOME_FAILED:
        await repo.add_task_update(
            db,
            int(item["task_id"]),
            "hub",
            "alert",
            f"Возврат на доработку по суждению судьи не применён (сдача "
            f"{item['generation']}): {detail}. Решает человек.",
            author_kind="hub",
        )
    await db.commit()
    return final.rowcount == 1
