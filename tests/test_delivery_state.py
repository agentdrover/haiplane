"""Merged is not running (#497).

On 21.08.2026 task #823 sat ``completed`` with its PR merged into develop while
the deploy job was skipped — deployment runs from main. Nothing in the hub could
tell the two apart; it took reading GitHub's logs. These tests hold the
comparison that closes that gap, and above all the third answer: "we could not
check" must never print as "not deployed".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from unittest.mock import AsyncMock

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.registry import plugins
from hub.services.delivery_state import IN_PROD, NOT_IN_PROD, UNKNOWN, delivery_state


async def _task_merged_at(client: AsyncClient, db, merge_sha: str) -> int:
    """A task the hub merged at ``merge_sha`` — the fact #534 records."""
    task_id = (await client.post("/api/tasks", json={"title": "Delivered?"})).json()[
        "id"
    ]
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
        "VALUES (?, ?, ?, ?)",
        (1, 4000 + task_id, task_id, merge_sha),
    )
    await db.commit()
    return task_id


def _use_real_git(monkeypatch, workspace: str, base_branch: str = "main") -> None:
    """Answer delivery questions from a real repository, all of them.

    Both git questions are wired, not just ancestry: since #946 the state also
    asks which base-branch commit holds what is deployed, and a helper that
    wires half of them would make every squash-released case read as "could
    not tell" for a reason that lives in the test harness.
    """
    from hub import app as hub_app

    real = GitOpsIntegration()
    context = AsyncMock(return_value={"repo": workspace, "base_branch": base_branch})
    monkeypatch.setattr(hub_app.services, "project_git_context", context)
    monkeypatch.setattr(
        "hub.services.orchestration.project_git_context", context, raising=False
    )
    monkeypatch.setattr(plugins.git_ops, "is_ancestor", real.is_ancestor, raising=False)
    monkeypatch.setattr(
        plugins.git_ops,
        "commit_with_same_tree",
        real.commit_with_same_tree,
        raising=False,
    )


async def test_merge_reachable_from_deploy_is_in_prod(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-1 (#497): the merge is in the history of what production runs.
    _use_real_git(monkeypatch, history["repo"])
    task_id = await _task_merged_at(client, db, history["shipped"])
    await repo.record_release(
        db, deployed_sha=history["released"], ref="main", source="ci"
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == IN_PROD, answer
    assert answer["deployed_sha"] == history["released"]
    assert answer["reason"], "even the good news has to say what it is based on"


async def test_merged_but_not_deployed_is_not_in_prod(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-2 (#497): the exact shape of the 21.08 defect — merged, not shipped.
    _use_real_git(monkeypatch, history["repo"])
    task_id = await _task_merged_at(client, db, history["pending"])
    await repo.record_release(
        db, deployed_sha=history["released"], ref="main", source="ci"
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == NOT_IN_PROD, answer
    assert "ждёт релиза" in answer["reason"]


async def test_missing_facts_read_as_unknown_not_denial(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-3 (#497): three answers, never two. Each absence names itself, and
    # none of them may masquerade as "not deployed".
    _use_real_git(monkeypatch, history["repo"])

    no_merge = await delivery_state(
        db,
        (await client.post("/api/tasks", json={"title": "Never merged"})).json()["id"],
    )
    assert no_merge["state"] == UNKNOWN
    assert "не мержил" in no_merge["reason"]

    task_id = await _task_merged_at(client, db, history["shipped"])
    no_release = await delivery_state(db, task_id)
    assert no_release["state"] == UNKNOWN
    assert "незнание, а не отрицание" in no_release["reason"]

    # git that cannot answer: a sha this checkout does not carry.
    await repo.record_release(db, deployed_sha="f" * 40, ref="main", source="ci")
    unreadable = await delivery_state(db, task_id)
    assert unreadable["state"] == UNKNOWN
    assert "не «не раскатано»" in unreadable["reason"]


async def test_card_names_the_delivery_state(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-4 (#497): the answer has to reach the person deciding, in words.
    _use_real_git(monkeypatch, history["repo"])
    task_id = await _task_merged_at(client, db, history["pending"])
    await repo.record_release(
        db, deployed_sha=history["released"], ref="main", source="ci"
    )
    await repo.update_task(db, task_id, status="completed")
    await db.commit()

    page = (await client.get(f"/tasks/{task_id}")).text

    assert "Доставка" in page
    assert "not_in_prod" in page
    assert "ждёт релиза" in page


# ---- #883: git can only answer about objects it has ----
#
# Observed on prod right after #497 shipped: every card answered "could not
# check", because the workspace tracks develop and the deployed commit — made
# on main — was simply not there. The computation was honest and useless.


@pytest.fixture
def clone_missing_the_deploy(tmp_path: Path, history) -> dict[str, str]:
    """A clone that has the merge but NOT the released commit.

    Built by cloning at the older commit and leaving origin behind: exactly
    the prod shape, where the workspace sits on one branch and the deploy came
    from another.
    """
    from tests.conftest import _git_in

    clone = tmp_path / "clone"
    # Two flags, both load-bearing, both found by the precondition below
    # failing: --single-branch, because a plain clone copies every ref; and
    # --no-local, because a clone from a path hardlinks the WHOLE object
    # database and the "missing" commit arrives anyway.
    _git_in(
        tmp_path,
        "clone",
        "--quiet",
        "--no-local",
        "--single-branch",
        "--branch",
        "later",
        history["repo"],
        str(clone),
    )
    return {**history, "clone": str(clone)}


async def test_present_commit_costs_no_network(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-4 (#883): the fetch is a repair, not a routine. A workspace that
    # already carries the commit must not pay for the network on every render.
    _use_real_git(monkeypatch, history["repo"])
    calls: list[str] = []

    async def _refuse_to_fetch(*args, **kwargs):
        calls.append("fetch")
        return (False, "should not have been called")

    monkeypatch.setattr(
        plugins.git_ops, "fetch_commit", _refuse_to_fetch, raising=False
    )
    monkeypatch.setattr(
        plugins.git_ops,
        "commit_exists",
        GitOpsIntegration().commit_exists,
        raising=False,
    )
    task_id = await _task_merged_at(client, db, history["shipped"])
    await repo.record_release(
        db, deployed_sha=history["released"], ref="main", source="ci"
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == IN_PROD
    assert calls == [], "the commit was here — nothing should have been fetched"


async def test_read_path_fetches_once_for_older_releases(
    client: AsyncClient, db: aiosqlite.Connection, clone_missing_the_deploy, monkeypatch
):
    # AC-2 (#883): releases recorded before this task left their commit behind.
    # One repair attempt on read turns "could not check" into a real answer.
    workspace = clone_missing_the_deploy["clone"]
    _use_real_git(monkeypatch, workspace)
    real = GitOpsIntegration()
    for name in ("commit_exists", "fetch_commit"):
        monkeypatch.setattr(plugins.git_ops, name, getattr(real, name), raising=False)
    task_id = await _task_merged_at(client, db, clone_missing_the_deploy["shipped"])
    await repo.record_release(
        db,
        deployed_sha=clone_missing_the_deploy["released"],
        ref="main",
        source="ci",
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] in (IN_PROD, NOT_IN_PROD), answer
    assert answer["state"] == IN_PROD, "the merge is in the released history"


async def test_failed_fetch_stays_unknown_never_denial(
    client: AsyncClient, db: aiosqlite.Connection, clone_missing_the_deploy, monkeypatch
):
    # AC-3 (#883): no network, no answer — and "no answer" must never be
    # spelled "not deployed". This is the same line #839 and #497 hold.
    workspace = clone_missing_the_deploy["clone"]
    _use_real_git(monkeypatch, workspace)
    monkeypatch.setattr(
        plugins.git_ops,
        "commit_exists",
        GitOpsIntegration().commit_exists,
        raising=False,
    )

    async def _no_network(*args, **kwargs):
        return (False, "origin unreachable")

    monkeypatch.setattr(plugins.git_ops, "fetch_commit", _no_network, raising=False)
    task_id = await _task_merged_at(client, db, clone_missing_the_deploy["shipped"])
    await repo.record_release(
        db,
        deployed_sha=clone_missing_the_deploy["released"],
        ref="main",
        source="ci",
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == UNKNOWN
    assert "подтянуть" in answer["reason"]
    assert "origin unreachable" in answer["reason"], "the cause travels to the reader"


async def test_recording_a_deploy_fetches_its_commit(
    db: aiosqlite.Connection, clone_missing_the_deploy, monkeypatch
):
    # AC-1 (#883): the repair happens once per deploy, where the deploy is
    # recorded — not once per card render.
    from hub.services.delivery_state import ensure_commit_available

    workspace = clone_missing_the_deploy["clone"]
    real = GitOpsIntegration()
    for name in ("commit_exists", "fetch_commit"):
        monkeypatch.setattr(plugins.git_ops, name, getattr(real, name), raising=False)
    # The in-memory database has no seeded project, so the row is created
    # rather than updated: an UPDATE that matches nothing fails silently and
    # the helper would answer "no workspace" for a reason unrelated to fetching.
    await db.execute(
        "INSERT INTO projects (slug, name, workspace_path) VALUES (?, ?, ?) "
        "ON CONFLICT(slug) DO UPDATE SET workspace_path = excluded.workspace_path",
        ("default", "Default", workspace),
    )
    await db.commit()
    released = clone_missing_the_deploy["released"]
    assert await real.commit_exists(workspace, released) is False, (
        "fixture precondition"
    )

    assert await ensure_commit_available(db, released, "main") is True
    assert await real.commit_exists(workspace, released) is True


# ---- #937: сетевые вызовы не живут в цикле рендера ----------------------------


@pytest.fixture(autouse=True)
def _clean_fetch_miss_cache():
    from hub.services import delivery_state as ds

    ds._fetch_misses.clear()
    yield
    ds._fetch_misses.clear()


async def test_foreign_project_skips_network(
    client: AsyncClient, db: aiosqlite.Connection, tmp_path: Path, monkeypatch
):
    # AC-1 (#937): задача проекта с чужим repo — ранний unknown, ноль сети.
    from hub import config
    from hub import app as hub_app

    context = AsyncMock(
        return_value={"repo": str(tmp_path), "gh_repo": "agentdrover/Spike_bo"}
    )
    monkeypatch.setattr(hub_app.services, "project_git_context", context)
    monkeypatch.setattr(config, "REPO_NAME", "agentdrover/haiplane")
    exists = AsyncMock(return_value=False)
    fetch = AsyncMock(return_value=(False, "должен остаться невызванным"))
    monkeypatch.setattr(plugins.git_ops, "commit_exists", exists, raising=False)
    monkeypatch.setattr(plugins.git_ops, "fetch_commit", fetch, raising=False)

    task_id = await _task_merged_at(client, db, "a" * 40)
    await repo.record_release(db, deployed_sha="b" * 40, ref="main", source="ci")

    answer = await delivery_state(db, task_id)
    assert answer["state"] == UNKNOWN
    assert "не применим" in answer["reason"]
    exists.assert_not_awaited()
    fetch.assert_not_awaited()


async def test_negative_cache_suppresses_refetch(
    client: AsyncClient, db: aiosqlite.Connection, tmp_path: Path, monkeypatch
):
    # AC-2 (#937): промах fetch_commit не повторяется в пределах TTL.
    from hub import config
    from hub import app as hub_app

    context = AsyncMock(return_value={"repo": str(tmp_path)})
    monkeypatch.setattr(hub_app.services, "project_git_context", context)
    monkeypatch.setattr(config, "REPO_NAME", "agentdrover/haiplane")
    exists = AsyncMock(return_value=False)
    fetch = AsyncMock(return_value=(False, "нет такого коммита нигде"))
    monkeypatch.setattr(plugins.git_ops, "commit_exists", exists, raising=False)
    monkeypatch.setattr(plugins.git_ops, "fetch_commit", fetch, raising=False)

    task_id = await _task_merged_at(client, db, "a" * 40)
    await repo.record_release(db, deployed_sha="b" * 40, ref="main", source="ci")

    first = await delivery_state(db, task_id)
    second = await delivery_state(db, task_id)
    assert first["state"] == UNKNOWN
    assert second["state"] == UNKNOWN
    assert fetch.await_count == 1, "повторный рендер не должен ходить в сеть"
    assert "промах" in second["reason"] or "отложена" in second["reason"]


async def test_local_commits_unchanged_semantics(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-3 (#937): для локально присутствующих коммитов семантика прежняя,
    # включая проект хаба с СОВПАДАЮЩИМ gh_repo (ранний выход не трогает своих).
    from hub import config
    from hub import app as hub_app

    real = GitOpsIntegration()
    context = AsyncMock(
        return_value={
            "repo": history["repo"],
            "base_branch": "main",
            "gh_repo": "agentdrover/haiplane",
        }
    )
    monkeypatch.setattr(hub_app.services, "project_git_context", context)
    monkeypatch.setattr(config, "REPO_NAME", "agentdrover/haiplane")
    monkeypatch.setattr(plugins.git_ops, "is_ancestor", real.is_ancestor, raising=False)
    monkeypatch.setattr(
        plugins.git_ops, "commit_exists", real.commit_exists, raising=False
    )

    task_id = await _task_merged_at(client, db, history["shipped"])
    await repo.record_release(
        db, deployed_sha=history["released"], ref="main", source="ci"
    )
    answer = await delivery_state(db, task_id)
    assert answer["state"] == IN_PROD


# ---- #946: a squash release keeps the content and drops the ancestry ----
#
# Observed on prod 24.08.2026, on the first release the policy made by itself
# (#927): the gate merged #910 into develop at f0d1e4e3, the hub opened release
# PR #12 and merged it into main SQUASH-ed at bddb322e, the deploy job shipped
# it — and the card then said "merged, waiting for a release" about code that
# was already running. git diff develop main was empty; only the ancestry was
# gone, because a squash writes a NEW commit instead of carrying the history.
#
# The rule these tests hold: what production runs is a question about content,
# not about the shape of the history that produced it.


async def test_squash_released_work_is_in_prod(
    client: AsyncClient, db: aiosqlite.Connection, squash_release, monkeypatch
):
    # AC-1 (#946): the merge is not an ancestor of the deployed commit — a
    # squash guarantees that — and the work is in production all the same.
    _use_real_git(monkeypatch, squash_release["repo"], "develop")
    task_id = await _task_merged_at(client, db, squash_release["task_merge"])
    await repo.record_release(
        db, deployed_sha=squash_release["released"], ref="main", source="ci"
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == IN_PROD, answer
    assert answer["deployed_sha"] == squash_release["released"]
    # The reason must say WHY, or the next reader re-derives the squash.
    assert "squash" in answer["reason"].lower()


async def test_work_merged_after_the_release_still_waits(
    client: AsyncClient, db: aiosqlite.Connection, squash_release, monkeypatch
):
    # AC-2 (#946): the fix must not turn "waiting for a release" into a
    # pretend deploy — that would trade one false answer for another.
    _use_real_git(monkeypatch, squash_release["repo"], "develop")
    task_id = await _task_merged_at(client, db, squash_release["after_release"])
    await repo.record_release(
        db, deployed_sha=squash_release["released"], ref="main", source="ci"
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == NOT_IN_PROD, answer
    assert "ждёт релиза" in answer["reason"]


async def test_unanswerable_squash_lookup_is_unknown_not_denial(
    client: AsyncClient, db: aiosqlite.Connection, squash_release, monkeypatch
):
    # AC-3 (#946): git that cannot answer stays "we do not know" (#725). The
    # released commit is present, the base branch is not — so the twin cannot
    # be looked for, and the card must not print that as "not deployed".
    _use_real_git(monkeypatch, squash_release["repo"], "no-such-branch")
    task_id = await _task_merged_at(client, db, squash_release["task_merge"])
    await repo.record_release(
        db, deployed_sha=squash_release["released"], ref="main", source="ci"
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == UNKNOWN, answer
    assert "не «не раскатано»" in answer["reason"]


# #950: ancestry is cut TWICE in the real flow — the release squashes, and the
# base branch is then recreated from the release branch. The twin (#946)
# survives the first cut but not the second: a merge left on the abandoned
# line belongs to no reachable history, and delivery_state answered "waiting
# for a release" about running code. That answer fed hub_record_live_check,
# which then refused the strongest kind of evidence the brief knows — a live
# observation of the fix behaving (task #949, refusal recorded in update
# #3496). The fact that survives both cuts is written by the release itself:
# which merges it carried, stamped at release-merge time.


from tests.conftest import _git_in  # noqa: E402 - harness of #497/#946


@pytest.fixture
def recreated_base(tmp_path: Path) -> dict[str, str]:
    """The 25.08 shape: task merged, squash-released, base recreated from main.

    The task's merge ends up on an ABANDONED line: not an ancestor of main
    (squash) and not an ancestor of the recreated develop either — the twin
    lookup of #946 finds a develop state matching the deploy, but the merge
    does not belong to its history.
    """
    root = tmp_path / "recreated"
    root.mkdir()
    _git_in(root, "init", "-b", "develop")
    (root / "a.py").write_text("a = 1\n")
    _git_in(root, "add", ".")
    _git_in(root, "commit", "-m", "seed")
    (root / "fix.py").write_text("fix = True\n")
    _git_in(root, "add", ".")
    _git_in(root, "commit", "-m", "feat(task): the fix (#949)")
    task_merge = _git_in(root, "rev-parse", "HEAD")
    # Release: squash develop into main — a NEW commit with the same tree.
    _git_in(root, "checkout", "-q", "--orphan", "main")
    _git_in(root, "add", ".")
    _git_in(root, "commit", "-m", "release: develop → main (#0)")
    released = _git_in(root, "rev-parse", "HEAD")
    # The base branch is recreated from the release branch: the old develop
    # line — including task_merge — is abandoned.
    _git_in(root, "branch", "-D", "develop")
    _git_in(root, "checkout", "-q", "-b", "develop")
    return {"repo": str(root), "task_merge": task_merge, "released": released}


async def _stamp(db, task_id: int, release_pr: int, release_sha: str) -> None:
    await db.execute(
        "UPDATE pipeline_merges SET released_pr = ?, released_sha = ? "
        "WHERE task_id = ?",
        (release_pr, release_sha, task_id),
    )
    await db.commit()


async def test_a_stamped_release_survives_a_recreated_base(
    client: AsyncClient, db: aiosqlite.Connection, recreated_base, monkeypatch
):
    # AC-1 (#950): ancestry is gone twice over, the stamp still answers.
    _use_real_git(monkeypatch, recreated_base["repo"], "develop")
    task_id = await _task_merged_at(client, db, recreated_base["task_merge"])
    await _stamp(db, task_id, 22, recreated_base["released"])
    await repo.record_release(
        db, deployed_sha=recreated_base["released"], ref="main", source="ci"
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == IN_PROD, answer
    assert "PR #22" in answer["reason"], (
        "ответ обязан назвать релиз, на записи которого он держится"
    )


async def test_an_unstamped_merge_on_an_abandoned_line_still_waits(
    client: AsyncClient, db: aiosqlite.Connection, recreated_base, monkeypatch
):
    # AC-2 (#950): no stamp — no claim. The old answer stands, and on the base
    # code THIS scenario is exactly what #949 hit: красная база для AC-1.
    _use_real_git(monkeypatch, recreated_base["repo"], "develop")
    task_id = await _task_merged_at(client, db, recreated_base["task_merge"])
    await repo.record_release(
        db, deployed_sha=recreated_base["released"], ref="main", source="ci"
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == NOT_IN_PROD, answer


async def test_a_stamped_but_undeployed_release_does_not_pretend(
    client: AsyncClient, db: aiosqlite.Connection, recreated_base, monkeypatch
):
    # The stamp must not turn "released but not yet deployed" into a deploy:
    # production still runs an OLDER release than the one that took the merge.
    _use_real_git(monkeypatch, recreated_base["repo"], "develop")
    task_id = await _task_merged_at(client, db, recreated_base["task_merge"])
    # The release that carried the merge is NOT what production runs — feed a
    # sha production has never seen (any commit not in released's history).
    await _stamp(db, task_id, 23, recreated_base["task_merge"])
    await repo.record_release(
        db, deployed_sha=recreated_base["released"], ref="main", source="ci"
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == NOT_IN_PROD, answer
    assert "ещё не раскатан" in answer["reason"]


async def test_the_release_flow_stamps_what_it_carried(db: aiosqlite.Connection):
    # The write side (#950): merging a release stamps every unreleased merge
    # of the project with the release PR and its commit.
    from unittest.mock import AsyncMock

    from hub.services.release import merge_ready_release
    from tests.test_release_policy import _git as _git_plugin
    from tests.test_release_policy import _release_project

    g = _git_plugin(existing_pr=777)
    g.ensure_remote_branch = AsyncMock(return_value=("present", "ok"))
    g.merge_commit_sha = AsyncMock(return_value="release0commit0sha")
    pid = await _release_project(db, "auto")
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
        "VALUES (?, ?, ?, ?)",
        (pid, 555, 42, "task0merge0sha"),
    )
    await db.commit()

    merged, _ = await merge_ready_release(db, await repo.get_project(db, pid))

    assert merged is True
    fact = await repo.release_fact_for_task(db, 42)
    assert fact is not None, "релиз обязан записать, какие мержи он увёз"
    assert fact["released_pr"] == 777
    assert fact["released_sha"] == "release0commit0sha"


async def test_the_stamp_never_rewrites_an_earlier_release(db: aiosqlite.Connection):
    # A merge already stamped by release N must not be re-stamped by N+1: the
    # first release that carried it is the historical fact.
    from unittest.mock import AsyncMock

    from hub.services.release import merge_ready_release
    from tests.test_release_policy import _git as _git_plugin
    from tests.test_release_policy import _release_project

    g = _git_plugin(existing_pr=888)
    g.ensure_remote_branch = AsyncMock(return_value=("present", "ok"))
    g.merge_commit_sha = AsyncMock(return_value="second0release0sha")
    pid = await _release_project(db, "auto")
    await db.execute(
        "INSERT INTO pipeline_merges "
        "(project_id, pr_number, task_id, merge_sha, released_pr, released_sha) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (pid, 556, 43, "old0merge0sha", 700, "first0release0sha"),
    )
    await db.commit()

    merged, _ = await merge_ready_release(db, await repo.get_project(db, pid))

    assert merged is True
    fact = await repo.release_fact_for_task(db, 43)
    assert fact["released_pr"] == 700, "первый увёзший релиз — исторический факт"
    assert fact["released_sha"] == "first0release0sha"


async def test_deployed_commit_is_the_merge_itself(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#953: identity needs no repository — the deploy IS this merge.

    Every other path here asks git about reachability, and a project without a
    working copy on this host therefore gets "could not check". When the
    deployed sha and the merge sha are the same string, there is nothing left
    to look up: a commit is part of its own history. Refusing to answer that
    left the demo stand — and any hub whose project is not cloned locally —
    with an empty delivery panel next to facts that fully answer the question.
    """
    from hub import app as hub_app

    sha = "c0ffee" + "0" * 34
    task_id = await _task_merged_at(client, db, sha)
    await repo.record_release(db, deployed_sha=sha, ref="main", source="ci")
    no_workspace = AsyncMock(return_value={"repo": "", "base_branch": "main"})
    monkeypatch.setattr(hub_app.services, "project_git_context", no_workspace)

    answer = await delivery_state(db, task_id)

    assert answer["state"] == IN_PROD, answer
    assert answer["merge_sha"] == sha
    assert answer["deployed_sha"] == sha
    assert answer["reason"]


# ---- #1214: молчание второго источника подавалось как отрицание ----
#
# ``merged_into_base`` спрашивает is_ancestor(submission_sha, origin/base) и по
# контракту различает три ответа, из которых третий — «посмотреть не смогли» —
# заведён ровно для случая, когда метод не работает. Метод не работал ВСЕГДА:
# конвейер мержит squash, и сдаточный коммит не остаётся предком базовой ветки
# никогда. False приходил на каждой доставке и читался как «посмотрели, кода в
# базовой ветке нет».
#
# ЗАМЕР 09.09.2026 на живом клоне, 4 из 4. Сдачи 48fad6129241 (#1186),
# bc3328def634 (#878), 3d2fd5814cf0 (#875), 3fedf1979908 (#909) — ни одна не
# предок origin/develop и origin/main, при том что AC-тест каждой лежит в
# origin/develop по имени. Цифра записана в ленте задачи; здесь она держится
# формой репозитория, а не переписанными хешами: тест воспроизводит НАСТОЯЩИЙ
# squash-мерж настоящим git и убеждается, что git отвечает «не предок» — то
# есть проверяет тот самый вход, на котором дефект и жил.
#
# Различение бесплатно: стратегию мержа задаёт сам гейт, она объявлена на
# форже (GitHub сливает `gh pr merge --squash`, GitVerse — локальным
# `merge --no-ff`), и спросить её можно ДО обращения к git.


def _hermetic_git(root: Path, *args: str) -> str:
    """git в ``root`` без пользовательской конфигурации, stdout строкой."""
    import subprocess

    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(root),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    ).stdout.strip()


@pytest.fixture
def squash_delivery(tmp_path: Path) -> dict[str, Any]:
    """Клон с настоящими origin/-ссылками: четыре доставки и одна нет.

    Собирается через bare-remote и clone, а не одним репозиторием с локальными
    ветками: вопрос задаётся про ``origin/<base>``, и подделать эту ссылку
    значило бы проверить не то, что работает в проде.

    Мержи делаются командой ``git merge --squash`` — тем же, что стоит за
    ``gh pr merge --squash``. Ни один хеш здесь не выдуман: «не предок» ниже
    утверждает git, а не фикстура.
    """
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _hermetic_git(remote, "init", "--bare", "-b", "develop")

    work = tmp_path / "clone"
    _hermetic_git(tmp_path, "clone", str(remote), str(work))

    (work / "base.py").write_text("base = 1\n")
    _hermetic_git(work, "add", ".")
    _hermetic_git(work, "commit", "-m", "base")
    _hermetic_git(work, "push", "origin", "develop")

    # Четыре доставки — по одной на каждую сдачу из замера.
    delivered: dict[int, str] = {}
    for task_id in (1186, 878, 875, 909):
        _hermetic_git(work, "checkout", "-q", "-b", f"task-{task_id}/w", "develop")
        (work / f"feature_{task_id}.py").write_text(f"answer = {task_id}\n")
        _hermetic_git(work, "add", ".")
        _hermetic_git(work, "commit", "-m", f"work for #{task_id}")
        delivered[task_id] = _hermetic_git(work, "rev-parse", "HEAD")
        _hermetic_git(work, "checkout", "-q", "develop")
        _hermetic_git(work, "merge", "--squash", f"task-{task_id}/w")
        _hermetic_git(work, "commit", "-m", f"feat(task): work (#{task_id})")

    # И одна работа, которая в базовую ветку не попадала вовсе.
    _hermetic_git(work, "checkout", "-q", "-b", "task-999/never", "develop")
    (work / "never.py").write_text("never = True\n")
    _hermetic_git(work, "add", ".")
    _hermetic_git(work, "commit", "-m", "work nobody merged")
    undelivered = _hermetic_git(work, "rev-parse", "HEAD")

    _hermetic_git(work, "checkout", "-q", "develop")
    _hermetic_git(work, "push", "origin", "develop")
    _hermetic_git(work, "fetch", "origin")

    return {
        "repo": str(work),
        "base": "develop",
        "delivered": delivered,
        "undelivered": undelivered,
    }


def _real_git_for(monkeypatch, squash_delivery: dict[str, Any], forge: str) -> None:
    """Настоящий git и настоящий адаптер форжа — обе половины вопроса.

    Подменяется весь ``plugins.git_ops`` целиком, а не отдельные методы:
    применимость метода и сам метод обязаны прийти из ОДНОГО объекта, иначе
    тест разрешит ровно то расхождение, которое чинит задача.
    """
    from hub import app as hub_app

    context = AsyncMock(
        return_value={
            "repo": squash_delivery["repo"],
            "base_branch": squash_delivery["base"],
            "gh_repo": "owner/repo",
            "forge": forge,
        }
    )
    monkeypatch.setattr(hub_app.services, "project_git_context", context)
    monkeypatch.setattr(
        "hub.services.orchestration.project_git_context", context, raising=False
    )
    monkeypatch.setattr(plugins, "git_ops", GitOpsIntegration(), raising=False)


async def _completed_blocker(
    client: AsyncClient, db: aiosqlite.Connection, sha: str, pr: int = 323
) -> dict[str, Any]:
    """Строка зависимости о завершённой задаче с закреплённой сдачей."""
    task_id = (await client.post("/api/tasks", json={"title": "upstream"})).json()["id"]
    await repo.update_task(
        db, task_id, status="completed", pr_number=pr, submission_sha=sha
    )
    await db.commit()
    return {
        "task_id": task_id,
        "title": "upstream",
        "status": "completed",
        "delivered": False,
        "reason": f"PR #{pr} не смержен гейтом",
    }


async def test_a_squash_merge_is_not_reported_as_missing_work(
    client: AsyncClient,
    db: aiosqlite.Connection,
    squash_delivery: dict[str, Any],
    monkeypatch,
):
    """AC-1: «этим способом нельзя» вместо «кода в базовой ветке нет».

    Фикстура намеренно начинается со squash-стратегии: на merge-commit зелена
    и доредакционная реализация, и такой тест не поймал бы ничего.
    """
    from hub.services.delivery_state import (
        BASE_UNANSWERABLE_NOTE,
        blocker_delivery,
        task_delivery,
        UNKNOWN,
    )

    sha = squash_delivery["delivered"][1186]

    # Вход теста — не предположение: спрашиваем настоящий git обеими половинами.
    real = GitOpsIntegration()
    assert (
        await real.is_ancestor(squash_delivery["repo"], sha, "origin/develop") is False
    ), "фикстура обязана воспроизводить именно тот вход, на котором жил дефект"
    assert "feature_1186.py" in _hermetic_git(
        Path(squash_delivery["repo"]), "ls-tree", "-r", "--name-only", "origin/develop"
    ), "а работа при этом доставлена — иначе проверялось бы не то"

    _real_git_for(monkeypatch, squash_delivery, "github")
    blocker = await _completed_blocker(client, db, sha)

    entry = await blocker_delivery(db, blocker)

    # Сила утверждения: не «не none», а именно unknown и именно этими словами.
    assert entry["delivery_path"] == "unknown", entry
    assert entry["delivered"] is False, "молчание — не подтверждение доставки"
    assert BASE_UNANSWERABLE_NOTE in entry["reason"], entry["reason"]
    assert "PR #323 не смержен гейтом" in entry["reason"], "исходная причина цела"

    # Второй потребитель — реестр расхождений — говорит ТЕМИ ЖЕ словами.
    row = await repo.get_task(db, blocker["task_id"])
    task = dict(row)
    task["pr_number"] = None  # у реестра без провайдера остаётся только база
    answer = await task_delivery(db, task)
    assert answer["state"] == UNKNOWN, answer

    monkeypatch.setattr(
        plugins.git_ops, "pr_state", AsyncMock(return_value=""), raising=False
    )
    task["pr_number"] = 323
    answer = await task_delivery(db, task)
    assert answer["state"] == UNKNOWN, answer
    assert BASE_UNANSWERABLE_NOTE in answer["reason"], answer["reason"]
    assert answer["delivery_path"] == "unknown", answer


async def test_the_measured_four_stop_reading_as_undelivered(
    client: AsyncClient,
    db: aiosqlite.Connection,
    squash_delivery: dict[str, Any],
    monkeypatch,
):
    """AC-2: 4 из 4 — цифра до правки; после неё не остаётся ни одной.

    Четыре сдачи замера воспроизведены по форме: каждая доставлена настоящим
    squash-мержем. Настоящие хеши (48fad6129241, bc3328def634, 3d2fd5814cf0,
    3fedf1979908) названы в ленте задачи — здесь важна не их запись, а то, что
    ни один из четырёх входов больше не читается как «не доставлено».
    """
    from hub.services.delivery_state import blocker_delivery

    _real_git_for(monkeypatch, squash_delivery, "github")
    real = GitOpsIntegration()

    said_undelivered: list[int] = []
    for task_id, sha in squash_delivery["delivered"].items():
        assert (
            await real.is_ancestor(squash_delivery["repo"], sha, "origin/develop")
            is False
        ), f"#{task_id}: замер держится на том, что git отвечает «не предок»"
        entry = await blocker_delivery(db, await _completed_blocker(client, db, sha))
        if entry["delivery_path"] == "none":
            said_undelivered.append(task_id)

    assert said_undelivered == [], (
        f"до правки этот список был всеми четырьмя: осталось {said_undelivered}"
    )
    assert len(squash_delivery["delivered"]) == 4, "замер был на четырёх, не меньше"


async def test_a_truly_undelivered_blocker_still_says_so(
    client: AsyncClient,
    db: aiosqlite.Connection,
    squash_delivery: dict[str, Any],
    monkeypatch,
):
    """AC-3: там, где родословная судить может, «не доставлено» осталось.

    Тот же файл и та же фикстура, что у AC-1 — иначе починка ложного
    срабатывания могла бы погасить настоящее, и никто бы не заметил. Форж
    здесь мержит локальным ``merge --no-ff`` (GitVerse), то есть сдаточный
    коммит остаётся предком базы, и «не предок» — настоящее отрицание.
    """
    from hub.services.delivery_state import (
        BASE_UNANSWERABLE_NOTE,
        BASE_UNCHECKED_NOTE,
        blocker_delivery,
    )

    _real_git_for(monkeypatch, squash_delivery, "gitverse")
    blocker = await _completed_blocker(client, db, squash_delivery["undelivered"])

    entry = await blocker_delivery(db, blocker)

    assert entry["delivery_path"] == "none", entry
    assert entry["delivered"] is False, entry
    assert BASE_UNANSWERABLE_NOTE not in entry["reason"], (
        "работа действительно не в базовой ветке — звать это молчанием нельзя"
    )
    assert BASE_UNCHECKED_NOTE not in entry["reason"], entry["reason"]

    # И тот же форж подтверждает доставку, когда она есть, — то есть источник
    # остался работающим, а не замолчал в обе стороны.
    _hermetic_git(Path(squash_delivery["repo"]), "checkout", "-q", "develop")
    tip = _hermetic_git(Path(squash_delivery["repo"]), "rev-parse", "origin/develop")
    delivered_entry = await blocker_delivery(
        db, await _completed_blocker(client, db, tip)
    )
    assert delivered_entry["delivered"] is True, delivered_entry
    assert delivered_entry["delivery_path"] == "outside_gate", delivered_entry
