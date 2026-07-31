"""Bulk refine binds projects, and the project filter survives pagination (#370).

K5 — ``project`` is a virtual refine field: ``structured_fields_to_db`` drops
it, and single refine handles it in a branch of its own. Bulk refine never had
that branch, so the binding silently did nothing while ``fields_set`` still
listed ``project`` as applied. The caller was told the write landed.

K6 — the project filter ran in Python *after* the SQL LIMIT, so it discarded
rows the page had already spent. A page whose top ``limit+1`` rows belonged to
other projects came back empty with ``next_cursor=null`` — indistinguishable
from "this project has no tasks".
"""

from __future__ import annotations

import aiosqlite
import pytest

from hub import repository as repo
from hub import services
from hub.models import BulkRefine, BulkRefineItem, TaskCreate
from hub.services.refinement import ProjectBindError


def _ids(page: dict) -> list[int]:
    """A paged call returns dicts, not TaskView."""
    return [t["id"] for t in page["tasks"]]


async def _project(db: aiosqlite.Connection, slug: str = "alpha") -> int:
    row = await repo.create_project(db, slug=slug, name=slug.title())
    await db.commit()
    return row["id"] if not isinstance(row, int) else row


async def _epic(db: aiosqlite.Connection, title: str = "E") -> int:
    tv = await services.create_task(db, TaskCreate(title=title, task_type="epic"))
    await db.commit()
    return tv.id


# --- K5 ---------------------------------------------------------------------


async def test_bulk_refine_actually_binds_the_project(db: aiosqlite.Connection):
    """AC-1. Before the fix: fields_set reported ['project'] while project_id
    stayed NULL."""
    project_id = await _project(db)
    epic_id = await _epic(db)

    result = await services.refine_tasks_bulk(
        db, BulkRefine(items=[BulkRefineItem(task_id=epic_id, project="alpha")])
    )

    assert result.results[0].fields_set == ["project"]
    row = dict(await repo.get_task(db, epic_id))
    assert row["project_id"] == project_id, (
        "reporting the field as applied while writing nothing is the defect"
    )


async def test_bulk_refine_rejects_an_unknown_project(db: aiosqlite.Connection):
    """The same three checks single refine makes — bulk must not be the looser
    door into the same column."""
    epic_id = await _epic(db)

    with pytest.raises(ProjectBindError):
        await services.refine_tasks_bulk(
            db, BulkRefine(items=[BulkRefineItem(task_id=epic_id, project="nope")])
        )


async def test_bulk_refine_rejects_a_project_on_a_non_epic(db: aiosqlite.Connection):
    await _project(db)
    tv = await services.create_task(db, TaskCreate(title="plain task"))
    await db.commit()

    with pytest.raises(ProjectBindError):
        await services.refine_tasks_bulk(
            db, BulkRefine(items=[BulkRefineItem(task_id=tv.id, project="alpha")])
        )


async def test_a_rejected_binding_rolls_back_the_whole_batch(
    db: aiosqlite.Connection,
):
    """refine_tasks_bulk promises all-or-nothing. The new check runs inside the
    same savepoint, so a bad slug on the second item must undo the first."""
    await _project(db)
    first = await _epic(db, "first")
    second = await _epic(db, "second")

    with pytest.raises(ProjectBindError):
        await services.refine_tasks_bulk(
            db,
            BulkRefine(
                items=[
                    BulkRefineItem(task_id=first, user_story="written by the batch"),
                    BulkRefineItem(task_id=second, project="nope"),
                ]
            ),
        )

    assert dict(await repo.get_task(db, first))["user_story"] == ""


# --- K6 ---------------------------------------------------------------------


async def _project_with_buried_task(db: aiosqlite.Connection, decoys: int) -> int:
    """One task in project 'alpha', then N tasks of other projects created
    after it — so the newer ids sort above it and fill the first page."""
    project_id = await _project(db)
    mine = await _epic(db, "belongs to alpha")
    await repo.update_task(db, mine, project_id=project_id)
    await db.commit()
    for i in range(decoys):
        await services.create_task(db, TaskCreate(title=f"other {i}", task_type="epic"))
    await db.commit()
    return mine


async def test_project_filter_finds_a_task_buried_under_a_full_page(
    db: aiosqlite.Connection,
):
    """AC-2. Before the fix this returned 0 tasks and next_cursor=None while
    the project demonstrably had one."""
    mine = await _project_with_buried_task(db, decoys=10)

    page = await services.list_tasks(db, project="alpha", limit=5, after_id=0)

    assert _ids(page) == [mine]


async def test_project_filter_pages_without_losing_tasks(db: aiosqlite.Connection):
    """Walking the cursor returns every task of the project exactly once, even
    though tasks of other projects are interleaved."""
    project_id = await _project(db)
    mine: list[int] = []
    for i in range(7):
        epic_id = await _epic(db, f"alpha {i}")
        await repo.update_task(db, epic_id, project_id=project_id)
        mine.append(epic_id)
        for j in range(3):
            await services.create_task(
                db, TaskCreate(title=f"other {i}.{j}", task_type="epic")
            )
    await db.commit()

    seen: list[int] = []
    cursor: int | None = 0
    while cursor is not None:
        page = await services.list_tasks(db, project="alpha", limit=2, after_id=cursor)
        seen.extend(_ids(page))
        cursor = page["next_cursor"]

    assert sorted(seen) == sorted(mine)
    assert len(seen) == len(set(seen)), "a paged walk must not repeat a task"


async def test_paging_without_a_project_filter_is_unchanged(
    db: aiosqlite.Connection,
):
    """The task's constraint. Moving the filter into SQL touches a query every
    board call goes through."""
    for i in range(5):
        await services.create_task(db, TaskCreate(title=f"t{i}"))
    await db.commit()

    seen: list[int] = []
    cursor: int | None = 0
    while cursor is not None:
        page = await services.list_tasks(db, limit=2, after_id=cursor)
        seen.extend(_ids(page))
        cursor = page["next_cursor"]

    assert len(seen) == 5
    assert len(seen) == len(set(seen))


async def test_descendants_inherit_the_project_filter(db: aiosqlite.Connection):
    """The project sits on the epic; children are found through it. The SQL
    filter has to keep that subtree rule, not just match project_id."""
    project_id = await _project(db)
    epic_id = await _epic(db, "root epic")
    await repo.update_task(db, epic_id, project_id=project_id)
    await db.commit()
    child = await services.create_task(
        db, TaskCreate(title="feature", task_type="feature", parent_id=epic_id)
    )
    await db.commit()

    page = await services.list_tasks(db, project="alpha", limit=50, after_id=0)

    assert child.id in _ids(page)
