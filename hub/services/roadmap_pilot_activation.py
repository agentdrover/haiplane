"""Activate the single owner-approved #2406 observation mandate as the DB owner."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import aiosqlite

from hub import config
from hub.db import connect, write_transaction
from hub.hub_instance import hub_base_url
from hub.services import admin, roadmap_requests
from hub.services.roadmap_activation import (
    ActivationRefused,
    _Mandate,
    _verify_approval,
)
from hub.services.roadmap_observation_pilot import TASK_ID, pilot_for_grant

_MANDATE = _Mandate(
    task_id=TASK_ID,
    project_id=14,
    approval_id=26249,
    approver_id=1,
    generation=2,
    fingerprint="a82777735cf56e78d50974762a7d7d439120d8069269711b58ed293738a5ac28",  # pragma: allowlist secret
    steward_id=42,
    roadmap_id=31,
)
_ACTOR = "hub.local.roadmap_pilot_activation"


async def _run(db: aiosqlite.Connection) -> dict[str, Any]:
    mandate = _MANDATE
    async with write_transaction(db):
        old = await roadmap_requests._one(
            db,
            "SELECT grant_id FROM roadmap_observation_pilots WHERE approval_id=?",
            (mandate.approval_id,),
        )
        if old is not None:
            current = await roadmap_requests._active_grant(db, mandate.task_id)
            active = (
                current
                and current["id"] == old["grant_id"]
                and await roadmap_requests._current(db, current)
            )
            return {
                "grant_id": old["grant_id"],
                "state": "active" if active else "inactive",
                "repeat": True,
            }
        snapshot = await _verify_approval(db, mandate)
        if await roadmap_requests._active_grant(db, mandate.task_id) is not None:
            raise ActivationRefused("existing_grant")
        if await roadmap_requests._one(
            db,
            "SELECT 1 FROM roadmap_grants WHERE task_id=? AND approval_id=? LIMIT 1",
            (mandate.task_id, mandate.approval_id),
        ):
            raise ActivationRefused("approval_already_used_for_grant")
        if not await roadmap_requests._active_principal(
            db, mandate.steward_id, "roadmap_steward"
        ):
            raise ActivationRefused("steward_not_active")
        if not await roadmap_requests._active_principal(
            db, mandate.roadmap_id, "agent"
        ):
            raise ActivationRefused("roadmap_not_active")
        grant_id = await roadmap_requests._store_verified_grant(
            db,
            snapshot,
            task_id=mandate.task_id,
            plan_id=mandate.task_id,
            signal_task_id=None,
            steward_id=mandate.steward_id,
            roadmap_id=mandate.roadmap_id,
            ttl_days=1,
        )
        await db.execute(
            "INSERT INTO roadmap_observation_pilots (approval_id,grant_id,batch_id,activated_at) "
            "SELECT ?,id,id,datetime(expires_at,'-1 day') FROM roadmap_grants WHERE id=?",
            (mandate.approval_id, grant_id),
        )
        await admin.write_audit(
            db,
            actor_id=None,
            action="roadmap_observation_pilot_activated",
            target_type="task",
            target_id=str(mandate.task_id),
            summary=f"{_ACTOR}: activated the approved observation pilot",
            detail=json.dumps(
                {
                    "actor": _ACTOR,
                    "effective_uid": os.geteuid(),
                    "grant_id": grant_id,
                    "approval_id": mandate.approval_id,
                    "approver_id": mandate.approver_id,
                    "project_id": mandate.project_id,
                    "task_id": mandate.task_id,
                    "statement_generation": mandate.generation,
                    "statement_fingerprint": mandate.fingerprint,
                    "ttl_hours": 24,
                    "steward_id": mandate.steward_id,
                    "roadmap_id": mandate.roadmap_id,
                }
            ),
            commit=False,
        )
        pilot = await pilot_for_grant(db, grant_id)
        return {
            "grant_id": grant_id,
            "state": "active",
            "repeat": False,
            "activated_at": pilot["activated_at"] if pilot else None,
        }


async def _command(path: Path) -> dict[str, Any]:
    db = await connect(f"{path.as_uri()}?mode=rw")
    try:
        return await _run(db)
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--apply", required=True, action="store_true")
    args = parser.parse_args()
    try:
        path = args.db.resolve(strict=True)
        if not path.is_file() or path.stat().st_uid != os.geteuid():
            raise ActivationRefused("must_run_as_database_owner")
        if config.HUB_AUTH_DISABLED or hub_base_url() != "https://agenthai.ru":
            raise ActivationRefused("pilot_hub_runtime_config_invalid")
        print(json.dumps(asyncio.run(_command(path)), sort_keys=True))
    except (ActivationRefused, OSError, aiosqlite.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
