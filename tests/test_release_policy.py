"""Carrying develop into main by policy, not by hand (#812).

One session repeated the same four steps six times on 21.08.2026 — open the
release PR, wait for CI, merge, wait for the deploy job — and none of them
holds a decision. Two facts from those releases are pinned here: a release
takes develop whole, so its body must name everything it carries; and turning
this on is a project decision, so manual stays the default.
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from hub import poller
from hub import repository as repo
from hub import services
from hub.integrations.noop import NoopGitOps
from hub.integrations.protocols import (
    CIProbeOutcome,
    CIProbeResult,
    MergeabilityOutcome,
)
from hub.integrations.registry import plugins
from hub.models import TaskCreate, TaskReviewVerdict, TaskUpdateCreate
from hub.services.release import release_body


def _git(*, ci: CIProbeOutcome = CIProbeOutcome.passed, existing_pr: int | None = None):
    g = NoopGitOps()
    g.check_pr_ci = AsyncMock(return_value=CIProbeResult(ci, f"checks_{ci.value}"))
    g.merge_pr = AsyncMock(return_value=True)
    g.merge_commit_sha = AsyncMock(return_value="release0merge0sha")
    g.pull_main = AsyncMock(return_value=True)
    g.delete_branch = AsyncMock(return_value=True)
    g.pr_state = AsyncMock(return_value="open")
    g.pr_for_branch = AsyncMock(return_value=existing_pr)
    g.release_range = AsyncMock(
        return_value=[
            "feat(task): live-check evidence (#813)",
            "fix(task): telemetry reason slug (#809)",
            "chore: bump nothing in particular",
        ]
    )
    # #968: the release asks about CONTENT before it asks about commits, so a
    # fake that answers only the range would make every case read as "could
    # not compare". True here means what these tests assume: develop carries
    # work main does not.
    g.content_differs = AsyncMock(return_value=True)
    # #969: the release ends by returning the release branch into the
    # integration branch. A fake that leaves this question to the noop makes
    # every case here read as "could not ask" — the same half-substituted
    # harness that cost #968 seven tests. These tests are about other things,
    # so the answer is the quiet one: there was nothing to return.
    g.return_release_into_base = AsyncMock(return_value=("nothing", "уже содержит"))
    # #970: зелёный CI больше не разрешение мержить — релиз отдельно
    # спрашивает, сливается ли PR. Незаявленный метод ушёл бы в noop, и каждый
    # тест здесь молча читался бы как «не смог спросить у GitHub».
    g.check_pr_mergeable = AsyncMock(
        return_value=(MergeabilityOutcome.mergeable, "clean")
    )
    g.open_release_pr = AsyncMock(return_value=777)
    plugins.git_ops = g
    return g


async def _release_project(db: aiosqlite.Connection, mode: str) -> int:
    """A project whose release policy is auto, manual, or unset."""
    pid = await repo.create_project(db, slug="shipper", name="Shipper")
    policy = json.dumps({"release": mode}) if mode else "{}"
    await repo.update_project(db, pid, gate_policy=policy)
    await db.commit()
    return pid


async def _delivered_task(db: aiosqlite.Connection, project_id: int) -> int:
    """A pair task walked to the point where the delivery gate merges it."""
    tv = await services.create_task(db, TaskCreate(title="Ship me"))
    await repo.update_task(db, tv.id, project_id=project_id)
    await db.commit()
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: build")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")
    await repo.update_task(db, tv.id, pr_number=77, branch="task-x/y")
    await db.commit()
    await services.submit_for_review(db, tv.id)
    await services.record_review_verdict(
        db, tv.id, TaskReviewVerdict(verdict="approved", agent="reviewer")
    )
    return tv.id


async def _report_done(db: aiosqlite.Connection, task_id: int) -> None:
    await services.add_update(
        db,
        task_id,
        TaskUpdateCreate(agent="dev", kind="done", content="done, delivered"),
    )


# ---- AC-1: the body names everything the release carries ----


async def test_release_pr_names_everything_it_carries(db: aiosqlite.Connection):
    g = _git()
    pid = await _release_project(db, "auto")
    task_id = await _delivered_task(db, pid)

    await _report_done(db, task_id)

    assert g.open_release_pr.await_count == 1
    body = g.open_release_pr.await_args.args[3]
    assert "#813" in body and "#809" in body, (
        "a release carries develop whole, including other sessions' work — "
        "naming one task while shipping three is a record that lies"
    )
    assert "chore: bump nothing in particular" in body, (
        "a commit without a task number still ships, so it is still listed"
    )
    feed = " ".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )
    assert "#777" in feed and "#813" in feed


def test_release_body_reads_task_numbers_out_of_subjects():
    body, ids = release_body(
        [
            "feat(task): something (#101)",
            "fix: no number here",
            "feat(task): another (#102)",
        ]
    )
    assert ids == [101, 102]
    assert "no number here" in body, "unnumbered commits are shown, not hidden"
    assert "depends_on" in body, (
        "the body says out loud that order between tasks is not understood here"
    )


# ---- AC-2: green CI releases and the fact is recorded ----


async def test_green_release_is_merged_and_recorded(db: aiosqlite.Connection):
    g = _git(existing_pr=777)
    pid = await _release_project(db, "auto")
    from hub.services.release import merge_ready_release

    project = await repo.get_project(db, pid)
    merged, reason = await merge_ready_release(db, project)

    assert merged is True
    assert "777" in reason
    assert g.merge_pr.await_args.args[0] == 777


# ---- AC-3: a red CI never releases, and says so once ----


async def test_red_ci_never_releases(db: aiosqlite.Connection):
    g = _git(ci=CIProbeOutcome.failed, existing_pr=777)
    pid = await _release_project(db, "auto")
    from hub.services.release import merge_ready_release

    project = await repo.get_project(db, pid)
    merged, reason = await merge_ready_release(db, project)

    assert merged is False
    assert "ci_fail" in reason, "a refusal names its cause"
    g.merge_pr.assert_not_awaited()

    # The same refusal on the next sweep must be the same string, so the
    # poller can recognise it as already reported instead of repeating it.
    _, again = await merge_ready_release(db, project)
    assert again == reason


# ---- AC-4: a manual project is untouched ----


async def test_manual_project_is_untouched(db: aiosqlite.Connection):
    g = _git()
    pid = await _release_project(db, "manual")
    task_id = await _delivered_task(db, pid)

    await _report_done(db, task_id)

    g.open_release_pr.assert_not_awaited()
    g.release_range.assert_not_awaited()

    from hub.services.release import merge_ready_release

    project = await repo.get_project(db, pid)
    merged, reason = await merge_ready_release(db, project)
    assert (merged, reason) == (False, "")
    # The task's own PR is still delivered by the gate — that is the existing
    # behaviour and not what this task changes. What must not happen is a
    # release merge: no call carries the release PR number.
    merged_prs = {call.args[0] for call in g.merge_pr.await_args_list}
    assert 777 not in merged_prs, "a manual project releases when its owner says so"


async def test_unset_policy_reads_as_manual(db: aiosqlite.Connection):
    """An unreadable or absent policy must never ship code (#743's rule)."""
    g = _git()
    pid = await _release_project(db, "")
    task_id = await _delivered_task(db, pid)

    await _report_done(db, task_id)

    g.open_release_pr.assert_not_awaited()


# ---- AC-5: two deliveries share one release PR ----


async def test_release_pr_creation_is_idempotent(db: aiosqlite.Connection):
    g = _git()
    pid = await _release_project(db, "auto")

    first = await _delivered_task(db, pid)
    await _report_done(db, first)
    second = await _delivered_task(db, pid)
    await repo.update_task(db, second, pr_number=78)
    await db.commit()
    await _report_done(db, second)

    assert g.open_release_pr.await_count == 2, "both deliveries refresh the release"
    # The upsert itself decides between create and edit; what matters here is
    # that the caller never asks for a second PR over the same range.
    heads = {call.args[1] for call in g.open_release_pr.await_args_list}
    bases = {call.args[0] for call in g.open_release_pr.await_args_list}
    assert len(heads) == 1 and len(bases) == 1, (
        "one range, one release — a second PR would split one release into two "
        "stories about the same commits"
    )


# ---- #931: the release is a state of the project, not an event of delivery ----
#
# On 22.08.2026, minutes after release=auto was switched on, develop was two
# tasks ahead of main (#929 and #879, both delivered before the policy) and
# nothing moved them: the release PR was only ever opened at the end of a
# successful delivery, and the poller, finding no open PR, returned an empty
# reason — no PR and no word about why. The same dead end followed any failed
# open. These tests pin the poller's own pass: unshipped tail — release PR.


@pytest.fixture(autouse=True)
def _fresh_sweep_state():
    """The dedup memory of the sweep is per process; tests must not share it."""
    poller._release_notices.clear()
    poller._release_stalls.clear()
    yield
    poller._release_notices.clear()
    poller._release_stalls.clear()


async def _release_activity(db: aiosqlite.Connection) -> list[dict]:
    cur = await db.execute(
        "SELECT summary, detail FROM activity_log WHERE kind='release' ORDER BY id"
    )
    return [dict(r) for r in await cur.fetchall()]


async def test_poller_opens_release_for_an_unshipped_tail(db: aiosqlite.Connection):
    # AC-1: develop ahead of main, no open release PR, no new delivery — one
    # poller cycle opens the PR and its body names everything the range carries.
    g = _git(existing_pr=None)
    pid = await _release_project(db, "auto")
    tail = await _delivered_task(db, pid)  # merged into develop, never released
    g.release_range = AsyncMock(
        return_value=[
            f"feat(task): the tail nobody shipped (#{tail})",
            "feat(task): another session's work (#879)",
        ]
    )

    await poller._sweep_release_policy(db)

    assert g.open_release_pr.await_count == 1, (
        "the tail must not wait for the next delivery to carry it out"
    )
    body = g.open_release_pr.await_args.args[3]
    assert f"#{tail}" in body and "#879" in body, "the body names the whole range"
    # A PR opened this second has no CI to judge; the next cycle does that.
    g.merge_pr.assert_not_awaited()

    feed = " ".join(dict(u)["content"] for u in await repo.get_task_updates(db, tail))
    assert "#777" in feed, "a release with no trigger task still reaches the feeds"
    entries = await _release_activity(db)
    assert len(entries) == 1 and "#777" in entries[0]["summary"], (
        "opened by the poller and visible in the hub, not only in server logs"
    )


async def test_poller_and_delivery_do_not_open_two_release_prs(
    db: aiosqlite.Connection,
):
    # AC-2: the poller's pass and a delivery finishing at the same moment are
    # the race this opens up. Both must land on one PR over one range — a
    # second one would split a single release into two stories about the same
    # commits.
    g = _git(existing_pr=None)
    pid = await _release_project(db, "auto")
    task_id = await _delivered_task(db, pid)

    await asyncio.gather(
        poller._sweep_release_policy(db),
        _report_done(db, task_id),
    )

    assert g.open_release_pr.await_count == 2, "both paths asked; neither was skipped"
    bases = {call.args[0] for call in g.open_release_pr.await_args_list}
    heads = {call.args[1] for call in g.open_release_pr.await_args_list}
    assert len(bases) == 1 and len(heads) == 1, (
        "one range — the upsert behind both calls then yields one PR"
    )
    numbers = {call.args[0] for call in g.merge_pr.await_args_list}
    assert numbers <= {77}, "the release PR itself is not merged on the cycle it opens"


async def test_empty_range_stays_silent(db: aiosqlite.Connection, caplog):
    # AC-3: develop and main agree — nothing to release. Silence, not a PR and
    # not a line per cycle: a line per cycle is how a real signal gets muted.
    g = _git(existing_pr=None)
    # Both ways of saying "nothing to release": no differing content (#968)
    # and, behind it, an empty range.
    g.content_differs = AsyncMock(return_value=False)
    g.release_range = AsyncMock(return_value=[])
    await _release_project(db, "auto")

    with caplog.at_level(logging.INFO, logger="hub"):
        for _ in range(3):
            await poller._sweep_release_policy(db)

    g.open_release_pr.assert_not_awaited()
    assert await _release_activity(db) == []
    assert [r.getMessage() for r in caplog.records if "релиз" in r.getMessage()] == []


async def test_manual_tail_is_untouched(db: aiosqlite.Connection):
    # AC-4: a project that releases by hand is not even asked what its range
    # is — the policy check comes before any question to git.
    g = _git(existing_pr=None)
    await _release_project(db, "manual")

    await poller._sweep_release_policy(db)

    g.pr_for_branch.assert_not_awaited()
    g.release_range.assert_not_awaited()
    g.open_release_pr.assert_not_awaited()


async def test_failed_open_is_reported_once(db: aiosqlite.Connection, caplog):
    # AC-5: GitHub refusing the pull request is a cause, named once per
    # project the way a red CI is — and a refusal that holds reaches the feed
    # once, so the tail does not sit unshipped in silence again (#962).
    g = _git(existing_pr=None)
    g.open_release_pr = AsyncMock(return_value=None)
    await _release_project(db, "auto")

    with caplog.at_level(logging.WARNING, logger="hub"):
        for _ in range(poller.RELEASE_STALL_CYCLES + 2):
            await poller._sweep_release_policy(db)

    assert g.open_release_pr.await_count == poller.RELEASE_STALL_CYCLES + 2, (
        "the policy keeps retrying; what is deduplicated is the reporting"
    )
    said = [r.getMessage() for r in caplog.records if "не открыт" in r.getMessage()]
    assert len(said) == 1, f"one line per reason, not per cycle: {said}"
    entries = await _release_activity(db)
    assert len(entries) == 1 and "не открыт" in entries[0]["summary"]


# ---- #970: релиз спрашивает про mergeable, а не узнаёт по отказу ----------
#
# У PR #83 26.08.2026 CI был зелёным — «Ruff and pytest» pass 4m2s, — а
# смержиться он не мог: mergeable=CONFLICTING, mergeStateStatus=DIRTY. Поллер
# каждый цикл звал merge_pr, получал отказ и выдавал «GitHub отказал». Строка
# называет исполнителя, а не причину: конфликт, отозванные права, снятая ветка
# и временная ошибка GitHub лечатся по-разному, а звучат одинаково.


async def test_conflicting_release_names_the_conflict(db: aiosqlite.Connection):
    # AC-1: конфликт узнаётся ДО попытки. merge_pr не зовётся вовсе — стучаться
    # в стену каждый цикл и пересказывать отказ GitHub не то же самое, что
    # знать, что происходит.
    from hub.integrations.protocols import MergeabilityOutcome
    from hub.services.release import merge_ready_release

    g = _git(existing_pr=777)
    g.check_pr_mergeable = AsyncMock(
        return_value=(MergeabilityOutcome.conflicting, "конфликт в hub/db.py")
    )
    pid = await _release_project(db, "auto")
    project = await repo.get_project(db, pid)

    merged, reason = await merge_ready_release(db, project)

    assert merged is False
    assert "hub/db.py" in reason, f"причина обязана вести к месту: {reason!r}"
    assert "отказал" not in reason, (
        f"«GitHub отказал» называет исполнителя, а не причину: {reason!r}"
    )
    g.merge_pr.assert_not_awaited()

    # Тот же отказ на следующем цикле — та же строка, иначе поллер не узнает
    # её как уже доложенную и начнёт писать в ленту заново (#534, #962).
    _, again = await merge_ready_release(db, project)
    assert again == reason


async def test_mergeable_release_still_merges(db: aiosqlite.Connection):
    # AC-2: новая проверка не должна стать ещё одним гейтом. Зелёный CI плюс
    # MERGEABLE — релиз идёт, как шёл.
    from hub.integrations.protocols import MergeabilityOutcome
    from hub.services.release import merge_ready_release

    g = _git(existing_pr=777)
    g.check_pr_mergeable = AsyncMock(
        return_value=(MergeabilityOutcome.mergeable, "clean")
    )
    pid = await _release_project(db, "auto")
    project = await repo.get_project(db, pid)

    merged, reason = await merge_ready_release(db, project)

    assert merged is True, reason
    g.merge_pr.assert_awaited()


async def test_unknown_mergeability_is_not_a_conflict(db: aiosqlite.Connection):
    # AC-3: GitHub считает mergeability асинхронно и на свежем PR несколько
    # секунд честно отвечает UNKNOWN. Принять это за конфликт — завести ложную
    # тревогу на КАЖДОМ только что открытом релизе; это класс #725 с другой
    # стороны. Ответ переспрашивается следующим циклом, как уже устроено с CI.
    from hub.integrations.protocols import MergeabilityOutcome
    from hub.services.release import merge_ready_release

    g = _git(existing_pr=777)
    g.check_pr_mergeable = AsyncMock(
        return_value=(MergeabilityOutcome.unknown, "GitHub ещё считает")
    )
    pid = await _release_project(db, "auto")
    project = await repo.get_project(db, pid)

    merged, reason = await merge_ready_release(db, project)

    assert merged is False
    assert "конфликт" not in reason.lower(), (
        f"«ещё не посчитано» — это не диагноз: {reason!r}"
    )
    assert reason, "и не тишина: причина названа, чтобы стойло было видно"
    g.merge_pr.assert_not_awaited()


async def test_an_unaskable_github_is_not_a_conflict_either(
    db: aiosqlite.Connection,
):
    # Граница того же: gh, который не ответил, — третий случай, а не конфликт
    # и не разрешение мержить. Разные причины лечатся разными руками.
    from hub.integrations.protocols import MergeabilityOutcome
    from hub.services.release import merge_ready_release

    g = _git(existing_pr=777)
    g.check_pr_mergeable = AsyncMock(
        return_value=(MergeabilityOutcome.unavailable, "gh молчит")
    )
    pid = await _release_project(db, "auto")
    project = await repo.get_project(db, pid)

    merged, reason = await merge_ready_release(db, project)

    assert merged is False
    assert "конфликт" not in reason.lower(), reason
    assert "gh молчит" in reason
    g.merge_pr.assert_not_awaited()


async def test_a_red_ci_is_answered_before_mergeability_is_asked(
    db: aiosqlite.Connection,
):
    # Порядок проверок: красный CI отвечает первым и один вопрос к GitHub
    # экономится. Это не оптимизация ради оптимизации — причина стойла должна
    # называть то, что случилось раньше, иначе владелец чинит не то.
    from hub.services.release import merge_ready_release

    g = _git(ci=CIProbeOutcome.failed, existing_pr=777)
    g.check_pr_mergeable = AsyncMock()
    pid = await _release_project(db, "auto")
    project = await repo.get_project(db, pid)

    merged, reason = await merge_ready_release(db, project)

    assert merged is False
    assert "ci_fail" in reason
    g.check_pr_mergeable.assert_not_awaited()
