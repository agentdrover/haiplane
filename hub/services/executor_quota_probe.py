"""A single service receipt exercises v1 without booking a production executor."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from typing import Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field

from hub import config
from hub.db import fetchall, write_transaction
from hub.services.epic_plan_authority import WorkBinding
from hub.services.executor_orders import HostError, enabled_host_names

Phase = Literal["before_redeem", "before_exec"]
Switch = Literal["1"]
HOST = "k8"
HEX_PATTERN = r"^[0-9a-f]{32}$"
REF_PATTERN = r"^[A-Za-z0-9_.-]{12,128}$"
READY_VERSION = 1


class ProbeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    admission_version: int = Field(ge=1, le=1, strict=True)
    authority_ref: str = Field(pattern=REF_PATTERN)
    binding: WorkBinding
    checks: dict[Phase, int]


class ProbePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    binding: WorkBinding
    authority_ref: str = Field(pattern=REF_PATTERN)


def enabled(host: str) -> bool:
    return (
        host == HOST and config.env_get("EPIC_PLAN_QUOTA_PROBE_ENABLED", "") == "true"
    )


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="microseconds")


async def _row(db: aiosqlite.Connection, host: str) -> aiosqlite.Row | None:
    rows = await fetchall(
        db, "SELECT * FROM executor_quota_probe WHERE host=?", (host,)
    )
    return rows[0] if rows else None


async def _require(db: aiosqlite.Connection, host: str, order_id: str) -> aiosqlite.Row:
    row = await _row(db, host) if enabled(host) else None
    if (
        row is None
        or row["order_id"] != order_id
        or host not in await enabled_host_names(db)
    ):
        raise HostError(404, {"reason": "quota_probe_not_found"})
    return row


def _binding(row: aiosqlite.Row) -> WorkBinding:
    return WorkBinding.model_validate_json(row["binding_json"])


def _envelope(row: aiosqlite.Row) -> dict:
    return {
        "admission_version": 1,
        "kind": "quota_probe",
        "order_id": row["order_id"],
        "host": row["host"],
        "pilot": True,
        "probe_only": True,
        "authority_ref": row["authority_ref"],
        "binding": _binding(row).model_dump(),
        "expires_at": row["expires_at"],
    }


def _receipt(row: aiosqlite.Row, recorded_at: str) -> dict:
    return {
        "admission_version": 1,
        "order_id": row["order_id"],
        "authority_ref": row["authority_ref"],
        "status": "recorded",
        "recorded_at": recorded_at,
    }


def _ready_until(row: aiosqlite.Row, now: str) -> bool:
    return row["revoked_at"] is None and row["expires_at"] > now


async def issue(
    db: aiosqlite.Connection,
    *,
    binding: WorkBinding,
    authority_ref: str,
    expires_at: datetime,
) -> dict:
    """Issue one probe from the local operator entry only. Never create a run."""
    subject = ProbePlan(binding=binding, authority_ref=authority_ref)
    if (
        not enabled(binding.host)
        or binding.phase != "executor"
        or binding.model != "probe-only"
    ):
        raise HostError(409, {"reason": "quota_probe_not_enabled"})
    if expires_at.utcoffset() is None:
        raise HostError(422, {"reason": "quota_probe_expiry_invalid"})
    encoded = json.dumps(binding.model_dump(), sort_keys=True, separators=(",", ":"))
    until = _stamp(expires_at)
    async with write_transaction(db):
        row = await _row(db, binding.host)
        if row is not None:
            if (row["authority_ref"], row["binding_json"], row["expires_at"]) != (
                subject.authority_ref,
                encoded,
                until,
            ):
                raise HostError(409, {"reason": "quota_probe_already_issued"})
            return _envelope(row)
        if binding.host not in await enabled_host_names(db):
            raise HostError(409, {"reason": "quota_probe_host_unavailable"})
        now = datetime.now(UTC)
        if not now < expires_at <= now + timedelta(minutes=30):
            raise HostError(422, {"reason": "quota_probe_expiry_invalid"})
        order_id = "probe-" + token_hex(16)
        await db.execute(
            """INSERT INTO executor_quota_probe
                (singleton,order_id,authority_ref,binding_json,host,created_at,expires_at)
                VALUES (1,?,?,?,?,?,?)""",
            (
                order_id,
                subject.authority_ref,
                encoded,
                binding.host,
                _stamp(now),
                until,
            ),
        )
        return _envelope(await _require(db, binding.host, order_id))


async def offers(db: aiosqlite.Connection, host: str) -> list[dict]:
    row = await _row(db, host) if enabled(host) else None
    if (
        row is None
        or not _ready_until(row, _stamp(datetime.now(UTC)))
        or row["recorded_at"]
    ):
        return []
    return [_envelope(row)]


async def check_phase(
    db: aiosqlite.Connection, host: str, order_id: str, phase: Phase
) -> dict:
    # The first observation, expiry and revoke have a single ordering.
    async with write_transaction(db):
        row = await _require(db, host, order_id)
        now = _stamp(datetime.now(UTC))
        floor = row["before_redeem_at"] or row["created_at"]
        now = max(now, floor)
        if phase == "before_redeem":
            observed = row["before_redeem_at"]
        else:
            observed = row["before_exec_at"]
        version, status, reason = READY_VERSION, "ready", "probe_only"
        decided_at = observed or now
        if row["revoked_at"] is not None:
            version, status, reason = 2, "revoked", "probe_revoked"
            decided_at = row["revoked_at"]
        elif row["expires_at"] <= now:
            version, status, reason = 2, "deny", "probe_expired"
            decided_at = row["expires_at"]
        elif phase == "before_exec" and row["before_redeem_at"] is None:
            status, reason = "wait", "before_redeem_required"
        elif observed is None:
            if phase == "before_redeem":
                query = "UPDATE executor_quota_probe SET before_redeem_at=? WHERE order_id=?"
            else:
                query = (
                    "UPDATE executor_quota_probe SET before_exec_at=? WHERE order_id=?"
                )
            await db.execute(query, (now, order_id))
        return {
            **_envelope(row),
            "phase": phase,
            "status": status,
            "state_version": version,
            "reason": reason,
            "decided_at": decided_at,
            "reset_at": None,
        }


async def record(
    db: aiosqlite.Connection, host: str, order_id: str, evidence: ProbeReceipt
) -> dict:
    async with write_transaction(db):
        row = await _require(db, host, order_id)
        if (
            evidence.authority_ref != row["authority_ref"]
            or evidence.binding != _binding(row)
            or evidence.checks
            != {"before_redeem": READY_VERSION, "before_exec": READY_VERSION}
            or row["before_exec_at"] is None
        ):
            raise HostError(409, {"reason": "quota_probe_evidence_mismatch"})
        if row["recorded_at"] is not None:
            return _receipt(row, row["recorded_at"])
        now = max(_stamp(datetime.now(UTC)), row["before_exec_at"])
        if not _ready_until(row, now):
            raise HostError(409, {"reason": "quota_probe_not_ready"})
        await db.execute(
            "UPDATE executor_quota_probe SET recorded_at=? WHERE order_id=?",
            (now, order_id),
        )
        return _receipt(row, now)


async def revoke(db: aiosqlite.Connection, order_id: str) -> None:
    """Stop new observations or new receipts, keeping any accepted receipt."""
    async with write_transaction(db):
        row = await _row(db, HOST)
        if row is None or row["order_id"] != order_id:
            raise HostError(404, {"reason": "quota_probe_not_found"})
        if row["revoked_at"] is None:
            await db.execute(
                "UPDATE executor_quota_probe SET revoked_at=? WHERE order_id=?",
                (_stamp(datetime.now(UTC)), order_id),
            )
