"""Versioned contract for the bounded owner decision."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

PlanState = Literal["draft", "active", "stopped", "expired", "revoked"]


class PlanTerms(BaseModel):
    duration_days: int = 14
    total_cents: int = 4000
    leaf_cents: int = 1500
    max_in_progress: int = 1
    technical_retries: int = 1
    fixes_same_cause: int = 2


class PlanNode(BaseModel):
    task_id: int
    parent_id: int | None
    project_id: int
    task_type: str
    title: str
    revision: int
    fingerprint: str
    areas: list[str]
    blocked_by: list[int]
    allowed: bool
    leaf: bool


class PlanSnapshot(BaseModel):
    schema_version: Literal["1"] = "1"
    epic_id: int
    project: str
    owner_principal_id: int | None
    project_policy_version: str
    project_leaf_ceiling_cents: float | None
    project_wip_limit: int | None
    terms: PlanTerms
    allowed_actions: list[str]
    exceptions: list[str]
    nodes: list[PlanNode]


class PlanDecision(BaseModel):
    version: str
    snapshot: PlanSnapshot
    state: PlanState
    reason: str
    approved_principal_id: int
    approved_at: str
    expires_at: str
    terminal_principal_id: int | None = None
    terminal_at: str | None = None


class PlanView(BaseModel):
    version: str
    snapshot: PlanSnapshot
    state: PlanState
    reason: str
    can_decide: bool
    decision: PlanDecision | None


class PlanConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(pattern=r"^[0-9a-f]{64}$")


class PlanTerminate(PlanConfirm):
    state: Literal["stopped", "revoked"]
    reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]
