"""The locator policy applies to every AC write path, not just bulk refine (#596).

``validate_test_locators`` guarded only ``refine_task``'s ``acceptance_criteria``
payload. The three single-AC paths — add, upsert, replace — never called it, so
with ``SDD_AC_LOCATOR=require`` an unresolvable locator was accepted in silence
and only surfaced later as ``missing`` in a review brief.

Measured on production when this was found: of 912 acceptance criteria with
``verifiable_by=test``, 695 carried a locator and 425 of those were
unresolvable. The mechanisms built on locators — the existence check (#506) and
the test run (#507) — were therefore operating on 39% of the criteria while
reporting nothing about the rest.

Same shape as the limits in #366 and the raw_count reconciliation in #519: a
rule placed on the batch path and forgotten on the single one.
"""

from __future__ import annotations

import aiosqlite
import pytest
from fastapi import HTTPException

from hub import config
from hub import repository as repo
from hub import services
from hub.models import AcceptanceCriterion, TaskCreate, TaskRefine

BAD = "tests/a.py::test_x, tests/a.py::test_y"
GOOD = "tests/test_ac_locator_gate.py::test_add_rejects_an_unresolvable_locator"


def _ac(idx: int = 1, ref: str = BAD) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=f"AC-{idx}",
        given="a criterion bound to a test",
        when="it is written through some path",
        then="the locator policy decides",
        verifiable_by="test",
        test_ref=ref,
    )


@pytest.fixture
def require(monkeypatch):
    monkeypatch.setattr(config, "SDD_AC_LOCATOR", "require")


async def _task(db: aiosqlite.Connection) -> int:
    tv = await services.create_task(db, TaskCreate(title="t"))
    await db.commit()
    return tv.id


# --- AC-1: every write path refuses ----------------------------------------


async def test_add_rejects_an_unresolvable_locator(db: aiosqlite.Connection, require):
    task_id = await _task(db)

    with pytest.raises(HTTPException) as exc:
        await services.add_acceptance_criterion(db, task_id, _ac())

    assert exc.value.status_code == 422
    assert not await repo.list_acceptance_criteria(db, task_id), (
        "a refused write must leave nothing behind"
    )


async def test_upsert_rejects_an_unresolvable_locator(
    db: aiosqlite.Connection, require
):
    task_id = await _task(db)

    with pytest.raises(HTTPException) as exc:
        await services.upsert_acceptance_criterion(db, task_id, _ac())

    assert exc.value.status_code == 422
    assert not await repo.list_acceptance_criteria(db, task_id)


async def test_replace_rejects_an_unresolvable_locator(
    db: aiosqlite.Connection, require
):
    task_id = await _task(db)
    await services.add_acceptance_criterion(db, task_id, _ac(1, GOOD))

    with pytest.raises(HTTPException) as exc:
        await services.replace_acceptance_criteria(db, task_id, [_ac(2)])

    assert exc.value.status_code == 422
    rows = await repo.list_acceptance_criteria(db, task_id)
    assert [dict(r)["ac_id"] for r in rows] == ["AC-1"], (
        "the existing criteria must survive a refused replace"
    )


async def test_a_valid_locator_still_goes_through_every_path(
    db: aiosqlite.Connection, require
):
    """The gate must refuse malformed input, not the work itself."""
    task_id = await _task(db)

    await services.add_acceptance_criterion(db, task_id, _ac(1, GOOD))
    await services.upsert_acceptance_criterion(db, task_id, _ac(2, GOOD))
    await services.replace_acceptance_criteria(db, task_id, [_ac(3, GOOD)])
    await services.refine_task(
        db, task_id, TaskRefine(acceptance_criteria=[_ac(4, GOOD)])
    )

    rows = await repo.list_acceptance_criteria(db, task_id)
    assert [dict(r)["ac_id"] for r in rows] == ["AC-4"]


# --- AC-3: the comma-separated near-miss is named --------------------------


async def test_the_refusal_says_a_locator_is_a_single_nodeid(
    db: aiosqlite.Connection, require
):
    """A refusal that only says "no valid locator" reads as if the field were
    empty. The commonest mistake is naming several tests, and the author has
    no way to guess that the field holds exactly one."""
    task_id = await _task(db)

    with pytest.raises(HTTPException) as exc:
        await services.add_acceptance_criterion(db, task_id, _ac())

    detail = str(exc.value.detail)
    assert "ONE" in detail or "one nodeid" in detail
    assert "AC-1" in detail


# --- AC-2: off and warn are untouched, and reads never fail -----------------


@pytest.mark.parametrize("policy", ["off", "warn"])
async def test_off_and_warn_are_unchanged(
    db: aiosqlite.Connection, monkeypatch, policy: str
):
    """This task closes a hole in require. It must not turn into a new gate
    for installations that never opted in."""
    monkeypatch.setattr(config, "SDD_AC_LOCATOR", policy)
    task_id = await _task(db)

    await services.add_acceptance_criterion(db, task_id, _ac(1))
    await services.upsert_acceptance_criterion(db, task_id, _ac(2))
    await services.replace_acceptance_criteria(db, task_id, [_ac(3)])

    rows = await repo.list_acceptance_criteria(db, task_id)
    assert [dict(r)["ac_id"] for r in rows] == ["AC-3"]


async def test_existing_unresolvable_rows_still_read(
    db: aiosqlite.Connection, monkeypatch
):
    """The 425 rows already stored must keep loading with the policy on.

    Enforcing on reads would take out most of the backlog — the gate belongs
    on writes only, and that is the difference between fixing the hole and
    breaking the board."""
    monkeypatch.setattr(config, "SDD_AC_LOCATOR", "off")
    task_id = await _task(db)
    await services.add_acceptance_criterion(db, task_id, _ac(1))
    await db.commit()

    monkeypatch.setattr(config, "SDD_AC_LOCATOR", "require")

    listed = await services.list_acceptance_criteria(db, task_id)
    assert [ac.id for ac in listed] == ["AC-1"]
    assert dict(await repo.get_task(db, task_id))["id"] == task_id


async def test_non_test_criteria_never_need_a_locator(
    db: aiosqlite.Connection, require
):
    """manual / ui_check / log_check criteria have no test to point at."""
    task_id = await _task(db)
    manual = AcceptanceCriterion(
        id="AC-1",
        given="a criterion checked by hand",
        when="nobody wrote a test for it",
        then="no locator is required",
        verifiable_by="manual",
    )

    await services.add_acceptance_criterion(db, task_id, manual)

    assert len(await repo.list_acceptance_criteria(db, task_id)) == 1


# --- the guard against a sixth path ----------------------------------------


def test_every_repository_ac_write_sits_behind_the_gate():
    """Whoever adds the next write path will not remember this rule.

    Submission #1 guarded four paths, found by reading refinement.py. The
    fifth lived in lifecycle.py and wrote acceptance criteria straight through
    the repository — the review caught it, not me, because I enumerated by
    module instead of by repository call.

    So this test enumerates by repository call. It walks the source for every
    AC-write on the repository and asserts the enclosing function also invokes
    the locator guard. A sixth bypass fails here rather than being discovered
    in a brief months later.
    """
    import ast
    from pathlib import Path

    writes = {
        "replace_acceptance_criteria",
        "add_acceptance_criterion",
        "upsert_acceptance_criterion",
    }
    guards = {"_guard_ac_locator", "validate_test_locators"}
    offenders: list[str] = []

    for path in sorted(Path("hub").rglob("*.py")):
        if path.name == "repository.py":
            continue  # the repository IS the write; policy lives above it
        tree = ast.parse(path.read_text(), filename=str(path))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Only repository-level writes count. A route calling
            # services.add_acceptance_criterion is already behind the gate, and
            # matching the bare method name would flag it — turning this guard
            # into noise that gets deleted.
            repo_writes = {
                node.func.attr
                for node in ast.walk(func)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"repo", "repository"}
                and node.func.attr in writes
            }
            called = {
                node.func.attr
                for node in ast.walk(func)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            } | {
                node.func.id
                for node in ast.walk(func)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            if repo_writes and not (called & guards):
                offenders.append(f"{path}::{func.name}")

    assert not offenders, (
        "these functions write acceptance criteria without applying the "
        f"locator policy: {offenders}"
    )


# --- Upper levels: warn, never block (#1032) --------------------------------
#
# The locator gate above runs on WRITES; it checks the form of a locator, not
# whether the test exists. An epic or a feature never submits — it folds up
# when its children finish — so nothing ever asked whether its criteria point
# at a real test. #985 closed with AC-3 naming
# tests/test_api.py::test_worktree_path_only_when_tree_exists, which collects
# zero tests, under sdd_ac_locator=require.


async def _draft(db: aiosqlite.Connection, task_type: str, locator: str) -> int:
    """A draft of ``task_type`` carrying one test-AC with ``locator``."""
    parent_id = None
    if task_type == "feature":
        epic = await services.create_task(
            db, TaskCreate(title="parent epic", task_type="epic")
        )
        parent_id = epic.id
    tv = await services.create_task(
        db,
        TaskCreate(
            title=f"{task_type} probe", task_type=task_type, parent_id=parent_id
        ),
    )
    await repo.upsert_acceptance_criterion(
        db,
        tv.id,
        AcceptanceCriterion(
            id="AC-1",
            given="a criterion",
            when="it is written",
            then="it names a test",
            verifiable_by="test",
            test_ref=locator,
        ),
    )
    await db.execute("UPDATE tasks SET status='draft' WHERE id=?", (tv.id,))
    await db.commit()
    return tv.id


def _collector(found: set[str] | None):
    async def collect(nodeids, repo_path):
        return found

    return collect


async def _approve_with(db, monkeypatch, task_id: int, found: set[str] | None):
    from hub.models import TaskApprove
    from hub.services import ac_tests

    monkeypatch.setattr(ac_tests, "default_locator_collector", _collector(found))
    return await services.approve_task(db, task_id, TaskApprove(force=True))


def _alerts(updates) -> list[str]:
    return [
        dict(u)["content"]
        for u in updates
        if "несуществующие тесты" in dict(u)["content"]
    ]


async def test_epic_approval_warns_on_dead_locator(
    db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#1032): the approval goes through, and the feed names both the
    # criterion and the locator — a warning nobody can act on is not one.
    dead = "tests/test_api.py::test_worktree_path_only_when_tree_exists"
    task_id = await _draft(db, "epic", dead)

    view = await _approve_with(db, monkeypatch, task_id, found=set())

    assert view.status.value == "open", "the warning must not block the approval"
    alerts = _alerts(await repo.get_task_updates(db, task_id))
    assert len(alerts) == 1, "named once, at approval"
    assert "AC-1" in alerts[0] and dead in alerts[0]


async def test_live_locator_is_silent_on_upper_levels(
    db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#1032): only a DEAD locator speaks. A living one, and a criterion
    # with no locator at all, leave the feed alone — a warning on every
    # approval is one nobody reads.
    live = "tests/test_ac_locator_gate.py::test_epic_approval_warns_on_dead_locator"
    task_id = await _draft(db, "feature", live)

    await _approve_with(db, monkeypatch, task_id, found={live})

    assert not _alerts(await repo.get_task_updates(db, task_id))


async def test_unreadable_collection_says_nothing(
    db: aiosqlite.Connection, monkeypatch
):
    # The #725 rule: "could not check" must never be reported as "checked and
    # clean". A collector that could not run produces no verdict at all.
    task_id = await _draft(db, "epic", "tests/test_api.py::test_gone")

    await _approve_with(db, monkeypatch, task_id, found=None)

    assert not _alerts(await repo.get_task_updates(db, task_id))


async def test_task_level_require_is_unchanged(db: aiosqlite.Connection, require):
    # AC-3 (#1032): nothing moved for tasks. With sdd_ac_locator=require an
    # unresolvable locator on a task is still REFUSED at the write, not
    # softened into a warning by this feature.
    task_id = await _task(db)

    with pytest.raises(HTTPException) as exc:
        await services.upsert_acceptance_criterion(db, task_id, _ac())

    assert exc.value.status_code == 422
    assert not await repo.list_acceptance_criteria(db, task_id)
