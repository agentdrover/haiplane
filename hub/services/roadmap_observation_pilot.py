"""#2406 pilot receipt and authenticated host observations/effects."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, Literal

import aiosqlite
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from hub.config import TokenIdentity
from hub.db import fetchall, write_transaction
from hub.services import admin
from hub.services.roadmap_contracts import PositiveId, RoadmapResult

TASK_ID = 2406
HOST = "k8"
MIN_SPACING_SECONDS = 60
OBSERVATION_MINUTES = 10
CONSULTATION_START_MINUTES = 15
CHECKS = ("codex_ready", "openai_ready", "repository_read")


async def authority_is_current(db: aiosqlite.Connection, grant: aiosqlite.Row) -> bool:
    from hub.services import roadmap_pilot_activation
    from hub.services.statement_generation import statement_fingerprint

    mandate = roadmap_pilot_activation._MANDATE
    return (
        grant["task_id"] == grant["plan_id"] == mandate.task_id
        and grant["project_id"] == mandate.project_id
        and grant["approval_id"] == mandate.approval_id
        and grant["plan_generation"] == grant["task_generation"] == mandate.generation
        and grant["steward_id"] == mandate.steward_id
        and grant["roadmap_id"] == mandate.roadmap_id
        and await statement_fingerprint(db, mandate.task_id) == mandate.fingerprint
    )


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activation_id: PositiveId
    observation_id: str = Field(
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )
    )
    codex_ready: StrictBool | None
    openai_ready: StrictBool | None
    repository_read: StrictBool | None


class EffectReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["read", "failed", "unknown"]
    version: str | None = Field(pattern=r"^[0-9a-f]{40}$")

    @model_validator(mode="after")
    def check_version(self) -> EffectReport:
        if (self.outcome == "read") != (self.version is not None):
            raise ValueError("version is required exactly when outcome is read")
        return self


async def pilot_for_grant(
    db: aiosqlite.Connection, grant_id: int
) -> aiosqlite.Row | None:
    from hub.services.roadmap_requests import _one

    return await _one(
        db,
        "SELECT *, datetime(activated_at, ?) <= datetime('now') AS timed_out "
        "FROM roadmap_observation_pilots WHERE grant_id=?",
        (f"+{OBSERVATION_MINUTES} minutes", grant_id),
    )


async def _measurements(
    db: aiosqlite.Connection, pilot: aiosqlite.Row
) -> list[aiosqlite.Row]:
    return await fetchall(
        db,
        "SELECT *, observed_at <= datetime('now', ?) AS spacing_elapsed "
        "FROM roadmap_pilot_observations WHERE pilot_id=? ORDER BY sequence",
        (f"-{MIN_SPACING_SECONDS} seconds", pilot["approval_id"]),
    )


def _may_sample(pilot: aiosqlite.Row, rows: list[aiosqlite.Row]) -> bool:
    return not pilot["timed_out"] and (
        not rows or (len(rows) == 1 and bool(rows[0]["spacing_elapsed"]))
    )


async def batch(
    db: aiosqlite.Connection, pilot: aiosqlite.Row
) -> dict[str, Any] | None:
    rows = await _measurements(db, pilot)
    if len(rows) < 2 and not pilot["timed_out"]:
        return None
    return {
        "observation_batch_id": pilot["batch_id"],
        "minimum_spacing_seconds": MIN_SPACING_SECONDS,
        "observations": [
            {
                "observation_id": row["observation_id"],
                "observed_at": row["observed_at"].replace(" ", "T") + "Z",
                **{key: None if row[key] is None else bool(row[key]) for key in CHECKS},
            }
            for row in rows
        ],
    }


@asynccontextmanager
async def host_scope(
    db: aiosqlite.Connection, name: str, who: TokenIdentity, *, write: bool
):
    from hub.services import roadmap_requests

    if name != HOST:
        raise HTTPException(404, "observation pilot not found")
    transaction = write_transaction if write else roadmap_requests._read_snapshot
    async with transaction(db):
        grant = await roadmap_requests._active_grant(db, TASK_ID)
        host = await roadmap_requests._one(
            db,
            "SELECT 1 FROM executor_hosts h JOIN principals p ON p.id=h.principal_id "
            "WHERE h.name=? AND h.enabled=1 AND p.status='active' AND p.id=?",
            (name, who.principal_id),
        )
        if (
            host is None
            or grant is None
            or not await roadmap_requests._current(db, grant)
        ):
            yield None
            return
        yield await pilot_for_grant(db, grant["id"])


async def observe_status(
    db: aiosqlite.Connection, name: str, who: TokenIdentity
) -> dict[str, Any]:
    async with host_scope(db, name, who, write=False) as pilot:
        return {
            "activation_id": pilot["grant_id"] if pilot else None,
            "observation_permitted": bool(
                pilot and _may_sample(pilot, await _measurements(db, pilot))
            ),
        }


async def _audit_host(
    db: aiosqlite.Connection, who: TokenIdentity, action: str, detail: dict[str, Any]
) -> None:
    await admin.write_audit(
        db,
        actor_id=who.principal_id,
        action=action,
        target_type="task",
        target_id=str(TASK_ID),
        summary=action,
        detail=json.dumps(detail, sort_keys=True),
        commit=False,
    )


async def observe(
    db: aiosqlite.Connection, name: str, who: TokenIdentity, body: Observation
) -> dict[str, Any]:
    async with host_scope(db, name, who, write=True) as pilot:
        if pilot is None or pilot["grant_id"] != body.activation_id:
            raise HTTPException(409, "observation activation is unavailable")
        rows = await _measurements(db, pilot)
        checks = tuple(getattr(body, key) for key in CHECKS)
        for row in rows:
            if row["observation_id"] == body.observation_id:
                if checks != tuple(row[key] for key in CHECKS):
                    raise HTTPException(409, "observation identifier already used")
                return {"sequence": row["sequence"], "repeat": True}
        if not _may_sample(pilot, rows):
            raise HTTPException(409, "observation is not permitted")
        sequence = len(rows) + 1
        await db.execute(
            "INSERT INTO roadmap_pilot_observations "
            "(pilot_id,sequence,observation_id,codex_ready,openai_ready,repository_read) "
            "VALUES (?,?,?,?,?,?)",
            (pilot["approval_id"], sequence, body.observation_id, *checks),
        )
        await _audit_host(db, who, "roadmap_pilot_observation", body.model_dump())
        return {"sequence": sequence, "repeat": False}


async def effect_status(
    db: aiosqlite.Connection, name: str, who: TokenIdentity
) -> dict[str, Any]:
    async with host_scope(db, name, who, write=False) as pilot:
        return {"effect_id": pilot["effect_id"] if pilot else None}


def _require_effect(pilot: aiosqlite.Row | None, effect_id: str) -> aiosqlite.Row:
    if pilot is None or pilot["effect_id"] != effect_id:
        raise HTTPException(404, "pilot effect not found")
    return pilot


async def claim(
    db: aiosqlite.Connection, name: str, who: TokenIdentity, effect_id: str
) -> dict[str, Any]:
    async with host_scope(db, name, who, write=True) as pilot:
        effect = _require_effect(pilot, effect_id)
        execute = effect["claimed_at"] is None
        if execute:
            await db.execute(
                "UPDATE roadmap_observation_pilots SET claimed_at=datetime('now') "
                "WHERE effect_id=?",
                (effect_id,),
            )
            await _audit_host(
                db, who, "roadmap_pilot_effect_claimed", {"effect_id": effect_id}
            )
        return {"effect_id": effect_id, "execute": execute}


async def report(
    db: aiosqlite.Connection,
    name: str,
    who: TokenIdentity,
    effect_id: str,
    body: EffectReport,
) -> dict[str, Any]:
    async with host_scope(db, name, who, write=True) as pilot:
        effect = _require_effect(pilot, effect_id)
        if effect["claimed_at"] is None:
            raise HTTPException(409, "pilot effect was not claimed")
        repeat = effect["reported_at"] is not None
        if repeat:
            if (effect["effect_outcome"], effect["effect_version"]) != (
                body.outcome,
                body.version,
            ):
                raise HTTPException(409, "pilot effect already reported")
        else:
            await db.execute(
                "UPDATE roadmap_observation_pilots SET effect_outcome=?, effect_version=?, "
                "reported_at=datetime('now') WHERE effect_id=?",
                (body.outcome, body.version, effect_id),
            )
            await _audit_host(
                db,
                who,
                "roadmap_pilot_effect_reported",
                {"effect_id": effect_id, **body.model_dump()},
            )
        return {"effect_id": effect_id, "repeat": repeat}


async def grant_signal(
    db: aiosqlite.Connection, pilot: aiosqlite.Row
) -> dict[str, Any]:
    observation_batch = await batch(db, pilot)
    state = await consultation_state(db, pilot)
    return {
        "activated_at": pilot["activated_at"].replace(" ", "T") + "Z",
        "observation_batch_id": pilot["batch_id"] if observation_batch else None,
        "consultation_order_id": pilot["order_id"],
        "pending_decision_order_id": pilot["order_id"]
        if state in ("ready", "unavailable")
        else None,
        "decision_id": pilot["order_id"] if pilot["decided_at"] is not None else None,
        "decided_at": pilot["decided_at"],
        "decision_reason": pilot["decision_reason"],
        "effect_claimed_at": pilot["claimed_at"],
        "effect_reported_at": pilot["reported_at"],
        "effect_id": pilot["effect_id"],
        "effect_outcome": pilot["effect_outcome"],
        "effect_version": pilot["effect_version"],
    }


async def consultation_state(db: aiosqlite.Connection, pilot: aiosqlite.Row) -> str:
    from hub.services.roadmap_requests import _one

    if pilot["decision_reason"] is not None:
        return "decided"
    order = await _one(
        db,
        "SELECT state, started_at IS NULL AND datetime(created_at, ?) <= datetime('now') AS timed_out "
        "FROM role_orders WHERE id=?",
        (f"+{CONSULTATION_START_MINUTES} minutes", pilot["order_id"]),
    )
    if order is None:
        return "not_requested"
    if order["state"] == "done" and not order["timed_out"]:
        return "ready"
    if order["state"] in ("failed", "expired") or order["timed_out"]:
        return "unavailable"
    return "pending"


async def check_source(
    db: aiosqlite.Connection,
    pilot: aiosqlite.Row | None,
    batch_id: int | None,
    source_order_id: int | None,
) -> None:
    if pilot is None and batch_id is None:
        return
    if (
        pilot is None
        or batch_id != pilot["batch_id"]
        or source_order_id is not None
        or await batch(db, pilot) is None
    ):
        raise HTTPException(
            409,
            {
                "reason": "roadmap_observation_batch_invalid",
                "message": "observation batch is outside the grant or not sealed",
            },
        )


async def bind_order(
    db: aiosqlite.Connection, pilot: aiosqlite.Row, order_id: int
) -> None:
    await db.execute(
        "UPDATE roadmap_observation_pilots SET order_id=? WHERE approval_id=?",
        (order_id, pilot["approval_id"]),
    )


async def decision_context(
    db: aiosqlite.Connection, pilot: aiosqlite.Row, legacy: dict[str, Any]
) -> dict[str, Any]:
    return {
        "plan": legacy["plan"],
        "observation_batch": await batch(db, pilot),
        "allowed_recommendations": [
            "verify_version_read",
            "hold_pilot",
            "insufficient_facts",
        ],
    }


def _decide(context: dict[str, Any], result: RoadmapResult | None) -> str:
    from hub.services.roadmap_contracts import ObservationBasis

    if result is None:
        return "consultation_failed"
    batch_view = context["observation_batch"]
    if (
        result.risk != "unknown"
        or not isinstance(result.basis, ObservationBasis)
        or (result.basis.observation_batch_id != batch_view["observation_batch_id"])
    ):
        return "consultation_invalid"
    values = [row[key] for row in batch_view["observations"] for key in CHECKS]
    if False in values:
        return (
            "hold_pilot"
            if result.recommendation == "hold_pilot"
            else "consultation_invalid"
        )
    if len(values) < 6 or None in values:
        return (
            "insufficient_facts"
            if result.recommendation == "insufficient_facts"
            else "consultation_invalid"
        )
    return (
        "verify_version_read"
        if result.recommendation == "verify_version_read"
        else "consultation_invalid"
    )


async def evaluate(
    db: aiosqlite.Connection,
    who: TokenIdentity,
    pilot: aiosqlite.Row,
    request: aiosqlite.Row,
    order_id: int,
) -> dict[str, Any]:
    from secrets import token_hex

    from hub.services import roadmap_decisions, roadmap_requests

    if pilot["order_id"] != order_id:
        raise HTTPException(404, "roadmap consultation not found")
    state = await consultation_state(db, pilot)
    if state not in ("ready", "unavailable", "decided"):
        raise HTTPException(409, {"reason": "consultation_pending"})
    context = await decision_context(
        db, pilot, roadmap_decisions.decision_context(request)
    )
    reason = pilot["decision_reason"]
    effect_id = pilot["effect_id"]
    if reason is None:
        row = await roadmap_requests._one(
            db,
            "SELECT context_json, result_json FROM roadmap_results WHERE order_id=?",
            (order_id,),
        )
        result = None
        if (
            state == "ready"
            and row is not None
            and json.loads(row["context_json"]) == context
        ):
            result = RoadmapResult.model_validate_json(row["result_json"])
        reason = _decide(context, result)
        effect_id = token_hex(16) if reason == "verify_version_read" else None
        await db.execute(
            "UPDATE roadmap_observation_pilots SET decision_reason=?,effect_id=?,"
            "decided_at=datetime('now') WHERE order_id=?",
            (reason, effect_id, order_id),
        )
        await roadmap_requests._audit(
            db,
            who,
            "roadmap_pilot_decision",
            TASK_ID,
            json.dumps(
                {
                    "reason": reason,
                    "effect_id": effect_id,
                    "order_id": order_id,
                    "grant_id": pilot["grant_id"],
                    "basis": context,
                },
                sort_keys=True,
            ),
        )
    return {
        "order_id": order_id,
        "decision_id": order_id,
        "reason": reason,
        "effect_id": effect_id,
        "status": "accepted"
        if reason in ("hold_pilot", "verify_version_read")
        else "blocked",
        "repeat": state == "decided",
        "recipient": "steward",
        "basis": context,
    }
