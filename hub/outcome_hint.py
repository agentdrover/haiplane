"""Contract of the resubmission hint on open findings (#1909)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class OutcomeHintItem(BaseModel):
    """One open finding in the resubmission hint (#1909)."""

    finding_uid: str
    kind: str  # confirmed | unresolved
    title: str = ""
    # The words of THIS finding's own dictionary (#1085), never the other one.
    allowed_outcomes: list[str] = Field(default_factory=list)


class OutcomeHintView(BaseModel):
    """What a resubmission left unanswered, with a ready ``finding_outcomes`` (#1909).

    Set on submit responses only when open findings of the previous report
    remain without an outcome. The submission is still accepted (warn mode).
    """

    count: int = 0
    items: list[OutcomeHintItem] = Field(default_factory=list)
    omitted: int = 0
    example: list[dict[str, str]] = Field(default_factory=list)
    text: str = ""
