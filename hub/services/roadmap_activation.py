"""Local one-shot execution of the exact owner-approved #2404 mandate."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import connect, fetchall, write_transaction
from hub.services import admin, roadmap_decisions, roadmap_requests
from hub.services.statement_generation import statement_fingerprint


@dataclass(frozen=True)
class _Mandate:
    task_id: int
    project_id: int
    approval_id: int
    approver_id: int
    generation: int
    fingerprint: str
    steward_id: int
    roadmap_id: int


# Reviewed binding for AC-4 of the approved #2404 statement. The command
# accepts no task, principal, grant duration, plan or authority overrides.
_MANDATE = _Mandate(
    task_id=2404,
    project_id=14,
    approval_id=26222,
    approver_id=1,
    generation=2,
    fingerprint="ea82f6dcbb2f100bd00c3905b7b486af1d4b21ad5f1be9c2e0e98d7a99f315f2",  # pragma: allowlist secret (public statement digest)
    steward_id=42,
    roadmap_id=31,
)
_ACTIVATION_DEADLINE = "2026-11-20 15:56:00"
_ACTOR = "hub.local.roadmap_activation"


class ActivationRefused(Exception):
    """The approved activation could not proceed."""


async def _previous(
    db: aiosqlite.Connection, mandate: _Mandate
) -> dict[str, Any] | None:
    rows = await fetchall(
        db,
        "SELECT r.grant_id, r.activated_at, g.expires_at, "
        "CASE WHEN g.id IS NULL OR g.revoked_at IS NOT NULL THEN 'revoked' "
        "WHEN g.expires_at <= datetime('now') THEN 'expired' ELSE 'active' END AS state "
        "FROM roadmap_activation_receipts r LEFT JOIN roadmap_grants g ON g.id=r.grant_id "
        "WHERE r.approval_id=? AND r.task_id=? AND r.statement_fingerprint=?",
        (mandate.approval_id, mandate.task_id, mandate.fingerprint),
    )
    if not rows:
        return None
    receipt = {**dict(rows[0]), "repeat": True}
    if receipt["state"] == "active":
        grant = await roadmap_requests._active_grant(db, mandate.task_id)
        if grant is None or not await roadmap_requests._current(db, grant):
            receipt["state"] = "stale"
    return receipt


async def _verify_approval(
    db: aiosqlite.Connection, mandate: _Mandate
) -> tuple[int, int, int, int]:
    rows = await fetchall(
        db,
        "SELECT payload, datetime('now') < ? AS within_window FROM events "
        "WHERE id=? AND task_id=? AND kind='task_approved' AND actor='human'",
        (_ACTIVATION_DEADLINE, mandate.approval_id, mandate.task_id),
    )
    if not rows:
        raise ActivationRefused("owner_approval_missing")
    if not rows[0]["within_window"]:
        raise ActivationRefused("activation_window_expired")
    stamp = json.loads(rows[0]["payload"])
    if (
        stamp.get("human") is not True
        or stamp.get("principal_id") != mandate.approver_id
        or stamp.get("statement_generation") != mandate.generation
        or stamp.get("statement_fingerprint") != mandate.fingerprint
    ):
        raise ActivationRefused("approval_stamp_mismatch")
    principal = await admin.get_principal(db, mandate.approver_id)
    if not principal or principal["kind"] != "human" or principal["status"] != "active":
        raise ActivationRefused("approver_not_active")
    if await admin.get_effective_role(db, mandate.approver_id) not in (
        "admin",
        "super_admin",
    ):
        raise ActivationRefused("approver_not_administrator")
    actual = await roadmap_requests._plan_snapshot(db, mandate.task_id, mandate.task_id)
    expected = (
        mandate.project_id,
        mandate.approval_id,
        mandate.generation,
        mandate.generation,
    )
    if (
        actual != expected
        or await statement_fingerprint(db, mandate.task_id) != mandate.fingerprint
    ):
        raise ActivationRefused("approved_plan_changed")
    return expected


async def _verify_subjects(db: aiosqlite.Connection, mandate: _Mandate) -> None:
    project = await repo.get_project(db, mandate.project_id)
    if project is None or project["slug"] != "home-infra":
        raise ActivationRefused("wrong_project")
    if not await roadmap_requests._active_principal(
        db, mandate.steward_id, "roadmap_steward"
    ):
        raise ActivationRefused("steward_not_active")
    if not await roadmap_requests._active_principal(db, mandate.roadmap_id, "agent"):
        raise ActivationRefused("roadmap_not_active")
    if await roadmap_requests._active_grant(db, mandate.task_id) is not None:
        raise ActivationRefused("another_grant_exists")
    if await fetchall(
        db,
        "SELECT id FROM roadmap_grants WHERE task_id=? AND approval_id=? LIMIT 1",
        (mandate.task_id, mandate.approval_id),
    ):
        raise ActivationRefused("approval_already_used_for_grant")
    checks = await roadmap_decisions.approved_checks(
        db, mandate.task_id, mandate.approval_id, mandate.generation
    )
    if checks != [
        {
            "id": "AC-5",
            "metric": "successful_fresh_redeems",
            "operator": "gte",
            "target": 2,
        }
    ]:
        raise ActivationRefused("approved_pilot_check_missing")


async def _run(db: aiosqlite.Connection, *, apply: bool) -> dict[str, Any]:
    mandate = _MANDATE
    transaction = write_transaction if apply else roadmap_requests._read_snapshot
    async with transaction(db):
        prior = await _previous(db, mandate)
        if prior is not None:
            return prior
        snapshot = await _verify_approval(db, mandate)
        await _verify_subjects(db, mandate)
        if not apply:
            return {
                "state": "eligible",
                "task_id": mandate.task_id,
                "approval_id": mandate.approval_id,
            }
        grant_id = await roadmap_requests._store_verified_grant(
            db,
            snapshot,
            task_id=mandate.task_id,
            plan_id=mandate.task_id,
            signal_task_id=mandate.task_id,
            steward_id=mandate.steward_id,
            roadmap_id=mandate.roadmap_id,
            ttl_days=1,
        )
        await db.execute(
            "INSERT INTO roadmap_activation_receipts "
            "(approval_id,grant_id,approver_id,task_id,statement_fingerprint) VALUES (?,?,?,?,?)",
            (
                mandate.approval_id,
                grant_id,
                mandate.approver_id,
                mandate.task_id,
                mandate.fingerprint,
            ),
        )
        await admin.write_audit(
            db,
            actor_id=None,
            action="roadmap_granted",
            target_type="task",
            target_id=str(mandate.task_id),
            summary=f"{_ACTOR}: executed approved roadmap mandate",
            detail=json.dumps(
                {
                    "actor": _ACTOR,
                    "effective_uid": os.geteuid(),
                    "grant_id": grant_id,
                    "approval_id": mandate.approval_id,
                    "approver_id": mandate.approver_id,
                    "project_id": mandate.project_id,
                    "plan_id": mandate.task_id,
                    "signal_task_id": mandate.task_id,
                    "steward_id": mandate.steward_id,
                    "roadmap_id": mandate.roadmap_id,
                    "ttl_hours": 24,
                },
                sort_keys=True,
            ),
            commit=False,
        )
        receipt = await _previous(db, mandate)
        if receipt is None:
            raise ActivationRefused("activation_receipt_missing")
        return {**receipt, "repeat": False}


async def _command(path: Path, *, apply: bool) -> dict[str, Any]:
    if apply:
        db = await connect(f"{path.as_uri()}?mode=rw")
    else:
        db = await aiosqlite.connect(f"{path.as_uri()}?mode=ro", uri=True)
        db.row_factory = aiosqlite.Row
    try:
        return await _run(db, apply=apply)
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    try:
        path = args.db.resolve(strict=True)
        if not path.is_file() or path.stat().st_uid != os.geteuid():
            raise ActivationRefused("must_run_as_database_owner")
        print(json.dumps(asyncio.run(_command(path, apply=args.apply)), sort_keys=True))
    except (ActivationRefused, OSError, aiosqlite.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
