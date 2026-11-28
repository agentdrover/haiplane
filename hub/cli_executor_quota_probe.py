"""Run the no-model probe as the local Hub database owner."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import AwareDatetime, ValidationError

from hub import config
from hub.db import connect
from hub.services import executor_quota_probe as probe
from hub.services.executor_orders import HostError


class _Plan(probe.ProbePlan):
    expires_at: AwareDatetime


def _private_plan(path: Path) -> _Plan:
    with os.fdopen(
        os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb"
    ) as file:
        info = os.fstat(file.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("Use a private plan owned by the local Hub account")
        raw = file.read(8193)
    if len(raw) > 8192:
        raise ValueError("Plan exceeds 8192 bytes")
    return _Plan.model_validate_json(raw)


def _database_path() -> Path:
    configured = config.HUB_DB_PATH.absolute()
    path = configured.resolve(strict=True)
    info = path.stat()
    if (
        path != configured
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
    ):
        raise ValueError("Run as the owner of the protected Hub database")
    return path


async def _apply(args: argparse.Namespace) -> dict:
    plan = _private_plan(args.plan) if args.probe_cmd == "issue" else None
    connection = await connect(str(_database_path()))
    try:
        if plan is None:
            await probe.revoke(connection, args.order_id)
            return {"order_id": args.order_id, "status": "revoked"}
        result = await probe.issue(
            connection,
            binding=plan.binding,
            authority_ref=plan.authority_ref,
            expires_at=plan.expires_at,
        )
        return {"order_id": result["order_id"], "expires_at": result["expires_at"]}
    finally:
        await connection.close()


def cmd_executor_quota_probe(args: argparse.Namespace) -> int:
    try:
        print(json.dumps(asyncio.run(_apply(args))))
    except ValidationError:
        print("Invalid quota probe plan", file=sys.stderr)
        return 2
    except HostError as exc:
        print(f"Quota probe refused ({exc.detail})", file=sys.stderr)
        return 2
    except (OSError, ValueError, aiosqlite.Error) as exc:
        print(f"Local quota probe unavailable ({type(exc).__name__})", file=sys.stderr)
        return 2
    return 0


def add_executor_quota_probe_parser(sub: Any) -> None:
    root = sub.add_parser(
        "executor-quota-probe",
        help="Issue or revoke the one no-model probe as the local Hub database owner",
    )
    commands = root.add_subparsers(dest="probe_cmd", required=True)
    commands.add_parser("issue").add_argument("--plan", type=Path, required=True)
    commands.add_parser("revoke").add_argument("order_id")
    root.set_defaults(func=cmd_executor_quota_probe)
