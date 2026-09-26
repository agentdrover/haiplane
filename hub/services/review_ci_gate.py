"""Прогон ревью не покупается на красном CI закреплённого коммита (#1405).

Кросс-модельный прогон стоит 1.5–2.2 млн provider-токенов, и на сдаче, у
которой CI уже назвал упавшую проверку, он стоит столько же, сколько на
готовой, — а автор всё равно пересдаёт (docs/agent-context/review-spend.md).

Решение принимается ОДИН раз — в общем раннем отказе диспетчера, который
проходят сдача, лестница (#879), вторая ось (#1243) и переспрос (#1242), — по
отчёту CI о ЗАКРЕПЛЁННОМ коммите сдачи (#546, #572), который есть на этот
момент:

- отчёт называет ``fail`` (проверка из ``checks`` или ``validation_status``) —
  прогон не покупается, в карточке одно событие на сдачу с именами упавших
  проверок и выходом: пересдача или ручной запрос ревью человеком;
- отчёт есть и ``fail`` в нём нет — заказ как раньше. ``unknown`` и
  ``skipped`` сюда же: это слова о прогоне, а не о коде (ci_report.py);
- отчёта нет — заказ сразу, как до задачи, и одна строка в ленте, что ревью
  заказано без отчёта CI;
- ``machine_review_override=require`` — ручной запрос человека: условие его
  не останавливает.

Отложенного заказа нет (упрощение владельца 26.09, вариант А): поздний отчёт
CI ревью не заказывает, ни приёмом отчёта, ни поллером. Отчёт о ДРУГОМ
коммите не считается отчётом о закреплённом.

Каждое событие пишется один раз на сдачу, а проверяется по таблице events —
вид плюс поколение в payload, а не текст ленты, который пишут все (#1330).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall
from hub.services.ci_report import CHECK_FAIL
from hub.services.validation_run import FAIL as VALIDATION_FAIL

RED_EVENT = "review_withheld_red_ci"
#: Красный отчёт пришёл после заказа: отказано добору, каскаду, переспросу.
TOPUP_RED_EVENT = "review_topup_withheld_red_ci"
NO_REPORT_EVENT = "review_ordered_without_ci"

#: Имя провала валидационных команд среди упавших проверок.
VALIDATION_CHECK = "validation"


def failed_checks(report: Mapping[str, Any]) -> list[str]:
    """Что отчёт назвал ``fail``: проверки ``checks`` и валидация, по имени."""
    try:
        checks = json.loads(report.get("checks") or "{}")
    except (TypeError, ValueError):
        checks = {}
    failed = (
        sorted(str(k) for k, v in checks.items() if v == CHECK_FAIL)
        if isinstance(checks, dict)
        else []
    )
    if (report.get("validation_status") or "").strip() == VALIDATION_FAIL:
        failed.append(VALIDATION_CHECK)
    return failed


async def _say_once(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    event: tuple[str, dict[str, Any]],
    update: tuple[str, str],
) -> None:
    """Событие и запись в карточке — один раз на сдачу, по событию."""
    kind, payload = event
    rows = await fetchall(
        db,
        "SELECT 1 FROM events WHERE task_id=? AND kind=? "
        "AND json_extract(payload, '$.generation') = ? LIMIT 1",
        (task_id, kind, generation),
    )
    if rows:
        return
    await repo.add_task_update(db, task_id, "hub", update[0], update[1])
    await repo.insert_event(
        db,
        kind=kind,
        task_id=task_id,
        actor="policy",
        payload={"generation": generation, **payload},
    )
    await db.commit()


async def review_may_be_bought(
    db: aiosqlite.Connection, task: dict[str, Any], project: Any
) -> bool:
    """Общее раннее условие заказа: сдача, лестница, каскад, переспрос."""
    if (task.get("machine_review_override") or "").strip() == "require":
        return True
    task_id = int(task["id"])
    generation = int(task.get("submission_generation") or 0)
    sha = (task.get("submission_sha") or "").strip()
    report = await repo.get_ci_run_report(db, task_id, sha)
    if report is None:
        await _say_once(
            db,
            task_id,
            generation,
            (NO_REPORT_EVENT, {"sha": sha}),
            (
                "status",
                f"Ревью заказывается без отчёта CI о закреплённом sha {sha[:12]} "
                f"(сдача {generation}): CI к моменту решения не отчитался, "
                "поэтому заказ идёт, как до #1405. Поздний отчёт заказ не "
                "отменяет и второго не ставит.",
            ),
        )
        return True
    failed = failed_checks(dict(report))
    if not failed:
        return True
    # Красный отказывает любому входу, и добору тоже (c609380e10b71078). Если
    # первый прогон уже куплен, отказано добору — и названо это так, отдельным
    # событием, а не вторым «ревью не куплено» (64ac296015b7d20d).
    if await fetchall(
        db,
        "SELECT 1 FROM review_dispatches WHERE task_id=? "
        "AND submission_generation=? LIMIT 1",
        (task_id, generation),
    ):
        await _say_once(
            db,
            task_id,
            generation,
            (TOPUP_RED_EVENT, {"sha": sha, "failed": failed}),
            (
                "alert",
                f"Добор ревью не куплен: CI красный на закреплённом коммите "
                f"{sha[:12]} (сдача {generation}) — упали: {', '.join(failed)}. "
                "Первый прогон этой сдачи уже куплен; добор, вторая ось и "
                "переспрос на красном не покупаются (#1405).",
            ),
        )
        return False
    await _say_once(
        db,
        task_id,
        generation,
        (RED_EVENT, {"sha": sha, "failed": failed}),
        (
            "alert",
            f"Ревью не куплено: CI красный на закреплённом коммите {sha[:12]} "
            f"(сдача {generation}) — упали: {', '.join(failed)}. Сдача "
            "принята, прогон ревью на ней не покупается. Выход: исправить и "
            "пересдать — на зелёном CI ревью закажется как обычно, — или "
            "ручной запрос ревью человеком (machine_review_override=require) "
            "(#1405).",
        ),
    )
    return False
