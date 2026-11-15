"""Записи дельты поведения (#2218).

Отметка ``unspecified|delta|no_delta`` и список записей живут в одном поле.
Модуль отделён от ``hub.models``, чтобы карточка задачи не росла этим контрактом.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class DeltaScenario(BaseModel):
    """Scenario carried by a delta record. Same fields as a spec scenario."""

    given: str
    when: str
    then: str
    verifiable_by: str
    test_ref: str = ""


class BehaviorDeltaRecord(BaseModel):
    """One behavior-delta record. ``added`` is the only op with an empty base."""

    requirement_id: str = Field(pattern=r"^REQ-[0-9]+$")
    op: Literal["added", "modified", "removed", "renamed"]
    base_spec_revision: str = ""
    new_requirement_id: str = ""
    obligation: str = Field(min_length=1)
    scenarios: list[DeltaScenario] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> BehaviorDeltaRecord:
        if self.op != "added" and not self.base_spec_revision.strip():
            raise ValueError("base_spec_revision is required when op is not added")
        if self.op == "renamed":
            if not re.fullmatch(r"^REQ-[0-9]+$", self.new_requirement_id):
                raise ValueError("renamed requires new_requirement_id")
        elif self.new_requirement_id:
            raise ValueError("new_requirement_id belongs only to renamed")
        for scenario in self.scenarios:
            if scenario.verifiable_by == "test" and not scenario.test_ref.strip():
                raise ValueError("test_ref is required when verifiable_by is test")
        return self


class BehaviorSnapshotRecord(BaseModel):
    """A frozen delta record: the draft plus the hash captured at submit."""

    requirement_id: str
    op: str
    content_hash: str
    base_spec_revision: str = ""
    new_requirement_id: str = ""
    obligation: str = ""
    scenarios: list[DeltaScenario] = Field(default_factory=list)


class SpecCheck(BaseModel):
    """The diff of ``specs/`` against the snapshot, bound to one head (#2220).

    ``matched``: blocks at ``head_sha`` equal the snapshot, measured from
    ``base_sha``. ``unchecked``: the hub could not read the commit; the reason
    says why. The check covers ``head_sha`` only: a later commit is not covered.
    """

    state: Literal["matched", "unchecked"]
    head_sha: str = ""
    base_sha: str = ""
    reason: str = ""


class BehaviorSnapshot(BaseModel):
    """The delta frozen on a submission: generation, head and records."""

    generation: int
    head_sha: str
    records: list[BehaviorSnapshotRecord] = Field(default_factory=list)
    explicit_no_delta: bool = False
    spec_check: SpecCheck | None = None


def coerce_behavior_delta(value: Any) -> Any:
    """Blank is ``unspecified``. A JSON array is the record list, not a mark."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return "unspecified"
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                return text
            if isinstance(parsed, list):
                return parsed
        return text
    return value


def reject_repeated_delta_ids(records: list[BehaviorDeltaRecord]) -> None:
    """One id, once. An empty list is not ``no_delta``."""
    if not records:
        raise ValueError("behavior_delta has no records; use no_delta")
    seen: set[str] = set()
    for rec in records:
        for ident in (rec.requirement_id, rec.new_requirement_id):
            if not ident:
                continue
            if ident in seen:
                raise ValueError(f"requirement id {ident} is listed twice")
            seen.add(ident)
