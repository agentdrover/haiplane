"""What is actually running in production, as a stored fact (#839).

The hub already records its own merges (#534) and read them as delivery. On
21.08.2026 that reading was wrong in a way nobody could see from the hub: task
#823 sat ``completed`` with its PR merged into develop, while the deploy job
was skipped — deployment runs from main. "Merged" and "running" were the same
fact, and one of them was false.

These tests hold the two properties that keep the new fact honest: a failed
deploy never becomes the state of production, and NO RECORD reads as unknown
rather than as "nothing is deployed".
"""

from __future__ import annotations

import aiosqlite

from hub import repository as repo
from hub.db import _migrate


async def test_migration_creates_releases_and_is_idempotent(db: aiosqlite.Connection):
    # AC-1 (#839): the table exists after migration, and running migrations a
    # second time — every hub start does — must not fail. A migration that
    # only works once is a boot failure waiting for a restart.
    rows = list(
        await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='releases'"
        )
    )
    assert rows, "releases table must exist after migrations"

    await _migrate(db)

    rows = list(
        await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='releases'"
        )
    )
    assert rows, "a repeated migration run must leave the table in place"


async def test_latest_release_returns_what_was_recorded(db: aiosqlite.Connection):
    # AC-2 (#839): the stored deploy comes back as itself — sha and time.
    await repo.record_release(
        db,
        deployed_sha="deadbee" + "f" * 33,
        project_id=1,
        ref="main",
        source="ci",
    )

    latest = await repo.latest_successful_release(db, 1)

    assert latest is not None
    assert latest["deployed_sha"].startswith("deadbee")
    assert latest["ref"] == "main"
    assert latest["deployed_at"], "a deploy without a time cannot be reasoned about"


async def test_failed_deploy_does_not_become_prod_state(db: aiosqlite.Connection):
    # AC-3 (#839): when a deploy falls over, production keeps running the code
    # from the last one that worked. Reading "the newest row" instead would
    # name a commit that never started.
    await repo.record_release(
        db, deployed_sha="a" * 40, project_id=1, ref="main", source="ci"
    )
    await repo.record_release(
        db,
        deployed_sha="b" * 40,
        project_id=1,
        ref="main",
        status=repo.RELEASE_FAILED,
        source="ci",
    )

    latest = await repo.latest_successful_release(db, 1)

    assert latest is not None
    assert latest["deployed_sha"] == "a" * 40, (
        "a failed deploy must not be reported as what is running"
    )
    failed = list(
        await db.execute_fetchall("SELECT * FROM releases WHERE status='failed'")
    )
    assert failed, "the failure is still recorded — it is evidence about the pipeline"


async def test_no_records_reads_as_unknown(db: aiosqlite.Connection):
    # AC-4 (#839): an installation that predates this table, or one whose CI
    # does not report yet, knows nothing about production. It must not answer
    # "nothing is deployed" — that is silence turned into a denial, the exact
    # failure this epic removes.
    assert await repo.latest_successful_release(db, 1) is None
    assert await repo.latest_successful_release(db) is None


# ---- #495: the deploy callback, the only writer of the facts above ----


def _deploy_tokens(monkeypatch) -> dict[str, dict[str, str]]:
    """Identities for the callback: one that may record, two that may not.

    Production grants ``deploys.record`` through the DB-backed ``ci_runner``
    role; from env tokens only ``admin`` carries every permission, so that
    stands in for CI here — the same substitution the ci-run-report tests make.
    """
    from hub import config

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        config.parse_tokens("denis:human-token:human,ci:ci-token:admin"),
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return {
        "ci": {"Authorization": "Bearer ci-token"},
        "human": {"Authorization": "Bearer human-token"},
        "none": {},
    }


async def _release_rows(db: aiosqlite.Connection) -> list[dict]:
    return [dict(r) for r in await db.execute_fetchall("SELECT * FROM releases")]


async def test_deploy_callback_requires_auth(client, db, monkeypatch):
    # AC-1 (#495): checked against the TABLE, not the status code. A 401 that
    # still wrote a row would be the worst of both — the caller told "no" while
    # the hub believes something shipped.
    auth = _deploy_tokens(monkeypatch)
    body = {"sha": "shipped-one", "ref": "main", "status": "success"}

    anonymous = await client.post("/api/deploys", json=body, headers=auth["none"])
    as_human = await client.post("/api/deploys", json=body, headers=auth["human"])

    assert anonymous.status_code in (401, 403), anonymous.text
    assert as_human.status_code == 403, "a human token does not carry deploys.record"
    assert await _release_rows(db) == [], "a refused call must write nothing"


async def test_deploy_callback_records_release(client, db, monkeypatch):
    # AC-2 (#495): the fact enters the hub and reads back as production state.
    auth = _deploy_tokens(monkeypatch)

    resp = await client.post(
        "/api/deploys",
        json={"sha": "shipped-two", "ref": "main", "status": "success"},
        headers=auth["ci"],
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["deployed_sha"] == "shipped-two"
    latest = await repo.latest_successful_release(db)
    assert latest is not None and latest["deployed_sha"] == "shipped-two"
    assert latest["source"], "who reported it is part of the fact"


async def test_deploy_callback_is_idempotent_by_sha(client, db, monkeypatch):
    # AC-3 (#495): CI runs get re-run, and a re-run redelivers the callback.
    # Two rows would claim the commit was deployed twice.
    auth = _deploy_tokens(monkeypatch)
    body = {"sha": "shipped-three", "ref": "main", "status": "success"}

    first = await client.post("/api/deploys", json=body, headers=auth["ci"])
    second = await client.post("/api/deploys", json=body, headers=auth["ci"])

    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert len(await _release_rows(db)) == 1


async def test_failed_callback_is_stored_but_not_prod_state(client, db, monkeypatch):
    # AC-4 (#495): a failed rollout is evidence about the pipeline and is kept,
    # but production still runs the last release that worked.
    auth = _deploy_tokens(monkeypatch)
    await client.post(
        "/api/deploys",
        json={"sha": "shipped-four", "ref": "main", "status": "success"},
        headers=auth["ci"],
    )
    await client.post(
        "/api/deploys",
        json={"sha": "fell-over", "ref": "main", "status": "failed"},
        headers=auth["ci"],
    )

    rows = await _release_rows(db)
    latest = await repo.latest_successful_release(db)

    assert len(rows) == 2, "the failure is recorded, not dropped"
    assert latest is not None and latest["deployed_sha"] == "shipped-four"


# ---- #968: a squash release leaves commits behind, and they are not work ----
#
# Observed on prod 26.08.2026, minutes after #931 shipped. The poller opened and
# merged twenty release PRs in ninety minutes; one carried work, nineteen were
# empty, and each one redeployed production. A squash release writes a NEW
# commit on the release branch instead of carrying the originals, so the range
# base..head never empties — and #931, which taught the poller to open a release
# on a non-empty range, turned that leftover into a self-renewing reason.
#
# The rule these tests hold: "is there anything to release" is a question about
# CONTENT, not about how many commits happen to sit in the range.

from unittest.mock import AsyncMock  # noqa: E402

from hub.integrations.git_ops import GitOpsIntegration  # noqa: E402
from hub.integrations.registry import plugins  # noqa: E402
from hub.services.release import open_release_for_range  # noqa: E402


def _release_ctx(workspace: str) -> dict[str, str]:
    return {"repo": workspace, "gh_repo": "agentdrover/haiplane"}


async def test_squash_leftover_range_is_not_a_release(squash_release, monkeypatch):
    # AC-1 (#968): commits in the range, no difference in content — silence.
    # Not a reason, not a PR: the poller walks this every cycle, and a line per
    # cycle is how a real signal gets muted (#534).
    #
    # The state right after a squash release: the release branch holds exactly
    # what the base branch held, and the range still lists every commit that
    # went into it. The fixture moves on past that point, so the moment is
    # named here as its own ref rather than by rewinding a shared fixture.
    from tests.conftest import _git_in

    _git_in(
        squash_release["repo"],
        "branch",
        "released-state",
        squash_release["develop_tip"],
    )
    real = GitOpsIntegration()
    monkeypatch.setattr(
        plugins.git_ops, "content_differs", real.content_differs, raising=False
    )
    opened = AsyncMock(return_value=999)
    monkeypatch.setattr(plugins.git_ops, "open_release_pr", opened, raising=False)
    monkeypatch.setattr(
        plugins.git_ops,
        "release_range",
        AsyncMock(return_value=["feat(task): whatever (#1)"]),
        raising=False,
    )

    pr, subjects, task_ids, reason = await open_release_for_range(
        _release_ctx(squash_release["repo"]), "main", "released-state"
    )

    assert pr is None, "an empty-by-content range must not open a release"
    assert reason == "", f"nothing to release is silence, not a reason: {reason!r}"
    assert not opened.called, "GitHub must not be asked to open an empty release"


async def test_real_work_still_opens_a_release_without_new_delivery(
    squash_release, monkeypatch
):
    # AC-2 (#968): the fix must not undo #931. Work that differs in content
    # opens a release even though no task was delivered just now.
    from tests.conftest import _git_in

    root = squash_release["repo"]
    _git_in(root, "checkout", "-q", "develop")
    (__import__("pathlib").Path(root) / "e.py").write_text("e = 5\n")
    _git_in(root, "add", ".")
    _git_in(root, "commit", "-m", "feat(task): real work (#777)")

    real = GitOpsIntegration()
    monkeypatch.setattr(
        plugins.git_ops, "content_differs", real.content_differs, raising=False
    )
    monkeypatch.setattr(
        plugins.git_ops,
        "release_range",
        AsyncMock(return_value=["feat(task): real work (#777)"]),
        raising=False,
    )
    monkeypatch.setattr(
        plugins.git_ops, "open_release_pr", AsyncMock(return_value=321), raising=False
    )

    pr, subjects, task_ids, reason = await open_release_for_range(
        _release_ctx(root), "main", "develop"
    )

    assert pr == 321, f"real work must still open a release (#931), got {reason!r}"
    assert 777 in task_ids


async def test_unreadable_diff_is_unknown_not_nothing_to_release(
    squash_release, monkeypatch
):
    # AC-3 (#968): git that cannot answer is not "everything is delivered".
    # The release is not opened, and the cause is named — the same line #725
    # draws between silence and denial.
    monkeypatch.setattr(
        plugins.git_ops,
        "content_differs",
        AsyncMock(return_value=None),
        raising=False,
    )
    opened = AsyncMock(return_value=999)
    monkeypatch.setattr(plugins.git_ops, "open_release_pr", opened, raising=False)

    pr, subjects, task_ids, reason = await open_release_for_range(
        _release_ctx(squash_release["repo"]), "main", "develop"
    )

    assert pr is None
    assert reason, "an unanswerable question must be named, not swallowed"
    assert not opened.called


# ---- #972: composition of the release PR is the leftover of #968 AC-4 ----
#
# Detection now asks about content. The title, body and feed note still count
# the squash leftover: every feat(task) subject that ever sat on develop.
# These tests hold the second half of the same rule — name what this release
# actually carries.


def _inflated_leftover_range() -> list[str]:
    return [
        "feat(task): leftover (#880)",
        "feat(task): leftover again (#880)",
        "feat(task): old (#927)",
        "feat(task): old (#931)",
        "feat(task): old (#46)",
        "feat(task): old (#962)",
        "feat(task): only this one (#728)",
    ]


def _numbered_squash_trail(tmp_path) -> str:
    """Squash leftover carries numbered tasks; one new task lands after it.

    The leftover subjects are what a broken cut (no tree-match stop) would
    still list. AC-1/AC-2 must fail if those numbers leak into the PR.
    """
    from pathlib import Path

    from tests.conftest import _git_in

    root = Path(tmp_path) / "numbered-trail"
    root.mkdir()
    _git_in(root, "init", "-b", "main")
    (root / "a.py").write_text("a = 1\n")
    _git_in(root, "add", ".")
    _git_in(root, "commit", "-m", "base")
    _git_in(root, "checkout", "-q", "-b", "develop")
    (root / "b.py").write_text("b = 2\n")
    _git_in(root, "add", ".")
    _git_in(root, "commit", "-m", "feat(task): leftover (#880)")
    (root / "c.py").write_text("c = 3\n")
    _git_in(root, "add", ".")
    _git_in(root, "commit", "-m", "feat(task): leftover again (#880)")
    (root / "d.py").write_text("d = 4\n")
    _git_in(root, "add", ".")
    _git_in(root, "commit", "-m", "feat(task): old (#927)")
    _git_in(root, "checkout", "-q", "main")
    _git_in(root, "merge", "--squash", "develop")
    _git_in(root, "commit", "-m", "release: develop -> main")
    _git_in(root, "checkout", "-q", "develop")
    (root / "e.py").write_text("e = 5\n")
    _git_in(root, "add", ".")
    _git_in(root, "commit", "-m", "feat(task): only this one (#728)")
    return str(root)


def _wire_real_cut(monkeypatch, *, pr: int):
    real = GitOpsIntegration()
    monkeypatch.setattr(
        plugins.git_ops, "content_differs", real.content_differs, raising=False
    )
    monkeypatch.setattr(
        plugins.git_ops,
        "undelivered_release_range",
        real.undelivered_release_range,
        raising=False,
    )
    monkeypatch.setattr(
        plugins.git_ops,
        "release_range",
        AsyncMock(return_value=_inflated_leftover_range()),
        raising=False,
    )
    opened = AsyncMock(return_value=pr)
    monkeypatch.setattr(plugins.git_ops, "open_release_pr", opened, raising=False)
    return opened


async def test_undelivered_release_range_stops_at_matching_tree(squash_release):
    # The cut itself: leftover whose tree equals main is not "new work";
    # the one commit after the squash is. Without the tree-match break this
    # returns the whole main..develop subject list.
    ops = GitOpsIntegration()
    root = squash_release["repo"]

    leftover_only = await ops.undelivered_release_range(
        "main", squash_release["develop_tip"], repo=root
    )
    assert leftover_only == [], leftover_only

    after = await ops.undelivered_release_range("main", "develop", repo=root)
    assert after == ["merged after the release"], after


async def test_release_title_counts_only_undelivered_work(tmp_path, monkeypatch):
    # AC-1: after several squash releases the range still lists the trail,
    # and one real task is new. The title names 1, not the trail length.
    root = _numbered_squash_trail(tmp_path)
    opened = _wire_real_cut(monkeypatch, pr=95)

    pr, subjects, task_ids, reason = await open_release_for_range(
        _release_ctx(root), "main", "develop"
    )

    assert pr == 95, f"real work must open a release, got {reason!r}"
    title = opened.await_args.args[2]
    assert "1 задач(и)" in title, title
    assert "880" not in title, title
    assert task_ids == [728]
    assert 880 not in task_ids
    assert 927 not in task_ids


async def test_release_body_lists_each_task_once(tmp_path, monkeypatch):
    # AC-2: the body lists only the work this release carries, each number once.
    root = _numbered_squash_trail(tmp_path)
    opened = _wire_real_cut(monkeypatch, pr=95)

    pr, subjects, task_ids, reason = await open_release_for_range(
        _release_ctx(root), "main", "develop"
    )

    assert pr == 95, reason
    body = opened.await_args.args[3]
    assert "#728" in body
    assert "#880" not in body, body
    assert "#927" not in body, body
    assert body.count("#728") == 1, body
    assert task_ids == [728]


async def test_narrowing_to_empty_never_silently_drops_a_release(
    squash_release, monkeypatch
):
    # AC-4: content differs, but the cut came back empty. Opening by the full
    # range (or naming why) — never the silent None that stalls the conveyor.
    monkeypatch.setattr(
        plugins.git_ops,
        "content_differs",
        AsyncMock(return_value=True),
        raising=False,
    )
    monkeypatch.setattr(
        plugins.git_ops,
        "release_range",
        AsyncMock(return_value=_inflated_leftover_range()),
        raising=False,
    )
    monkeypatch.setattr(
        plugins.git_ops,
        "undelivered_release_range",
        AsyncMock(return_value=[]),
        raising=False,
    )
    opened = AsyncMock(return_value=99)
    monkeypatch.setattr(plugins.git_ops, "open_release_pr", opened, raising=False)

    pr, subjects, task_ids, reason = await open_release_for_range(
        _release_ctx(squash_release["repo"]), "main", "develop"
    )

    assert pr == 99, f"fallback must still open, got {reason!r}"
    assert opened.called
    title = opened.await_args.args[2]
    assert "6 задач(и)" in title, title
    assert 880 in task_ids and 728 in task_ids
