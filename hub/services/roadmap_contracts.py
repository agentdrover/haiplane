"""Bound wire types for explicit roadmap orders and their steward decisions."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

PositiveId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
CheckId = Annotated[str, Field(pattern=r"^AC-[0-9]{1,16}$")]


class RoadmapBasis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_order_id: PositiveId
    check_id: CheckId


class RoadmapResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommendation: Literal["hold_expansion", "owner_required", "insufficient_facts"]
    basis: RoadmapBasis | None
    risk: Literal["repeat_redeem_failure", "unknown"]


class RoadmapDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_id: PositiveId
