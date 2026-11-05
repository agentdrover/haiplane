"""Operation-key contract helpers; no transport or database I/O."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

from fastapi import HTTPException

from hub import config

CREATE_OPERATION = "POST /api/tasks"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~:-]*\Z")


def principal_ref(identity: Any) -> str:
    if identity.principal_id is not None:
        return f"p:{identity.principal_id}"
    return f"t:{identity.username}"


def error(status: int, reason: str, message: str = "", **extra: Any) -> HTTPException:
    detail = {
        "reason": reason,
        "message": message or reason.replace("_", " "),
        "hint": "Keep the same key for retries; use a new key for a new intention.",
        "actor_hint": "agent",
        "retry_safe": status == 503,
        **extra,
    }
    return HTTPException(
        status, detail, headers={"Retry-After": "1"} if status == 503 else None
    )


def database_error(exc: Exception) -> HTTPException | None:
    text = str(exc).lower()
    if "full" in text:
        return error(507, "idempotency_storage_full")
    if "locked" in text or "busy" in text:
        return error(503, "idempotency_busy")
    return None


def conflict(kind: str, operation_id: str | None, **extra: Any) -> HTTPException:
    return error(
        409,
        "idempotency_conflict",
        hint=(
            "Use a new operation_id for a new intention; do not change "
            "arguments under an existing id."
        ),
        conflict_kind=kind,
        operation=CREATE_OPERATION,
        operation_id=operation_id,
        **extra,
    )


def validate_operation_id(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if not 16 <= len(value) <= 128 or not _ID.fullmatch(value):
        raise error(400, "invalid_operation_id")
    return value


def validate_alias(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if len(value) > 128 or not _ID.fullmatch(value):
        raise error(400, "invalid_client_request_id")
    return value


def resolve_alias(header: str | None, body: str | None) -> str | None:
    header, body = (header or "").strip(), (body or "").strip()
    if header and body and header != body:
        raise error(400, "idempotency_key_mismatch")
    return validate_alias(header or body)


def resolve_operation_id(headers: Mapping[str, str]) -> str | None:
    # Plain dicts and Starlette Headers are both accepted.
    values = {k.lower(): v.strip() for k, v in headers.items()}
    key, alias = values.get("idempotency-key", ""), values.get("x-operation-id", "")
    if key and alias and key != alias:
        raise error(400, "idempotency_key_mismatch")
    return validate_operation_id(key or alias)


def new_server_operation_id() -> str:
    return "srv_" + uuid4().hex


def hash_args_v1(args: Any) -> str:
    from hub.services.task_idempotency import hash_task_create_payload

    return hash_task_create_payload(args)


def hash_args_v2(operation: str, args: Any) -> str:
    canonical = json.dumps(
        {"operation": operation, "args": args},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(b"haiplane-idem-v2\n" + canonical.encode()).hexdigest()


def utc_timestamp(seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def result_expires_at() -> str:
    return utc_timestamp(config.IDEMPOTENCY_RESULT_TTL_SECONDS)


def key_expires_at() -> str:
    return utc_timestamp(config.IDEMPOTENCY_KEY_TTL_SECONDS)


def response_headers(operation_id: str, replayed: bool) -> dict[str, str]:
    headers = {"Idempotency-Key": operation_id, "Cache-Control": "no-store"}
    if replayed:
        headers.update({"Idempotent-Replayed": "true", "X-Original-Status": "201"})
    return headers
