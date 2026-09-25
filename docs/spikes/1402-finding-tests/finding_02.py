"""Битый JSON findings не должен выдаваться за «замечаний не было»."""

from __future__ import annotations

import aiosqlite
import pytest
from fastapi import HTTPException

from hub import repository as repo
from hub.services.statement_generation import baseline_if_absent
from hub.services.steward_dispatch import KIND_DOR
from hub.services.steward_dor_applied import apply_dor_judgement


async def test_unreadable_findings_json_is_not_called_empty(
    db: aiosqlite.Connection,
) -> None:
    task_id = await repo.create_task(
        db,
        title="драфт с битым JSON суждения",
        description="постановка",
        runtime="auto",
        source="agent",
        assigned_agent="pda_claude",
        rationale="",
        status="draft",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(db, task_id, dor_passed=1)
    await db.commit()
    generation = await baseline_if_absent(db, task_id)

    inserted = await repo.insert_steward_judgement(
        db,
        task_id=task_id,
        generation=generation,
        kind=KIND_DOR,
        submitted_verdict="changes_requested",
        verdict="changes_requested",
        findings=(
            '[{"title": "AC-2 словом «корректно»",'
            ' "why": "нельзя проверить"'
        ),
        submitted_by="steward",
    )
    await db.commit()
    assert inserted is not None

    with pytest.raises(HTTPException) as refusal:
        await apply_dor_judgement(db, task_id, generation)

    assert refusal.value.status_code in (409, 422)
    detail = str(refusal.value.detail)
    assert "без единого замечания" not in detail
    lowered = detail.lower()
    assert (
        "json" in lowered
        or "формат" in lowered
        or "прочитать" in detail
        or "разобр" in lowered
    ), detail
