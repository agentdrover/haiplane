"""Merged is not running (#497).

On 21.08.2026 task #823 sat ``completed`` with its PR merged into develop while
the deploy job was skipped — deployment runs from main. Nothing in the hub could
tell the two apart; it took reading GitHub's logs. These tests hold the
comparison that closes that gap, and above all the third answer: "we could not
check" must never print as "not deployed".
"""

from __future__ import annotations

from pathlib import Path

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
