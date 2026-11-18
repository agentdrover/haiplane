"""Narrow HTTP doors for role outbox acknowledgements and human replies."""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from hub.agent_outbox_models import (
    MAX_SQL_INT,
    OutboxAck,
    OutboxAnswer,
    OutboxPublish,
    OutboxScope,
    OutboxStatus,
)
from hub.auth import current_identity
from hub.executor_host_api import _db, _host_caller, _human
from hub.services import agent_outbox


class _SafeRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(
                    422, detail={"reason": "outbox_invalid_request"}
                ) from None

        return handle


ResourceID = Annotated[int, Path(gt=0, le=MAX_SQL_INT)]
router = APIRouter(route_class=_SafeRoute)
HOST_OUTBOX = "/api/executor-hosts/{name}/role-orders/{order_id}/outbox"


@router.get(HOST_OUTBOX)
async def scope(name: str, order_id: ResourceID, request: Request) -> OutboxScope:
    principal = await _principal(request, name)
    db = _db(request)
    await agent_outbox.check_host(db, name, principal)
    order = await agent_outbox.live_order(db, order_id, name)
    if order is None or not order["admitted"]:
        agent_outbox.refuse(404, "outbox_not_found")
    return OutboxScope(
        project=order["project_slug"],
        role=order["role"],
        task_id=order["task_id"],
        generation=order["generation"],
        run=order["attempt"],
    )


@router.post(HOST_OUTBOX)
async def publish(
    name: str,
    order_id: ResourceID,
    body: OutboxPublish,
    request: Request,
) -> OutboxAck:
    principal = await _principal(request, name)
    return await agent_outbox.publish(_db(request), name, principal, order_id, body)


@router.get(HOST_OUTBOX + "/{delivery_id}")
async def status(
    name: str, order_id: ResourceID, delivery_id: ResourceID, request: Request
) -> OutboxStatus:
    principal = await _principal(request, name)
    db = getattr(request.state, "db", None)
    if db is None:
        agent_outbox.refuse(503, "outbox_connection_unavailable")
    return await agent_outbox.get_status(db, name, principal, order_id, delivery_id)


@router.post("/api/tasks/{task_id}/outbox-questions/{message_id}/answer")
async def answer(
    task_id: ResourceID, message_id: ResourceID, body: OutboxAnswer, request: Request
) -> dict:
    return await _answer(request, task_id, message_id, body.body)


async def _answer(
    request: Request, task_id: ResourceID, message_id: ResourceID, body: str
) -> dict:
    identity = await _human(request, "role question answer")
    if identity.principal_id is None:
        agent_outbox.refuse(403, "outbox_owner_required")
    return await agent_outbox.answer(
        _db(request),
        task_id,
        message_id,
        identity.principal_id,
        identity.username,
        body,
    )


async def _principal(request: Request, name: str) -> int:
    await _host_caller(request, name)
    principal = current_identity(request).principal_id
    if principal is None:
        agent_outbox.refuse(403, "outbox_host_not_allowed")
    return principal
