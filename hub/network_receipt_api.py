"""Physical k8 receipt pilot. Credentials and operations are fixed per role."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.responses import JSONResponse

from hub.auth import current_identity, require_admin
from hub.executor_host_api import _db, _host_caller
from hub.services import network_receipt as pilot
from hub.services.network_receipt_submission import submit_verified_publication

router = APIRouter()
HOST = "/api/executor-hosts/{name}/network-receipt/2413"
TASK = "/api/tasks/2413/network-receipt"
OrderId = Annotated[int, Path(gt=0, le=2**63 - 1)]


def result(value):
    if value is None:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    return JSONResponse(value, headers={"Cache-Control": "no-store"})


@router.post(TASK + "/issue")
async def issue(request: Request, body: pilot.StrictBody):
    db, who = _db(request), current_identity(request)
    issued = await pilot.issue(db, who)
    if issued is not None:
        await submit_verified_publication(db, who, issued["order_id"])
    return result(issued)


@router.delete(TASK + "/mandate", dependencies=[Depends(require_admin)])
async def revoke(request: Request):
    await pilot.revoke(_db(request))
    return result(None)


@router.post(HOST + "/ready")
async def ready(request: Request, name: str, body: pilot.Ready):
    await _host_caller(request, name)
    return result(await pilot.ready(_db(request), current_identity(request), body))


@router.get(HOST + "/order")
async def current_order(request: Request, name: str):
    await _host_caller(request, name)
    return result(await pilot.current(_db(request), current_identity(request)))


@router.get(HOST + "/publication")
async def publication(request: Request, name: str):
    await _host_caller(request, name)
    return result(
        await pilot.current(_db(request), current_identity(request), publication=True)
    )


@router.post(HOST + "/orders/{order_id}/receipt")
async def report(request: Request, name: str, order_id: OrderId, body: pilot.Receipt):
    await _host_caller(request, name)
    return result(
        await pilot.measure(_db(request), current_identity(request), order_id, body)
    )


@router.post(HOST + "/orders/{order_id}/publication")
async def published(
    request: Request, name: str, order_id: OrderId, body: pilot.Publication
):
    await _host_caller(request, name)
    return result(
        await pilot.publish(_db(request), current_identity(request), order_id, body)
    )
