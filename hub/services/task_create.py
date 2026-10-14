"""Этапы ``lifecycle.create_task`` (#1692): подготовка, транзакция вставки, хвост после commit.

Вынесено без изменения поведения: у ``create_task`` не было запаса сложности, а у
``lifecycle.py`` — запаса размера. Имена, которые тесты подменяют через
``lifecycle`` (``_refuse_state_parent``, ``_project_of_new_task``,
``log_activity``, ``dispatch_task``), здесь берутся из модуля ``lifecycle`` в
момент вызова, а не копируются.

Инварианты, которые нельзя ослаблять:

* до write-лока (``prepare_create``) — только проверки и вычисления: отказ здесь
  не оставляет ни строки, ни брони, ни job;
* ``insert_in_transaction`` — единственный владелец транзакции: ``BEGIN
  IMMEDIATE`` (если её не открыл вызывающий), повторная проверка state-родителя
  под локом, поиск ключа идемпотентности, допуск заморозки и вставка — одна
  транзакция; три пути rollback (``HTTPException``, ``IntegrityError`` с
  гонкой за ключ, любое иное исключение) оставлены как были;
* replay (ключ уже известен, хеш тот же) возвращает ``is_new=False`` и не
  повторяет ни dispatch, ни ``task_created``: они идут только из
  ``finish_create`` и только после commit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import db as db_module
from hub import repository as repo
from hub.models import TaskCreate, TaskSource, TaskType, TaskView
from hub.services import lifecycle as lc
from hub.services import review_limit
from hub.services.project_policy import risk_map_for_task
from hub.services.result_kind import is_state
from hub.services.risk_class import derive_risk_class
from hub.services.task_idempotency import (
    IdempotencyRecord,
    hash_task_create_payload,
    idempotency_conflict_detail,
    normalize_task_create,
    resolve_client_request_id,
)


@dataclass(frozen=True)
class CreatePlan:
    """Всё, что решено до write-лока и нужно транзакции вставки и хвосту."""

    normalized: TaskCreate
    idem_key: str | None
    request_hash: str | None
    bound_project: Any
    initial_status: str
    run_check: review_limit.ReviewQueueCheck | None
    run_held: bool

    @property
    def project_id(self) -> int | None:
        return self.bound_project["id"] if self.bound_project else None


async def _load_task_view(db: aiosqlite.Connection, task_id: int) -> TaskView:
    row = await repo.get_task(db, task_id)
    return lc.row_to_task(row)  # type: ignore[arg-type]


async def _refuse_bad_shape(db: aiosqlite.Connection, body: TaskCreate) -> None:
    """Иерархия, дверь state-задачи и state-родитель — до вставки (первая проверка)."""
    err = await db_module.validate_hierarchy(db, body.task_type.value, body.parent_id)
    if err:
        raise HTTPException(
            400,
            detail=lc.hierarchy_error_detail(
                err,
                task_type=body.task_type.value,
                parent_id=body.parent_id,
            ),
        )
    if is_state(body) and body.run_immediately:
        # #1647: до вставки строки — отказ не оставляет задачи, брони и job.
        from hub.services.state_task import refusal_for_door

        raise refusal_for_door("run_immediately")
    await lc._refuse_state_parent(db, body.parent_id)


async def _epic_project_at_creation(db: aiosqlite.Connection, body: TaskCreate) -> Any:
    """Строка проекта, к которому привязывается создаваемый эпик (#346); иначе None."""
    if not body.project:
        return None
    if body.task_type != TaskType.epic:
        raise HTTPException(
            422, "project can only be set on epics; children inherit it"
        )
    project_row = await repo.get_project_by_slug(db, body.project)
    if project_row is None:
        raise HTTPException(422, f"unknown project slug: {body.project!r}")
    if project_row["archived"] or project_row["status"] != "active":
        raise HTTPException(
            422,
            f"project {body.project!r} is not active "
            "(pending proposals and archived projects cannot take epics)",
        )
    return project_row


def _wants_run(normalized: TaskCreate) -> bool:
    return normalized.run_immediately and normalized.source != TaskSource.agent


async def _plan_created_run(
    db: aiosqlite.Connection, normalized: TaskCreate
) -> tuple[review_limit.ReviewQueueCheck | None, bool]:
    """Decide the run of create(run_immediately) BEFORE the row exists (#1264).

    A held run inserts the row as open straight away — never running without
    a job, not even between two commits. Only the initial status changes, not
    ``normalized``: the idempotency hash must not depend on the queue.
    """
    if not _wants_run(normalized):
        return None, False
    check = await review_limit.check_review_queue_for_new_task(
        db, normalized.parent_id, normalized.class_of_service
    )
    return check, check is not None and check.outcome == review_limit.HELD


async def prepare_create(
    db: aiosqlite.Connection,
    body: TaskCreate,
    client_request_id: str | None,
    force_draft: bool,
) -> CreatePlan:
    """Проверки и вычисления до write-лока; ничего не пишет."""
    idem_key = resolve_client_request_id(
        None, client_request_id or body.client_request_id
    )
    await _refuse_bad_shape(db, body)

    # Bind an epic to a project at creation (#346). Only epics carry
    # project_id — children resolve it by walking up to the root epic.
    bound_project = await _epic_project_at_creation(db, body)

    initial_status, normalized = normalize_task_create(body)
    if force_draft:
        initial_status = "draft"
    request_hash = hash_task_create_payload(normalized) if idem_key else None
    run_check, run_held = await _plan_created_run(db, normalized)
    if run_held:
        initial_status = "open"
    return CreatePlan(
        normalized=normalized,
        idem_key=idem_key,
        request_hash=request_hash,
        bound_project=bound_project,
        initial_status=initial_status,
        run_check=run_check,
        run_held=run_held,
    )


async def _replay_known_key(
    db: aiosqlite.Connection, plan: CreatePlan
) -> lc.CreateTaskOutcome | None:
    """Ключ уже известен: тот же хеш — вернуть прежнюю задачу, иной — 409.

    Вызывается под write-локом. Replay закрывает транзакцию и возвращает
    ``is_new=False``; dispatch и ``task_created`` он не повторяет.
    """
    if not plan.idem_key:
        return None
    existing = await repo.get_task_idempotency_key(db, plan.idem_key)
    if not existing:
        return None
    if existing["request_hash"] != plan.request_hash:
        record = IdempotencyRecord(
            client_request_id=plan.idem_key,
            task_id=int(existing["task_id"]),
            request_hash=existing["request_hash"],
        )
        raise HTTPException(
            409,
            idempotency_conflict_detail(record),
        )
    await db.commit()
    task = await _load_task_view(db, int(existing["task_id"]))
    return lc.CreateTaskOutcome(task=task, is_new=False)


async def _insert_new_task(db: aiosqlite.Connection, plan: CreatePlan) -> int:
    """Допуск заморозки и вставка строки (без commit): под write-локом."""
    normalized = plan.normalized
    # #1594: уже одобренная работа входит в проект только через допуск
    # заморозки. После поиска по ключу: повтор успешного создания вернул
    # бы существующую задачу выше, и заморозка к нему не применяется;
    # проверяется только новая вставка. Черновик (в т.ч. от агента) не
    # ограничивается.
    if plan.initial_status != "draft":
        lc._require_freeze_admission(
            await lc._project_of_new_task(db, normalized.parent_id, plan.bound_project),
            normalized.work_type,
            normalized.freeze_rationale,
        )

    # Structured-aware insert so all fields from TaskCreate (work_type,
    # scope_in/out, user_story, etc.) persist (#46). ``normalized`` carries
    # the lifecycle normalizations that also feed the idempotency hash.
    task_id = await repo.create_task_full(db, normalized, status=plan.initial_status)
    if plan.project_id is not None:
        await repo.update_task(db, task_id, project_id=plan.project_id)

    # Shadow risk class (#582): derived from the declared areas, never
    # from anything the author says about risk. No declared areas → the
    # class honestly stays "not computed" (NULL from the migration).
    risk, risk_reasons = derive_risk_class(
        normalized.affected_areas, await risk_map_for_task(db, task_id)
    )
    if risk is not None:
        await repo.update_task(
            db,
            task_id,
            risk_class=risk.value,
            risk_class_reasons=db_module.serialize_str_list(risk_reasons),
        )

    if plan.idem_key and plan.request_hash is not None:
        await repo.insert_task_idempotency_key(
            db,
            client_request_id=plan.idem_key,
            task_id=task_id,
            request_hash=plan.request_hash,
        )
    return task_id


async def _replay_after_conflict(
    db: aiosqlite.Connection, idem_key: str | None, request_hash: str | None
) -> lc.CreateTaskOutcome:
    """Гонка за ключ идемпотентности: вернуть победителя или 409 (прежняя логика).

    Без ключа или без записи по нему исходная IntegrityError пробрасывается.
    Вызывается только из ``except``-блока: голый ``raise`` берёт её оттуда.
    """
    if not idem_key:
        raise
    existing = await repo.get_task_idempotency_key(db, idem_key)
    if not existing:
        raise
    if existing["request_hash"] != request_hash:
        record = IdempotencyRecord(
            client_request_id=idem_key,
            task_id=int(existing["task_id"]),
            request_hash=existing["request_hash"],
        )
        raise HTTPException(409, idempotency_conflict_detail(record)) from None
    task = await _load_task_view(db, int(existing["task_id"]))
    return lc.CreateTaskOutcome(task=task, is_new=False)


async def insert_in_transaction(
    db: aiosqlite.Connection, body: TaskCreate, plan: CreatePlan
) -> int | lc.CreateTaskOutcome:
    """Транзакция вставки: id новой задачи или готовый исход replay.

    Единственное место с BEGIN/commit/rollback создания.
    """
    owns_tx = not db.in_transaction
    try:
        # #1594: write-лок берётся ДО чтения политики проекта. Допуск заморозки
        # и вставка - одна транзакция: правка политики между ними иначе
        # пропускала бы работу, запрещённую уже действующей заморозкой.
        if owns_tx:
            await db.execute("BEGIN IMMEDIATE")
        # #1647: тот же вопрос ещё раз — под замком, которым защищена вставка:
        # конкурентный refine мог перевести листовой draft в state после первой
        # проверки. Refine читает детей под своей транзакцией, значит из двух
        # операций выигрывает одна.
        await lc._refuse_state_parent(db, body.parent_id)
        replay = await _replay_known_key(db, plan)
        if replay is not None:
            return replay
        task_id = await _insert_new_task(db, plan)
        await db.commit()
        return task_id
    except HTTPException:
        if owns_tx:
            await db.rollback()
        raise
    except aiosqlite.IntegrityError:
        await db.rollback()
        return await _replay_after_conflict(db, plan.idem_key, plan.request_hash)
    except BaseException:
        # Любое иное исключение (ValueError, OperationalError, отмена) не должно
        # оставлять BEGIN IMMEDIATE открытым и write-лок занятым (#1594).
        if owns_tx and db.in_transaction:
            await db.rollback()
        raise


async def _run_created_task(
    db: aiosqlite.Connection,
    task_id: int,
    check: review_limit.ReviewQueueCheck | None,
    held: bool,
) -> dict[str, Any]:
    """Card note for the limit's outcome, then the dispatch it allowed."""
    await review_limit.note_run_check(db, task_id, check, "создана")
    if held:
        return {}
    row = await repo.get_task(db, task_id)
    return await lc.dispatch_task(db, task_id, dict(row))  # type: ignore[arg-type]


async def finish_create(
    db: aiosqlite.Connection, plan: CreatePlan, task_id: int
) -> lc.CreateTaskOutcome:
    """После commit: dispatch, событие ``task_created``, итоговая карточка."""
    normalized = plan.normalized
    result: dict[str, Any] = {}
    if _wants_run(normalized):
        result = await _run_created_task(db, task_id, plan.run_check, plan.run_held)

    await lc.log_activity(
        db,
        "task_created",
        f"{normalized.task_type.value.capitalize()} #{task_id}: {normalized.title}",
        json.dumps(result, ensure_ascii=False) if result else None,
    )

    task = await _load_task_view(db, task_id)
    return lc.CreateTaskOutcome(task=task, is_new=True)
