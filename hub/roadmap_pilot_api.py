"""Authenticated k8 observations and single read permit for approved pilot #2406."""

from typing import Annotated

from fastapi import APIRouter, Path, Request, Response
from pydantic import BaseModel, ConfigDict

from hub.auth import current_identity
from hub.executor_host_api import _db, _host_caller
from hub.services import roadmap_observation_pilot as pilot

router = APIRouter(prefix="/api/executor-hosts/{name}/roadmap-pilot/2406")
EffectId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.get("/observations")
async def observation_status(name: str, request: Request, response: Response):
    await _host_caller(request, name)
    response.headers["Cache-Control"] = "no-store"
    return await pilot.observe_status(_db(request), name, current_identity(request))


@router.post("/observations")
async def observation(name: str, body: pilot.Observation, request: Request):
    await _host_caller(request, name)
    return await pilot.observe(_db(request), name, current_identity(request), body)


@router.get("/effect")
async def effect_status(name: str, request: Request, response: Response):
    await _host_caller(request, name)
    response.headers["Cache-Control"] = "no-store"
    return await pilot.effect_status(_db(request), name, current_identity(request))


@router.post("/effects/{effect_id}/claim")
async def claim(name: str, effect_id: EffectId, body: Claim, request: Request):
    await _host_caller(request, name)
    return await pilot.claim(_db(request), name, current_identity(request), effect_id)


@router.post("/effects/{effect_id}/report")
async def report(
    name: str, effect_id: EffectId, body: pilot.EffectReport, request: Request
):
    await _host_caller(request, name)
    return await pilot.report(
        _db(request), name, current_identity(request), effect_id, body
    )
