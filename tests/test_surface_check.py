"""Declared areas vs the branch diff on submit (#550).

The check exists because affected_areas were prose nobody compared with
anything: on #360 two of three call sites were closed and the third — a web
form — was found only when the review harness demanded an enumeration, after
the work had been submitted. At DoR there is nothing to compare against; on
submit the hub has the diff, which is truth rather than a prediction.
"""

from __future__ import annotations

import aiosqlite
import pytest
from fastapi import HTTPException

from hub import commit_scope, config
from hub import repository as repo
from hub import services
from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins
from hub.models import TaskCreate, TaskRefine


async def _running_task_with_areas(
    db: aiosqlite.Connection, areas: list[str] | None
) -> int:
    tv = await services.create_task(db, TaskCreate(title="Surface check"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: do the work")
    await db.commit()
    if areas is not None:
        await repo.update_task_structured(db, tv.id, TaskRefine(affected_areas=areas))
        await db.commit()
    started = await services.pair_start_task(db, tv.id, caller="dev-agent")
    assert started.status.value == "running"
    return tv.id


class _DiffGitOps(NoopGitOps):
    """Stands in for the branch diff; None means "could not be determined".

    Opens a PR when asked: since #967 a submission with a confirmed diff gets
    one from the hub, and a fake that refused would add a "PR could not be
    opened" alert to tests that only want to talk about areas.
    """

    def __init__(self, paths: list[str] | None) -> None:
        self._paths = paths
        self.calls: list[str] = []

    async def branch_diff_paths(self, branch, base_branch=None, repo=None):
        self.calls.append(branch)
        return self._paths

    async def push_branch(self, branch, repo=None, force=False):
        return True

    async def create_pr(
        self,
        task_id,
        title,
        description,
        branch,
        repo=None,
        gh_repo=None,
        base_branch=None,
        forge: str = "",
    ):
        return 41


async def _alerts(db: aiosqlite.Connection, task_id: int) -> list[str]:
    updates = await repo.get_task_updates(db, task_id)
    return [u["content"] for u in updates if u["kind"] == "alert"]


async def test_submit_refuses_undeclared_files_in_require(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """#550 AC-1. The refusal must name the files, not advise checking a field."""
    monkeypatch.setattr(config, "SDD_SURFACES", "require")
    task_id = await _running_task_with_areas(db, ["hub/app.py"])
    plugins.git_ops = _DiffGitOps(["hub/app.py", "tests/test_api.py"])

    with pytest.raises(HTTPException) as excinfo:
        await services.submit_for_review(db, task_id)

    assert excinfo.value.status_code == 422
    assert "tests/test_api.py" in str(excinfo.value.detail)
    assert "hub/app.py" not in str(excinfo.value.detail), (
        "a declared file must not be reported as undeclared"
    )
    assert dict(await repo.get_task(db, task_id))["status"] == "running", (
        "a refused submission must leave the task where it was"
    )


async def test_submit_passes_when_areas_cover_the_diff(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """#550 AC-2."""
    monkeypatch.setattr(config, "SDD_SURFACES", "require")
    task_id = await _running_task_with_areas(db, ["hub", "tests"])
    plugins.git_ops = _DiffGitOps(["hub/app.py", "tests/test_api.py"])

    view = await services.submit_for_review(db, task_id)

    assert view.status.value == "review"
    assert not await _alerts(db, task_id)


async def test_submit_area_check_unavailable_is_unknown(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """#550 AC-3. An unreadable diff is not agreement.

    #506 shipped a guard that treated an unavailable environment as an absent
    problem; saying nothing here would repeat it, because a silent submission
    reads as a submission that passed the check.
    """
    monkeypatch.setattr(config, "SDD_SURFACES", "require")
    task_id = await _running_task_with_areas(db, ["hub/app.py"])
    plugins.git_ops = _DiffGitOps(None)  # diff could not be determined

    view = await services.submit_for_review(db, task_id)

    assert view.status.value == "review", "unknown must not be a false refusal"
    alerts = await _alerts(db, task_id)
    assert any("НЕ выполнялась" in a for a in alerts), (
        "the submission must say the check did not run"
    )


async def test_submit_area_check_warn_does_not_block(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """#550 AC-4. Default mode: recorded with names, never blocking."""
    monkeypatch.setattr(config, "SDD_SURFACES", "warn")
    task_id = await _running_task_with_areas(db, ["hub/app.py"])
    plugins.git_ops = _DiffGitOps(["hub/app.py", "tests/test_api.py"])

    view = await services.submit_for_review(db, task_id)

    assert view.status.value == "review"
    alerts = await _alerts(db, task_id)
    assert any("tests/test_api.py" in a for a in alerts)


async def test_routine_lockfiles_are_not_undeclared(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """A lockfile changes BECAUSE of the work, it is never the point OF it.

    Demanding it in affected_areas would train authors to pad the field until
    it means nothing — the very thing this check exists to prevent.
    """
    monkeypatch.setattr(config, "SDD_SURFACES", "require")
    task_id = await _running_task_with_areas(db, ["hub/app.py"])
    plugins.git_ops = _DiffGitOps(["hub/app.py", "uv.lock"])

    view = await services.submit_for_review(db, task_id)

    assert view.status.value == "review"


async def test_no_declared_areas_is_unknown_not_ok(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """Nothing declared means nothing to compare — say so rather than pass."""
    monkeypatch.setattr(config, "SDD_SURFACES", "require")
    task_id = await _running_task_with_areas(db, None)
    plugins.git_ops = _DiffGitOps(["hub/app.py"])

    view = await services.submit_for_review(db, task_id)

    assert view.status.value == "review"
    assert any("НЕ выполнялась" in a for a in await _alerts(db, task_id))


async def test_off_disables_the_check_entirely(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    monkeypatch.setattr(config, "SDD_SURFACES", "off")
    task_id = await _running_task_with_areas(db, ["hub/app.py"])
    git = _DiffGitOps(["tests/test_api.py"])
    plugins.git_ops = git

    view = await services.submit_for_review(db, task_id)

    assert view.status.value == "review"
    # Since #583 the diff has a second consumer — the risk-class recompute —
    # so "off" no longer means "never fetch the diff". What it still means:
    # the SURFACE check stays silent — no refusal, no undeclared-files alert,
    # even though the diff (tests/test_api.py) sits outside the declared area.
    assert not [a for a in await _alerts(db, task_id) if "Класс риска" not in a]


async def _submission_notes(db: aiosqlite.Connection, task_id: int) -> list[str]:
    updates = await repo.get_task_updates(db, task_id)
    return [u["content"] for u in updates if "Submitted for review" in u["content"]]


async def test_submit_distinguishes_empty_diff_from_unreadable(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """#762 AC-3: the feed must say which of the two happened.

    Until this task both printed "Класс риска по диффу НЕ пересчитан: дифф
    пуст", so a workspace looking at a stale ref was indistinguishable from a
    branch that genuinely changes nothing — and #759 and #756 both read as the
    harmless one while being the other.
    """
    monkeypatch.setattr(config, "SDD_SURFACES", "warn")

    empty_id = await _running_task_with_areas(db, ["hub/app.py"])
    plugins.git_ops = _DiffGitOps([])
    await services.submit_for_review(db, empty_id)
    empty_note = (await _submission_notes(db, empty_id))[-1]

    unreadable_id = await _running_task_with_areas(db, ["hub/app.py"])
    plugins.git_ops = _DiffGitOps(None)
    await services.submit_for_review(db, unreadable_id)
    unreadable_note = (await _submission_notes(db, unreadable_id))[-1]

    assert "ветка не меняет файлов" in empty_note, (
        "an empty diff is an observation about the branch, not a failure"
    )
    assert "НЕ пересчитан" not in empty_note, (
        "nothing failed here — the class was checked and had nothing to change"
    )
    assert "прочитать дифф не удалось" in unreadable_note, (
        "an unreadable diff must name itself as unreadable"
    )
    assert empty_note != unreadable_note


async def test_surfaces_default_mode_stays_warn(monkeypatch) -> None:
    """#854: tightening is an installation's decision, never a new default.

    The check has been in ``warn`` since #550, and the measurement behind #854
    is why that stays: over the 30 days to 2026-08-21, 46 of 104 checkable
    submissions (44%) would have been refused rather than alerted. A default
    that refuses two submissions in five is not a default — it is a migration,
    and it belongs to whoever runs the installation, switched on through the
    environment with the number in hand.
    """
    import importlib
    import os

    monkeypatch.delenv("HAIPLANE_SDD_SURFACES", raising=False)
    fresh = importlib.reload(config)
    try:
        assert fresh.SDD_SURFACES == "warn"
    finally:
        # Leave the module as the rest of the suite expects to find it.
        if "HAIPLANE_SDD_SURFACES" in os.environ:
            del os.environ["HAIPLANE_SDD_SURFACES"]
        importlib.reload(config)


# ---- #890: accepting the actual scope instead of refusing a forecast ----
#
# #854 measured the price of the alternative: 46 of 104 submissions over 30
# days changed files outside their declared areas, and the residue after the
# most generous routine-path allowances was real surfaces, not noise. The
# field is written at DoR as a prediction; work discovers its own scope. What
# review, commit-scope and the risk recompute need is that declared and actual
# agree AT SUBMISSION — so the submitter may accept the truth in one step.


async def test_accepting_actual_areas_records_the_growth(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """#890 AC-1. The field becomes true, and the growth is on the record."""
    from hub.models import TaskSubmitReview

    monkeypatch.setattr(config, "SDD_SURFACES", "warn")
    task_id = await _running_task_with_areas(db, ["hub/app.py"])
    plugins.git_ops = _DiffGitOps(["hub/app.py", "hub/db.py", "tests/test_api.py"])

    view = await services.submit_for_review(
        db, task_id, TaskSubmitReview(accept_areas=True)
    )

    assert view.status.value == "review"
    task = dict(await repo.get_task(db, task_id))
    from hub.db import deserialize_str_list

    areas = deserialize_str_list(task["affected_areas"])
    assert areas == ["hub/app.py", "hub/db.py", "tests/test_api.py"], (
        "the declared field now says what the work actually touched"
    )

    alerts = await _alerts(db, task_id)
    growth = [a for a in alerts if a.startswith(commit_scope.SCOPE_GROWTH_MARKER)]
    assert len(growth) == 1, alerts
    assert "+2" in growth[0], growth[0]
    assert "hub/db.py" in growth[0] and "tests/test_api.py" in growth[0]
    # The point of the event: the reviewer can tell a fact from a forecast.
    assert "признание факта" in growth[0]
    assert not any("Вне объявленной области" in a for a in alerts), (
        "accepted scope is not also reported as an unresolved divergence"
    )


async def test_areas_are_never_accepted_implicitly(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """#890 AC-2. Without the explicit flag nothing changes — in either mode.

    A hub that widened the field on its own would leave affected_areas always
    equal to the diff, and then there would be nothing left to compare.
    """
    from hub.models import TaskSubmitReview

    monkeypatch.setattr(config, "SDD_SURFACES", "warn")
    task_id = await _running_task_with_areas(db, ["hub/app.py"])
    plugins.git_ops = _DiffGitOps(["hub/app.py", "hub/db.py"])

    await services.submit_for_review(db, task_id, TaskSubmitReview())

    from hub.db import deserialize_str_list

    task = dict(await repo.get_task(db, task_id))
    assert deserialize_str_list(task["affected_areas"]) == ["hub/app.py"]
    alerts = await _alerts(db, task_id)
    assert any("Вне объявленной области" in a for a in alerts)
    assert not any(a.startswith(commit_scope.SCOPE_GROWTH_MARKER) for a in alerts)

    # And in require the same submission is still refused, not silently fixed.
    monkeypatch.setattr(config, "SDD_SURFACES", "require")
    other_id = await _running_task_with_areas(db, ["hub/app.py"])
    plugins.git_ops = _DiffGitOps(["hub/app.py", "hub/db.py"])
    with pytest.raises(HTTPException) as exc_info:
        await services.submit_for_review(db, other_id, TaskSubmitReview())
    assert exc_info.value.status_code == 422
    # The refusal now names the one-step way out as well.
    assert "accept_areas" in str(exc_info.value.detail)


async def test_unknown_verdict_cannot_be_accepted_away(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """#890 AC-3. A check that did not run is not a divergence to accept."""
    from hub.models import TaskSubmitReview

    monkeypatch.setattr(config, "SDD_SURFACES", "require")
    task_id = await _running_task_with_areas(db, ["hub/app.py"])
    plugins.git_ops = _DiffGitOps(None)  # diff could not be determined

    view = await services.submit_for_review(
        db, task_id, TaskSubmitReview(accept_areas=True)
    )

    assert view.status.value == "review"
    from hub.db import deserialize_str_list

    task = dict(await repo.get_task(db, task_id))
    assert deserialize_str_list(task["affected_areas"]) == ["hub/app.py"], (
        "nothing was invented from a check that never ran"
    )
    alerts = await _alerts(db, task_id)
    assert any("НЕ выполнялась" in a for a in alerts)
    assert not any(a.startswith(commit_scope.SCOPE_GROWTH_MARKER) for a in alerts)
