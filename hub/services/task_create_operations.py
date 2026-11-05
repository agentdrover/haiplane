"""T11 create orchestration: key, row, activity, snapshot and outbox commit once."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import config, idempotency as idem, repository as tasks
from hub import repository_idempotency as repo
from hub.db import write_transaction
from hub.models import TaskView
from hub.services import lifecycle as lc, task_create as create_steps
from hub.services import effects, review_limit
from hub.services.operation_access import require_scope, task_scope
from hub.services.task_idempotency import (
    hash_task_create_payload,
    normalize_task_create,
)


def admitted(body: Any, alias: str | None, operation_id: str | None) -> bool:
    if operation_id and not config.IDEMPOTENCY_ENABLED:
        raise idem.error(400, "operation_not_idempotent")
    return config.IDEMPOTENCY_ENABLED and bool(
        operation_id or alias or body.client_request_id or config.IDEMPOTENCY_SERVER_ID
    )


async def expired(db: Any, row: Any) -> bool:
    if row["status"] != "completed":
        return True
    if row["expires_at"] > idem.utc_timestamp():
        return False
    # Retain the result until an ambiguous external effect has been reconciled.
    return not await repo.has_unfinished_effects(
        db, row["principal_ref"], row["operation_id"]
    )


def _check_row(row: Any, body: Any, alias: str | None) -> None:
    if row["operation"] != idem.CREATE_OPERATION:
        raise idem.conflict("operation_mismatch", row["operation_id"])
    if row["args_hash_version"] not in (1, 2):
        raise idem.error(500, "idempotency_hash_version_unsupported")
    normalized = normalize_task_create(body)[1]
    args_hash = (
        hash_task_create_payload(normalized)
        if row["args_hash_version"] == 1
        else idem.hash_args_v2(
            idem.CREATE_OPERATION,
            normalized.model_dump(mode="json", exclude={"client_request_id"}),
        )
    )
    if row["args_hash"] != args_hash:
        snapshot = json.loads(row["response_body"])
        raise idem.conflict(
            "payload_mismatch",
            row["operation_id"],
            client_request_id=alias,
            existing_task_id=snapshot["id"],
        )


async def _legacy_replay(
    db: Any, body: Any, alias: str | None, identity: Any
) -> lc.CreateTaskOutcome | None:
    if not alias:
        return None
    row = await tasks.get_task_idempotency_key(db, alias)
    if row is None:
        return None
    if await tasks.get_task(db, int(row["task_id"])) is None:
        raise idem.error(404, "operation_not_found")
    scope = await task_scope(db, row["task_id"])
    try:
        await require_scope(db, identity, scope, source=body.source)
    except HTTPException as exc:
        if exc.status_code == 403:
            raise idem.error(404, "operation_not_found") from None
        raise
    if row["request_hash"] != hash_task_create_payload(normalize_task_create(body)[1]):
        raise idem.conflict(
            "payload_mismatch",
            None,
            client_request_id=alias,
            existing_task_id=row["task_id"],
        )
    task = await create_steps._load_task_view(db, row["task_id"])
    return lc.CreateTaskOutcome(task=task, is_new=False, legacy_replay=True)


async def replay(
    db: Any,
    body: Any,
    alias: str | None,
    operation_id: str,
    principal: str,
    identity: Any,
) -> lc.CreateTaskOutcome | None:
    row = await repo.get_key(db, principal, operation_id)
    bound = await repo.get_alias(db, principal, alias) if alias else None
    if row is not None:
        if bound and bound["operation_id"] != operation_id:
            raise idem.conflict("alias_taken", operation_id)
        if alias and not bound:
            raise idem.conflict("alias_not_bound", operation_id)
        await require_scope(db, identity, row["scope_ref"], source=body.source)
        if await expired(db, row):
            raise idem.error(410, "operation_expired")
        _check_row(row, body, alias)
        # Do not reload a live task; it may have changed or even been removed.
        task = TaskView.model_validate_json(row["response_body"])
        return lc.CreateTaskOutcome(
            task=task,
            is_new=False,
            response_body=row["response_body"],
            response_status=200,
            response_headers=idem.response_headers(operation_id, True),
        )
    if bound:
        raise idem.conflict("operation_mismatch", operation_id)
    other = await repo.get_key_any_principal(db, operation_id)
    if other is not None and other["principal_ref"] != principal:
        raise idem.error(404, "operation_not_found")
    return await _legacy_replay(db, body, alias, identity)


async def _save(
    db: Any, plan: create_steps.CreatePlan, task_id: int
) -> tuple[lc.CreateTaskOutcome, str | None]:
    if plan.operation_id is None:
        raise ValueError("operation_id_required")
    normalized = plan.normalized
    if create_steps._wants_run(normalized):
        await review_limit.note_run_check(
            db, task_id, plan.run_check, "создана", commit=False
        )
    await lc.log_activity(
        db,
        "task_created",
        f"{normalized.task_type.value.capitalize()} #{task_id}: {normalized.title}",
        commit=False,
    )
    task = await create_steps._load_task_view(db, task_id)
    try:
        snapshot = json.dumps(
            task.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (ValueError, TypeError) as exc:
        raise idem.error(500, "idempotency_result_unserializable") from exc
    size = len(snapshot.encode("utf-8"))
    if size > config.IDEMPOTENCY_MAX_RESULT_BYTES:
        raise idem.error(500, "idempotency_result_too_large")
    await repo.insert_key(
        db,
        principal_ref=plan.principal_ref,
        operation_id=plan.operation_id,
        operation=idem.CREATE_OPERATION,
        args_hash=plan.args_hash,
        args_hash_version=plan.args_hash_version,
        scope_ref=plan.scope_ref,
        actor_ref=plan.actor_ref,
        response_status=201,
        response_body=snapshot,
        response_headers='{"Content-Type":"application/json"}',
        response_bytes=size,
        expires_at=idem.result_expires_at(),
        key_expires_at=idem.key_expires_at(),
    )
    if plan.alias:
        await repo.insert_alias(
            db, plan.principal_ref, plan.alias, plan.operation_id, idem.CREATE_OPERATION
        )
    effect_id = await _enqueue_dispatch(db, plan, task_id)
    outcome = lc.CreateTaskOutcome(
        task=task,
        response_body=snapshot,
        response_status=201,
        response_headers=idem.response_headers(plan.operation_id, False),
    )
    return outcome, effect_id


async def _enqueue_dispatch(
    db: Any, plan: create_steps.CreatePlan, task_id: int
) -> str | None:
    if not create_steps._wants_run(plan.normalized) or plan.run_held:
        return None
    if plan.operation_id is None:
        raise ValueError("operation_id_required")
    return await effects.enqueue_effect(
        db,
        operation_id=plan.operation_id,
        principal_ref=plan.principal_ref,
        effect_kind="task.dispatch",
        effect_key=f"task.dispatch:{task_id}:{plan.operation_id}",
        payload={"task_id": task_id},
        ordering_key=f"task:{task_id}",
    )


async def finish_idempotent_create(
    db: Any, outcome: lc.CreateTaskOutcome, effect_id: str | None
) -> lc.CreateTaskOutcome:
    if effect_id:
        await effects.run_dispatch_effect(db, effect_id)
        return replace(
            outcome, task=await create_steps._load_task_view(db, outcome.task.id)
        )
    return outcome


async def _insert(
    db: Any,
    body: Any,
    alias: str | None,
    force_draft: bool,
    operation_id: str | None,
    principal: str,
    actor: str | None,
    identity: Any,
) -> tuple[lc.CreateTaskOutcome, str | None]:
    if not operation_id:
        bound = await repo.get_alias(db, principal, alias) if alias else None
        operation_id = (
            bound["operation_id"] if bound else idem.new_server_operation_id()
        )
    plan = await create_steps.prepare_create(
        db,
        body,
        alias,
        force_draft,
        operation_id=operation_id,
        principal_ref=principal,
        actor_ref=actor,
        identity=identity,
    )
    if isinstance(plan, lc.CreateTaskOutcome):
        return plan, None
    # Re-check the state parent under the same lock as the actual insertion.
    await lc._refuse_state_parent(db, body.parent_id)
    task_id = await create_steps._insert_new_task(db, plan)
    plan = replace(plan, scope_ref=await task_scope(db, task_id))
    await require_scope(db, identity, plan.scope_ref, source=body.source)
    return await _save(db, plan, task_id)


async def create(
    db: Any,
    body: Any,
    alias: str | None,
    force_draft: bool,
    operation_id: str | None,
    principal: str,
    actor: str | None,
    identity: Any,
) -> lc.CreateTaskOutcome:
    alias = idem.validate_alias(alias or body.client_request_id)
    operation_id = idem.validate_operation_id(operation_id or "")
    if db.in_transaction:
        # The audit entrance may already own the write lock. It must not run
        # effects before committing; the poller will dispatch the outbox row.
        outcome, _ = await _insert(
            db, body, alias, force_draft, operation_id, principal, actor, identity
        )
        return outcome
    try:
        async with write_transaction(db):
            outcome, effect_id = await _insert(
                db, body, alias, force_draft, operation_id, principal, actor, identity
            )
    except aiosqlite.IntegrityError:
        winner = await _read_winner(db, body, alias, operation_id, principal, identity)
        if winner is None:
            raise
        return winner
    except aiosqlite.OperationalError as exc:
        refusal = idem.database_error(exc)
        if refusal is not None:
            raise refusal from None
        raise
    return await finish_idempotent_create(db, outcome, effect_id)


async def _winner(
    db: Any,
    body: Any,
    alias: str | None,
    operation_id: str | None,
    principal: str,
    identity: Any,
) -> lc.CreateTaskOutcome | None:
    if not operation_id and alias:
        bound = await repo.get_alias(db, principal, alias)
        operation_id = bound["operation_id"] if bound else None
    if operation_id:
        return await replay(db, body, alias, operation_id, principal, identity)
    return await _legacy_replay(db, body, alias, identity)


async def _read_winner(
    db: Any,
    body: Any,
    alias: str | None,
    operation_id: str | None,
    principal: str,
    identity: Any,
) -> lc.CreateTaskOutcome | None:
    try:
        async with write_transaction(db):
            return await _winner(db, body, alias, operation_id, principal, identity)
    except aiosqlite.OperationalError as exc:
        refusal = idem.database_error(exc)
        if refusal is not None:
            raise refusal from None
        raise
