"""Server-owned limits for a temporary steward act pilot (#2418)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import aiosqlite
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from hub import config
from hub import repository as repo
from hub.db import fetchall
from hub.services.statement_generation import statement_fingerprint

REFUSED = "steward_pilot_scope"
RETRY_SOURCE_EVENT = "steward_executor_retry_source"


class RetryTarget(BaseModel):
    """An approved launch pin, optionally restricted to a known source order."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_order_id: int | None = Field(default=None, gt=0)
    starting_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    model: str = Field(min_length=1, max_length=96)


class TaskPin(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    task_id: int = Field(gt=0)
    statement_generation: int = Field(ge=0)
    submission_generation: int = Field(ge=0)
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    retry: RetryTarget | None = None


class Pilot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    project: Literal["default"]
    plan_id: int = Field(gt=0)
    approval_event_id: int = Field(gt=0)
    starts_at: AwareDatetime
    expires_at: AwareDatetime
    tasks: list[TaskPin] = Field(min_length=1, max_length=3)
    active_task_id: int = Field(gt=0)

    def active_pin(self, now: datetime) -> TaskPin | None:
        ids = {pin.task_id for pin in self.tasks}
        if len(ids) != len(self.tasks) or self.plan_id in ids:
            return None
        if not self.starts_at <= now < self.expires_at:
            return None
        if self.expires_at - self.starts_at > timedelta(days=7):
            return None
        return next(
            (pin for pin in self.tasks if pin.task_id == self.active_task_id), None
        )


def read_pilot() -> Pilot | None:
    if config.STEWARD_ACT_PILOT is None:
        return None
    try:
        return Pilot.model_validate_json(config.STEWARD_ACT_PILOT)
    except ValidationError:
        return None


async def approval_matches(db: aiosqlite.Connection, pilot: Pilot) -> bool:
    plan = await repo.get_task(db, pilot.plan_id)
    if (
        plan is None
        or plan["archived"]
        or plan["status"] not in {"open", "claimed", "running", "review", "completed"}
    ):
        return False
    project = await repo.resolve_project_for_task(db, pilot.plan_id)
    if project is None or project["slug"] != pilot.project:
        return False
    approvals = await fetchall(
        db,
        "SELECT payload FROM events WHERE id=? AND task_id=? "
        "AND kind='task_approved' AND actor='human'",
        (pilot.approval_event_id, pilot.plan_id),
    )
    if not approvals:
        return False
    try:
        stamp = json.loads(approvals[0]["payload"])
    except (TypeError, ValueError):
        return False
    return (
        isinstance(stamp, dict)
        and stamp.get("force") is False
        and stamp.get("statement_generation") == plan["statement_generation"]
        and stamp.get("statement_fingerprint")
        == await statement_fingerprint(db, pilot.plan_id)
    )


async def retry_source_pin(
    db: aiosqlite.Connection, task: dict[str, Any], project: str, host: str
) -> tuple[int, RetryTarget] | None:
    """Bind an ordinary slot launch to a current, approved pilot before it is issued."""
    if host != "k8" or project != "default" or task["status"] != "open":
        return None

    pilot = read_pilot()
    active = pilot.active_pin(datetime.now(timezone.utc)) if pilot else None
    if pilot is None or active is None or active.retry is None:
        return None
    if active.retry.source_order_id is not None:
        return None
    if (
        task["id"] != active.task_id
        or task["archived"]
        or task["risk_class"] not in {"R0", "R1"}
        or task["statement_generation"] != active.statement_generation
        or task["submission_generation"] != active.submission_generation
        or (active.submission_generation > 0 and task["submission_sha"] != active.sha)
    ):
        return None

    from hub.services.steward_apply import policy_refusal
    from hub.services.steward_shadow import act_granted_now

    if (
        await policy_refusal(db, active.task_id)
        or not await act_granted_now(db)
        or not await approval_matches(db, pilot)
    ):
        return None
    return pilot.approval_event_id, active.retry


async def refusal_for(
    db: aiosqlite.Connection, task_id: int, generation: int | None
) -> tuple[str, str] | None:
    """Read the current pilot scope inside the transaction writing an effect."""
    if config.STEWARD_ACT_PILOT is None:
        return None

    pilot = read_pilot()
    pin = pilot.active_pin(datetime.now(timezone.utc)) if pilot else None
    if pilot is None or pin is None:
        return REFUSED, "ограниченный допуск стюарда закрыт, просрочен или некорректен"
    if (
        generation is None
        or generation < 1
        or (task_id, generation) != (pin.task_id, pin.submission_generation)
    ):
        return REFUSED, "задача или поколение вне действующего допуска стюарда"

    project = await repo.resolve_project_for_task(db, task_id)
    if project is None or project["slug"] != pilot.project:
        return REFUSED, "проект вне действующего допуска стюарда"

    task = await repo.get_task(db, task_id)
    if (
        task is None
        or task["archived"]
        or task["status"] != "review"
        or task["risk_class"] not in {"R0", "R1"}
        or task["statement_generation"] != pin.statement_generation
        or task["submission_generation"] != pin.submission_generation
        or task["submission_sha"] != pin.sha
    ):
        return REFUSED, "версия, класс риска или состояние сдачи изменились"
    if not await approval_matches(db, pilot):
        return REFUSED, "текущая версия плана не подтверждена одобрением владельца"
    return None
