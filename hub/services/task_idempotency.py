"""Idempotent POST /api/tasks via client_request_id."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from hub.models import TaskCreate, TaskSource, TaskType


class IdempotencyConflictError(Exception):
    """Same client_request_id was already used with a different payload."""

    def __init__(self, client_request_id: str, existing_task_id: int):
        self.client_request_id = client_request_id
        self.existing_task_id = existing_task_id
        super().__init__(
            f"client_request_id {client_request_id!r} already used for task "
            f"#{existing_task_id} with a different payload"
        )


@dataclass(frozen=True)
class IdempotencyRecord:
    client_request_id: str
    task_id: int
    request_hash: str


def resolve_client_request_id(
    header_value: str | None,
    body_value: str | None,
) -> str | None:
    key = (header_value or body_value or "").strip()
    return key or None


def normalize_task_create(body: TaskCreate) -> tuple[str, TaskCreate]:
    """Apply the same lifecycle normalizations used before insert."""
    normalized = body.model_copy(deep=True)
    if normalized.task_type in (TaskType.epic, TaskType.feature):
        # Agents PROPOSE features/epics as drafts (#323) — the human approval
        # gate owns the decomposition. Human-created ones stay open (the
        # pre-#323 invariant, now scoped to source=human).
        initial_status = "draft" if normalized.source == TaskSource.agent else "open"
        normalized.run_immediately = False
        normalized.auto_review = False
    elif normalized.source == TaskSource.agent:
        initial_status = "draft"
    elif normalized.run_immediately:
        initial_status = "running"
    else:
        initial_status = "open"

    if normalized.task_type == TaskType.subtask and normalized.auto_review:
        normalized.auto_review = False

    return initial_status, normalized


def hash_task_create_payload(body: TaskCreate) -> str:
    """Stable hash of the create payload (excluding the idempotency key itself)."""
    payload = body.model_dump(mode="json", exclude={"client_request_id"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def idempotency_conflict_detail(record: IdempotencyRecord) -> dict[str, Any]:
    return {
        "reason": "idempotency_conflict",
        "message": (
            "client_request_id already used with a different payload; "
            "reuse the original request body or choose a new key"
        ),
        "client_request_id": record.client_request_id,
        "existing_task_id": record.task_id,
    }
