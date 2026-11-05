"""REST projection of operation history and create response snapshots."""

import json
import aiosqlite
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from hub import config, idempotency as idem, services
from hub import repository_idempotency as repo
from hub.auth import current_identity
from hub.db import write_transaction
from hub.services.operation_access import require_scope
from hub.services.task_create_operations import expired

router = APIRouter()


def refuse_operation_headers(request: Request) -> None:
    if (
        request.headers.get("Idempotency-Key", "").strip()
        or request.headers.get("X-Operation-Id", "").strip()
    ):
        raise idem.error(400, "operation_not_idempotent")


async def create_task_response(
    body: Any, request: Request, response: Response, db: Any, enforce_audit_key: Any
) -> Any:
    alias = idem.resolve_alias(
        request.headers.get("X-Client-Request-Id"), body.client_request_id
    )
    if not config.IDEMPOTENCY_ENABLED:
        refuse_operation_headers(request)
    operation_id = idem.resolve_operation_id(request.headers)
    identity = current_identity(request)
    kwargs: dict[str, Any] = dict(
        client_request_id=alias,
        operation_id=operation_id,
        principal_ref=idem.principal_ref(identity),
        actor_ref=identity.username,
        identity=identity,
    )
    if identity.is_ci_auditor:
        try:
            async with write_transaction(db):
                await enforce_audit_key(request, body, alias)
                outcome = await services.create_task(db, body, **kwargs)
        except aiosqlite.OperationalError as exc:
            keyed = config.IDEMPOTENCY_ENABLED and (
                operation_id or alias or config.IDEMPOTENCY_SERVER_ID
            )
            refusal = idem.database_error(exc) if keyed else None
            if refusal is not None:
                raise refusal from None
            raise
    else:
        outcome = await services.create_task(db, body, **kwargs)
    if outcome.response_body is not None:
        return Response(
            content=outcome.response_body,
            media_type="application/json",
            status_code=outcome.response_status,
            headers=outcome.response_headers,
        )
    if alias:
        response.status_code = 201 if outcome.is_new else 200
    if outcome.legacy_replay:
        response.headers["X-Idempotency-Legacy"] = "true"
    return outcome.task


@router.get("/api/operations/{operation_id}")
async def operation_status(
    operation_id: str, request: Request, include: str = ""
) -> Response:
    operation_id = idem.validate_operation_id(operation_id) or operation_id
    identity = current_identity(request)
    db = getattr(request.state, "db", None) or request.app.state.db
    row = await repo.get_key(db, idem.principal_ref(identity), operation_id)
    if row is None:
        raise idem.error(404, "operation_not_found")
    saved_result = json.loads(row["response_body"]) if row["response_body"] else None
    await require_scope(
        db,
        identity,
        row["scope_ref"],
        source=saved_result.get("source") if saved_result else None,
    )
    is_expired = await expired(db, row)
    effects = await repo.list_effects(db, row["principal_ref"], operation_id)
    body = dict(
        operation_id=operation_id,
        operation=row["operation"],
        status="expired" if is_expired else "completed",
        response_status=None if is_expired else row["response_status"],
        effects=[_effect_view(effect) for effect in effects],
    )
    if include == "result" and not is_expired:
        body["result"] = saved_result
    return Response(
        json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


def _effect_view(effect: Any) -> dict[str, Any]:
    return {
        name: effect[name]
        for name in (
            "effect_id",
            "effect_kind",
            "state",
            "attempts",
            "next_at",
            "lease_until",
            "last_error",
            "resolution",
            "resolved_at",
            "resolved_by",
        )
    }


async def operation_admission(request: Request, call_next: Any) -> Response:
    if (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and request.url.path != "/api/tasks"
    ):
        try:
            refuse_operation_headers(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return await call_next(request)
