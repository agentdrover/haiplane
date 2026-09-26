"""Ревью покупается только на зелёном CI закреплённого коммита (#1405).

Кросс-модельный прогон стоит 1.5–2.2 млн provider-токенов, и на сдаче, у
которой CI уже назвал упавшую проверку, он стоит столько же, сколько на
готовой, — а автор всё равно пересдаёт (docs/agent-context/review-spend.md).
Детерминированное — до модели: пока CI не сказал своё, ревьюер не покупается.

Решение принимается по отчёту CI о ЗАКРЕПЛЁННОМ коммите сдачи (#546, #572):

- отчёт есть и что-то названо ``fail`` (проверка из ``checks`` или
  ``validation_status``) — красный: прогон не покупается, в карточке одно
  событие на сдачу с именами упавших проверок. Сдача принята; после зелёной
  пересдачи ревью заказывается как обычно;
- отчёт есть и ``fail`` в нём нет — заказ. Сюда же ``unknown`` и
  ``skipped``: это слова о прогоне, а не о коде (ci_report.py), и отказ в
  ревью по ним значил бы прочитать отсутствие свидетельства как провал;
- отчёта нет, а CI у проекта хаб видит, — CI ещё идёт: заказ ЖДЁТ, не
  покупается и не отменяется. Его ставит приём отчёта
  (``order_after_ci_report``), а если отчёт не пришёл за
  ``REVIEW_CI_WAIT_MINUTES`` — тик поллера пишет одно событие «ревью ждёт CI,
  CI нет» с причиной и заказывает ревью без CI: сломанный путь отчёта не
  должен оставить сдачу без второго читателя навсегда;
- отчёта нет, и хаб не видел ни одного отчёта CI ни по одной задаче проекта
  (GitVerse-проекты, проект без workflow), — ревью как до этой задачи;
- ``machine_review_override=require`` — ручной запрос человека: условие его
  не останавливает.

Отчёт о ДРУГОМ коммите не считается отчётом о закреплённом. Каждое событие
пишется один раз на сдачу — по метке с номером поколения в тексте, как
NO_REVIEWER_MARK (#1216): поллер проходит по карточке снова и снова (#1330).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import aiosqlite

from hub import config
from hub import repository as repo
from hub.db import fetchall
from hub.services.ci_report import CHECK_FAIL, ci_report_state
from hub.services.validation_run import FAIL as VALIDATION_FAIL

log = logging.getLogger("hub")

GREEN = "green"
RED = "red"
PENDING = "pending"
OVERDUE = "overdue"
NO_CI = "no_ci"
REQUESTED = "requested"

#: Метки событий. Номер поколения внутри — дедуп в пределах сдачи; префиксы
#: отдельно, потому что свип ищет их в SQL для всех задач разом.
WAIT_PREFIX = "[ревью ждёт CI: сдача "
RED_PREFIX = "[ревью не куплено, CI красный: сдача "
MISSING_PREFIX = "[ревью ждёт CI, CI нет: сдача "
GREEN_PREFIX = "[CI зелёный, ревью заказывается: сдача "

RED_EVENT = "review_withheld_red_ci"
MISSING_EVENT = "review_ci_missing"

#: Имя провала валидационных команд среди упавших проверок.
VALIDATION_CHECK = "validation"


def mark(prefix: str, generation: int) -> str:
    return f"{prefix}{generation}]"


@dataclass(frozen=True)
class CiStanding:
    """Что CI сказал о закреплённом коммите, в терминах заказа ревью."""

    verdict: str
    sha: str
    failed: tuple[str, ...] = ()
    validation: str = ""


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


async def project_sees_ci(db: aiosqlite.Connection, project: Any) -> bool:
    """Приходил ли хоть один отчёт CI по какой-нибудь задаче проекта.

    Проект живёт на корневом эпике (#335), поэтому от задач с отчётами идём
    вверх до первого project_id. Корень без проекта — это default, как в
    resolve_project_for_task.
    """
    row = dict(project)
    is_default = 1 if (row.get("slug") or "") == "default" else 0
    rows = await fetchall(
        db,
        "WITH RECURSIVE up(id, parent_id, project_id, depth) AS ("
        " SELECT t.id, t.parent_id, t.project_id, 0 FROM tasks t"
        " WHERE t.id IN (SELECT task_id FROM ci_run_reports)"
        " UNION ALL"
        " SELECT p.id, p.parent_id, p.project_id, up.depth + 1"
        " FROM up JOIN tasks p ON p.id = up.parent_id"
        " WHERE up.project_id IS NULL AND up.depth < 20"
        ") SELECT 1 FROM up WHERE project_id = ?"
        " OR (? = 1 AND project_id IS NULL AND parent_id IS NULL) LIMIT 1",
        (row.get("id"), is_default),
    )
    return bool(rows)


async def _waited_past_ceiling(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> bool:
    rows = await fetchall(
        db,
        "SELECT 1 FROM submissions WHERE task_id=? AND generation=? "
        "AND submitted_at <= datetime('now', ?)",
        (task_id, generation, f"-{config.REVIEW_CI_WAIT_MINUTES} minutes"),
    )
    return bool(rows)


async def ci_standing(
    db: aiosqlite.Connection, task: dict[str, Any], project: Any
) -> CiStanding:
    task_id = int(task["id"])
    sha = (task.get("submission_sha") or "").strip()
    if (task.get("machine_review_override") or "").strip() == "require":
        return CiStanding(REQUESTED, sha)
    report = await repo.get_ci_run_report(db, task_id, sha)
    if report is not None:
        stored = dict(report)
        failed = failed_checks(stored)
        return CiStanding(
            RED if failed else GREEN,
            sha,
            tuple(failed),
            (stored.get("validation_status") or "").strip(),
        )
    if not await project_sees_ci(db, project):
        return CiStanding(NO_CI, sha)
    generation = int(task.get("submission_generation") or 0)
    if await _waited_past_ceiling(db, task_id, generation):
        return CiStanding(OVERDUE, sha)
    return CiStanding(PENDING, sha)


async def _marked(db: aiosqlite.Connection, task_id: int, text: str) -> bool:
    rows = await fetchall(
        db,
        "SELECT 1 FROM task_updates WHERE task_id=? AND content LIKE ? LIMIT 1",
        (task_id, f"%{text}%"),
    )
    return bool(rows)


async def _say_once(
    db: aiosqlite.Connection,
    task_id: int,
    tag: str,
    kind: str,
    text: str,
    event: tuple[str, dict[str, Any]] | None = None,
) -> None:
    if await _marked(db, task_id, tag):
        return
    await repo.add_task_update(db, task_id, "hub", kind, f"{tag} {text}")
    if event is not None:
        await repo.insert_event(
            db, kind=event[0], task_id=task_id, actor="policy", payload=event[1]
        )
    await db.commit()


async def review_may_be_bought(
    db: aiosqlite.Connection, task: dict[str, Any], project: Any
) -> bool:
    """Общее раннее условие заказа: сдача, лестница, каскад, переспрос."""
    standing = await ci_standing(db, task, project)
    task_id = int(task["id"])
    generation = int(task.get("submission_generation") or 0)
    sha12 = standing.sha[:12]
    if standing.verdict == RED:
        names = ", ".join(standing.failed)
        await _say_once(
            db,
            task_id,
            mark(RED_PREFIX, generation),
            "alert",
            f"Ревью не куплено: CI красный на закреплённом коммите {sha12} — "
            f"упали: {names}. Сдача принята, но прогон ревью на ней не "
            "покупается: исправьте и пересдайте — на зелёном CI ревью "
            "закажется как обычно. Запросить ревью вопреки этому может "
            "человек (machine_review_override=require) (#1405).",
            (
                RED_EVENT,
                {
                    "generation": generation,
                    "sha": standing.sha,
                    "failed": list(standing.failed),
                },
            ),
        )
        return False
    if standing.verdict == PENDING:
        await _say_once(
            db,
            task_id,
            mark(WAIT_PREFIX, generation),
            "status",
            f"Отчёта CI о закреплённом коммите {sha12} ещё нет, а CI у проекта "
            "хаб видит: прогон ревью не покупается, пока CI не отчитается. "
            "Зелёный отчёт закажет ревью сразу, красный — нет. Потолок "
            f"ожидания {config.REVIEW_CI_WAIT_MINUTES} мин "
            "(REVIEW_CI_WAIT_MINUTES): после него хаб назовёт это и закажет "
            "ревью без CI (#1405).",
        )
        return False
    if standing.verdict == OVERDUE:
        _, reason = await ci_report_state(db, task)
        await _say_once(
            db,
            task_id,
            mark(MISSING_PREFIX, generation),
            "alert",
            f"За {config.REVIEW_CI_WAIT_MINUTES} мин CI не дал отчёта о "
            f"закреплённом коммите {sha12}: {reason}. Ревью заказывается без "
            "CI, чтобы сдача не стояла без второго читателя; если так часто — "
            "сломан путь отчёта CI (hub-ci-report) (#1405).",
            (
                MISSING_EVENT,
                {
                    "generation": generation,
                    "sha": standing.sha,
                    "waited_minutes": config.REVIEW_CI_WAIT_MINUTES,
                    "reason": reason,
                },
            ),
        )
    elif standing.verdict == GREEN and await _deferred(db, task_id, generation):
        await _say_once(
            db,
            task_id,
            mark(GREEN_PREFIX, generation),
            "status",
            f"CI отчитался о закреплённом коммите {sha12} "
            f"(validation={standing.validation or 'не названа'}): упавших "
            "проверок нет, отложенный заказ ревью ставится (#1405).",
        )
    return True


async def _deferred(db: aiosqlite.Connection, task_id: int, generation: int) -> bool:
    """Заказ этой сдачи был отложен: ждал отчёта или стоял на красном."""
    return await _marked(db, task_id, mark(WAIT_PREFIX, generation)) or (
        await _marked(db, task_id, mark(RED_PREFIX, generation))
    )


async def _settled(db: aiosqlite.Connection, task_id: int, generation: int) -> bool:
    """Отложенный заказ уже снят: зелёным отчётом или потолком ожидания."""
    return await _marked(db, task_id, mark(GREEN_PREFIX, generation)) or (
        await _marked(db, task_id, mark(MISSING_PREFIX, generation))
    )


async def order_after_ci_report(db: aiosqlite.Connection, task_id: int) -> bool:
    """Поставить отложенный заказ, когда CI отчитался о закреплённом коммите.

    Только для сдачи, заказ которой был отложен и ещё не снят: у остальных
    ревью уже заказано при сдаче или не заказывается по своей причине, и
    второй вход в диспетчер им ничего не даст. Двойной заказ при гонке сдачи
    и приёма отчёта держит бронь #1399 внутри maybe_dispatch_review.
    """
    row = await repo.get_task(db, task_id)
    if row is None:
        return False
    task = dict(row)
    generation = int(task.get("submission_generation") or 0)
    if task.get("status") != "review" or task.get("review_job_id") or generation <= 0:
        return False
    if not await _deferred(db, task_id, generation) or await _settled(
        db, task_id, generation
    ):
        return False
    from hub.services.review_dispatch import maybe_dispatch_review

    return await maybe_dispatch_review(db, task_id)


async def order_reviews_waiting_for_ci(db: aiosqlite.Connection) -> int:
    """Тик поллера: отложенные заказы, дождавшиеся отчёта или потолка.

    Выбираются только сдачи с меткой ожидания и без снявшей её метки: сдача
    на красном или уже заказанная сюда не попадает, и тик по ней ничего не
    пишет. Отчёт, чей приём не довёл заказ до конца, тоже подбирается здесь.
    """
    rows = await fetchall(
        db,
        "SELECT t.id FROM tasks t JOIN submissions s "
        "ON s.task_id = t.id AND s.generation = t.submission_generation "
        "WHERE t.status = 'review' AND t.review_job_id IS NULL "
        "AND t.submission_generation > 0 "
        "AND EXISTS (SELECT 1 FROM task_updates u WHERE u.task_id = t.id "
        "  AND u.content LIKE '%' || ? || t.submission_generation || ']%') "
        "AND NOT EXISTS (SELECT 1 FROM task_updates u WHERE u.task_id = t.id "
        "  AND (u.content LIKE '%' || ? || t.submission_generation || ']%' "
        "    OR u.content LIKE '%' || ? || t.submission_generation || ']%' "
        "    OR u.content LIKE '%' || ? || t.submission_generation || ']%')) "
        "AND NOT EXISTS (SELECT 1 FROM review_dispatches d WHERE d.task_id = t.id "
        "  AND d.submission_generation = t.submission_generation) "
        "AND (s.submitted_at <= datetime('now', ?) OR EXISTS ("
        "  SELECT 1 FROM ci_run_reports c WHERE c.task_id = t.id "
        "  AND c.head_sha = t.submission_sha))",
        (
            WAIT_PREFIX,
            RED_PREFIX,
            GREEN_PREFIX,
            MISSING_PREFIX,
            f"-{config.REVIEW_CI_WAIT_MINUTES} minutes",
        ),
    )
    ordered = 0
    for row in rows:
        task_id = int(row["id"])
        try:
            ordered += int(await order_after_ci_report(db, task_id))
        except Exception:  # noqa: BLE001 - one task must not stop the sweep
            log.exception("deferred review order failed for task #%s", task_id)
    return ordered
