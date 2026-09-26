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

Отчёт о ДРУГОМ коммите не считается отчётом о закреплённом.

Состояние отложенного заказа хранится СТРУКТУРНО — в таблице events, с номером
поколения в payload (находка c6073282c3ad0636), а не метками в тексте ленты:
текст ленты пишут все, и чужая запись с похожими словами глушила бы покупку.
events пишет только код хаба. Запись в карточке остаётся — для человека, но
решение по ней не принимается. Каждое событие — один раз на сдачу: поллер
проходит по задаче снова и снова (#1330).

Отложенный заказ снимается только ЗАКАЗОМ — строкой review_dispatches или
отчётом этой сдачи (находка 51b3163a19adc1cf). Вызов, который не встал
(нет настройки, сбой подготовки), оставляет заказ в ожидании, и свип
повторяет его — не чаще ORDER_RETRY_PAUSE_MINUTES и не больше ORDER_ATTEMPTS
раз, после чего одно событие отдаёт решение человеку.
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

WAIT_EVENT = "review_ci_wait"
RED_EVENT = "review_withheld_red_ci"
MISSING_EVENT = "review_ci_missing"
GREEN_EVENT = "review_ci_green"
ATTEMPT_EVENT = "review_ci_order_attempt"
EXHAUSTED_EVENT = "review_ci_order_exhausted"
#: Заказ сдачи отложен: ждал отчёта или стоял на красном.
DEFERRED_EVENTS = (WAIT_EVENT, RED_EVENT)

#: Потолок и пауза повторов отложенного заказа, который не встал (#1405).
#: Образец — переспрос #1242: два повтора сверх первого, пауза 10 минут.
ORDER_ATTEMPTS = 3
ORDER_RETRY_PAUSE_MINUTES = 10

#: Имя провала валидационных команд среди упавших проверок.
VALIDATION_CHECK = "validation"

_OF_GENERATION = "json_extract(payload, '$.generation') = ?"


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


async def _events_of(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    kinds: tuple[str, ...],
    within_minutes: int | None = None,
) -> int:
    """Сколько событий этих видов у сдачи — по полю поколения, не по тексту."""
    marks = ", ".join("?" for _ in kinds)
    sql = (
        # Вставляются только плейсхолдеры и константа; значения — параметры.
        f"SELECT COUNT(*) AS n FROM events WHERE task_id=? AND kind IN ({marks}) "  # nosec B608
        f"AND {_OF_GENERATION}"
    )
    params: list[Any] = [task_id, *kinds, generation]
    if within_minutes is not None:
        sql += " AND created_at > datetime('now', ?)"
        params.append(f"-{within_minutes} minutes")
    rows = await fetchall(db, sql, tuple(params))
    return int(rows[0]["n"]) if rows else 0


async def _say_once(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    event: tuple[str, dict[str, Any]],
    update: tuple[str, str],
) -> None:
    """Событие и запись в карточке — один раз на сдачу, по событию."""
    kind, payload = event
    if await _events_of(db, task_id, generation, (kind,)):
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
    standing = await ci_standing(db, task, project)
    task_id = int(task["id"])
    generation = int(task.get("submission_generation") or 0)
    sha12 = standing.sha[:12]
    if standing.verdict == RED:
        await _say_once(
            db,
            task_id,
            generation,
            (RED_EVENT, {"sha": standing.sha, "failed": list(standing.failed)}),
            (
                "alert",
                f"Ревью не куплено: CI красный на закреплённом коммите {sha12} "
                f"(сдача {generation}) — упали: {', '.join(standing.failed)}. "
                "Сдача принята, но прогон ревью на ней не покупается: "
                "исправьте и пересдайте — на зелёном CI ревью закажется как "
                "обычно. Запросить ревью вопреки этому может человек "
                "(machine_review_override=require) (#1405).",
            ),
        )
        return False
    if standing.verdict == PENDING:
        await _say_once(
            db,
            task_id,
            generation,
            (WAIT_EVENT, {"sha": standing.sha}),
            (
                "status",
                f"Событие «ревью ждёт CI» (сдача {generation}): отчёта о закреплённом "
                f"коммите {sha12} ещё не пришло, а CI у проекта хаб видит — "
                "прогон ревью не покупается, пока CI не отчитается. Зелёный "
                "отчёт закажет ревью сразу, красный — нет. Потолок ожидания "
                f"{config.REVIEW_CI_WAIT_MINUTES} мин (REVIEW_CI_WAIT_MINUTES): "
                "после него хаб назовёт это и закажет ревью без CI (#1405).",
            ),
        )
        return False
    if standing.verdict == OVERDUE:
        _, reason = await ci_report_state(db, task)
        await _say_once(
            db,
            task_id,
            generation,
            (
                MISSING_EVENT,
                {
                    "sha": standing.sha,
                    "waited_minutes": config.REVIEW_CI_WAIT_MINUTES,
                    "reason": reason,
                },
            ),
            (
                "alert",
                f"Событие «ревью ждёт CI, CI нет» (сдача {generation}): за "
                f"{config.REVIEW_CI_WAIT_MINUTES} мин CI не дал отчёта о "
                f"закреплённом коммите {sha12}: {reason}. Ревью заказывается "
                "без CI, чтобы сдача не стояла без второго читателя; если так "
                "часто — сломан путь отчёта CI (hub-ci-report) (#1405).",
            ),
        )
    elif standing.verdict == GREEN and await _deferred(db, task_id, generation):
        await _say_once(
            db,
            task_id,
            generation,
            (GREEN_EVENT, {"sha": standing.sha, "validation": standing.validation}),
            (
                "status",
                f"CI отчитался о закреплённом коммите {sha12} (сдача "
                f"{generation}, validation={standing.validation or 'не названа'}"
                "): упавших проверок нет, отложенный заказ ревью ставится "
                "(#1405).",
            ),
        )
    return True


async def _deferred(db: aiosqlite.Connection, task_id: int, generation: int) -> bool:
    """Заказ этой сдачи был отложен: ждал отчёта или стоял на красном."""
    return bool(await _events_of(db, task_id, generation, DEFERRED_EVENTS))


async def _ordered(db: aiosqlite.Connection, task_id: int, generation: int) -> bool:
    """Заказ сдачи состоялся: строка заказа или отчёт этой сдачи.

    Только это снимает отложенный заказ (находка 51b3163a19adc1cf). Событие
    «CI зелёный» пишется ДО вызова провайдера и заказом не является.
    """
    rows = await fetchall(
        db,
        "SELECT 1 FROM review_dispatches WHERE task_id=? "
        "AND submission_generation=? LIMIT 1",
        (task_id, generation),
    )
    if rows:
        return True
    return bool(await repo.machine_reviews_of_generation(db, task_id, generation))


async def _may_try_again(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> bool:
    """Пауза между попытками. Потолок попыток держит выборка свипа; приём
    нового отчёта CI — новое свидетельство и пробует сверх него."""
    return not await _events_of(
        db, task_id, generation, (ATTEMPT_EVENT,), ORDER_RETRY_PAUSE_MINUTES
    )


async def order_after_ci_report(db: aiosqlite.Connection, task_id: int) -> bool:
    """Поставить отложенный заказ, когда CI отчитался или вышел потолок.

    Только для сдачи, заказ которой был отложен и ещё не состоялся. Двойной
    заказ при гонке сдачи и приёма отчёта держит бронь #1399 внутри
    maybe_dispatch_review.
    """
    row = await repo.get_task(db, task_id)
    if row is None:
        return False
    task = dict(row)
    generation = int(task.get("submission_generation") or 0)
    if task.get("status") != "review" or task.get("review_job_id") or generation <= 0:
        return False
    if not await _deferred(db, task_id, generation) or await _ordered(
        db, task_id, generation
    ):
        return False
    project = await repo.resolve_project_for_task(db, task_id)
    if project is None:
        return False
    if (await ci_standing(db, task, project)).verdict in (RED, PENDING):
        # Попыткой это не считается: заказывать нечего. Красный называется
        # один раз — событием, которое проверяет сам review_may_be_bought.
        await review_may_be_bought(db, task, project)
        return False
    if not await _may_try_again(db, task_id, generation):
        return False
    await repo.insert_event(
        db,
        kind=ATTEMPT_EVENT,
        task_id=task_id,
        actor="policy",
        payload={"generation": generation},
    )
    await db.commit()
    from hub.services.review_dispatch import maybe_dispatch_review

    if await maybe_dispatch_review(db, task_id):
        return True
    await _name_exhausted_order(db, task_id, generation)
    return False


async def _name_exhausted_order(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> None:
    if await _ordered(db, task_id, generation):
        return
    if await _events_of(db, task_id, generation, (ATTEMPT_EVENT,)) < ORDER_ATTEMPTS:
        return
    await _say_once(
        db,
        task_id,
        generation,
        (EXHAUSTED_EVENT, {"attempts": ORDER_ATTEMPTS}),
        (
            "alert",
            f"Отложенный до CI заказ ревью не поставлен (сдача {generation}): "
            f"{ORDER_ATTEMPTS} попытки не дали заказа, причины — в записях "
            "выше. Хаб больше не повторяет; решение за человеком (#1405).",
        ),
    )


async def order_reviews_waiting_for_ci(db: aiosqlite.Connection) -> int:
    """Тик поллера: отложенные заказы, дождавшиеся отчёта или потолка.

    Выбираются сдачи с событием отложенного заказа (ожидание ИЛИ красный —
    зелёный перепрогон того же sha, находка a9afb5ea8b295634), без
    состоявшегося заказа и с неисчерпанными попытками. Пауза между попытками
    и красный отчёт отсеиваются в order_after_ci_report без записей.
    """
    rows = await fetchall(
        db,
        "SELECT t.id FROM tasks t JOIN submissions s "
        "ON s.task_id = t.id AND s.generation = t.submission_generation "
        "WHERE t.status = 'review' AND t.review_job_id IS NULL "
        "AND t.submission_generation > 0 "
        "AND EXISTS (SELECT 1 FROM events e WHERE e.task_id = t.id "
        "  AND e.kind IN (?, ?) "
        "  AND json_extract(e.payload, '$.generation') = t.submission_generation) "
        "AND (SELECT COUNT(*) FROM events e WHERE e.task_id = t.id AND e.kind = ? "
        "  AND json_extract(e.payload, '$.generation') = t.submission_generation"
        ") < ? "
        "AND NOT EXISTS (SELECT 1 FROM review_dispatches d WHERE d.task_id = t.id "
        "  AND d.submission_generation = t.submission_generation) "
        "AND (s.submitted_at <= datetime('now', ?) OR EXISTS ("
        "  SELECT 1 FROM ci_run_reports c WHERE c.task_id = t.id "
        "  AND c.head_sha = t.submission_sha))",
        (
            *DEFERRED_EVENTS,
            ATTEMPT_EVENT,
            ORDER_ATTEMPTS,
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
