"""A production defect filed in one call (#915, feature #906).

The intake fills what the hub knows and a person under fire should not have
to type: the passport (``found_in='prod'``, ``detected_at``), expedite,
bugfix, and the release the defect showed up in — the newest successful
deploy of the task's project history (``defect_release.project_scope``). It
asks for the minimum the ``incident`` DoR profile requires: what broke, how
to check the fix, where.

It does not open a lane past any gate. The task is filed as a DRAFT whoever
files it, the fields go through the ordinary refine path — so readiness,
the culprit hypothesis (#917) and the DoR autopilot run exactly as they do
for any other draft — and approval stays where the project policy puts it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite

from hub import repository as repo
from hub.models import (
    AcceptanceCriterion,
    ACVerifiableBy,
    ClassOfService,
    DefectFoundIn,
    ProdDefectCreate,
    ProdDefectFiled,
    TaskCreate,
    TaskRefine,
    TaskSource,
    WipTag,
    WorkType,
)
from hub.services.defect_release import latest_release_in_scope

REASON_NO_RELEASE = "релиз не привязан: у проекта нет записанного успешного выката"


def _now() -> str:
    # Same TEXT form as SQLite's datetime('now'): stamps are compared as text.
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _create_body(body: ProdDefectCreate, source: TaskSource) -> TaskCreate:
    return TaskCreate(
        title=body.title,
        description=body.broken,
        parent_id=body.parent_id,
        source=source,
        agent=body.agent,
        work_type=WorkType.incident,
        class_of_service=ClassOfService.expedite,
        wip_tag=WipTag.bugfix,
        problem_statement=body.broken,
        affected_areas=body.affected_areas,
        validation_commands=[body.verify],
    )


def _passport(body: ProdDefectCreate, release_id: int | None) -> TaskRefine:
    fields: dict = {
        "found_in": DefectFoundIn.prod,
        "detected_at": _now(),
        "acceptance_criteria": [
            AcceptanceCriterion(
                id="AC-1",
                given=f"прод-дефект: {body.title}"[:500],
                when="починка выкачена",
                then=body.verify,
                verifiable_by=ACVerifiableBy.manual,
            )
        ],
    }
    if release_id is not None:
        fields["release_id"] = release_id
    return TaskRefine(**fields)


async def file_prod_defect(
    db: aiosqlite.Connection, body: ProdDefectCreate, *, source: TaskSource
) -> ProdDefectFiled:
    """Create the draft, fill its passport, bind the release or say why not."""
    from hub.services.lifecycle import create_task, enrich_task_view, row_to_task
    from hub.services.refinement import refine_task

    created = await create_task(db, _create_body(body, source), force_draft=True)
    task_id = created.task.id
    project = await repo.resolve_project_for_task(db, task_id)
    release = await latest_release_in_scope(
        db, int(project["id"]) if project is not None else None
    )
    release_id = int(release["id"]) if release else None
    await refine_task(db, task_id, _passport(body, release_id))
    reason = "" if release_id is not None else REASON_NO_RELEASE
    if reason:
        await repo.add_task_update(db, task_id, body.agent, "status", reason)
        await db.commit()

    row = await repo.get_task(db, task_id)
    if row is None:  # created above in this call; gone only if deleted meanwhile
        raise LookupError(f"task #{task_id} disappeared while being filed")
    view = row_to_task(row, updates=await repo.get_task_updates(db, task_id))
    return ProdDefectFiled(
        task=await enrich_task_view(db, view),
        release_id=release_id,
        release_reason=reason,
    )
