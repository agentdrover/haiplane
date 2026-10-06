"""What is running in production, asked of the whole board at once (#499).

The facts existed before this: the deploy CI reported (#839, #496), the merges
the hub performed (#534) and the comparison between them (#497). A card could
answer for one task; "what has not reached production" meant opening cards one
by one.

Two of these tests are about restraint rather than output: unknown must stay
its own list, and the window the snapshot covers must be stated. A bounded list
presented as the whole board is the failure #824 refused to ship.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.registry import plugins
from hub.services.prod_state import format_prod_state, prod_state
from tests.test_delivery_state import _task_merged_at, _use_real_git


async def _completed(client: AsyncClient, db, merge_sha: str) -> int:
    task_id = await _task_merged_at(client, db, merge_sha)
    await repo.update_task(db, task_id, status="completed")
    await db.commit()
    return task_id


async def test_snapshot_splits_delivered_from_waiting(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-1 (#499): one call answers for the whole board — what shipped and
    # what is merged and still waiting.
    _use_real_git(monkeypatch, history["repo"])
    monkeypatch.setattr(
        plugins.git_ops,
        "commit_exists",
        GitOpsIntegration().commit_exists,
        raising=False,
    )
    shipped = await _completed(client, db, history["shipped"])
    waiting = await _completed(client, db, history["pending"])
    await repo.record_release(
        db, deployed_sha=history["released"], ref="main", source="ci"
    )

    snapshot = await prod_state(db)

    assert snapshot["deployed"]["sha"] == history["released"]
    assert shipped in [e["task_id"] for e in snapshot["in_prod"]]
    assert waiting in [e["task_id"] for e in snapshot["not_in_prod"]]


async def test_unknown_is_its_own_list(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-2 (#499): a task the hub never merged cannot be compared with
    # anything. Putting it among not_in_prod would say it failed to ship.
    _use_real_git(monkeypatch, history["repo"])
    task_id = (await client.post("/api/tasks", json={"title": "Never merged"})).json()[
        "id"
    ]
    await repo.update_task(db, task_id, status="completed")
    await repo.record_release(
        db, deployed_sha=history["released"], ref="main", source="ci"
    )
    await db.commit()

    snapshot = await prod_state(db)

    assert task_id in [e["task_id"] for e in snapshot["unknown"]]
    assert task_id not in [e["task_id"] for e in snapshot["not_in_prod"]]
    entry = next(e for e in snapshot["unknown"] if e["task_id"] == task_id)
    assert entry["reason"], "an unknown without a cause is just a blank"


async def test_no_releases_explains_itself(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-3 (#499): an installation with no delivery facts knows nothing about
    # production. The snapshot must say that instead of showing an empty
    # in_prod list, which reads as "nothing ever shipped".
    _use_real_git(monkeypatch, history["repo"])
    await _completed(client, db, history["shipped"])

    snapshot = await prod_state(db)

    assert snapshot["deployed"]["sha"] == ""
    assert "не знает" in snapshot["note"]
    assert "неизвестно" in format_prod_state(snapshot)


async def test_interfaces_share_one_builder(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-4 (#499): REST and the CLI/MCP rendering come from the same snapshot.
    # Two renderings of the same facts drift, and then two readers disagree
    # about production — the reason #808 and #823 unified their builders.
    _use_real_git(monkeypatch, history["repo"])
    monkeypatch.setattr(
        plugins.git_ops,
        "commit_exists",
        GitOpsIntegration().commit_exists,
        raising=False,
    )
    await _completed(client, db, history["shipped"])
    await repo.record_release(
        db, deployed_sha=history["released"], ref="main", source="ci"
    )

    over_rest = (await client.get("/api/prod-state")).json()
    direct = await prod_state(db)

    assert over_rest["deployed"] == direct["deployed"]
    assert [e["task_id"] for e in over_rest["in_prod"]] == [
        e["task_id"] for e in direct["in_prod"]
    ]
    assert over_rest["note"] == direct["note"], "the stated window must match too"
    assert history["released"][:12] in format_prod_state(over_rest)


async def test_window_is_stated_not_implied(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # #824's lesson, applied here: the snapshot is bounded, and a bound nobody
    # is told about reads as the whole board.
    _use_real_git(monkeypatch, history["repo"])
    # Three merges, three commits: the registry is keyed by the merge (#1343),
    # so one commit cannot stand for three deliveries.
    for sha_key in ("shipped", "released", "pending"):
        await _completed(client, db, history[sha_key])

    snapshot = await prod_state(db, limit=2)

    assert snapshot["window"] == 2
    assert snapshot["examined"] == 2
    assert "окно 2" in snapshot["note"]
    assert "старше окна" in snapshot["note"]


# ---- #1603: снимок укладывается в срок клиента -------------------------------


class _FakeGit:
    """Подставной git_ops: задержка, счётчик одновременных вызовов, счёт вызовов."""

    def __init__(self, delay: float = 0.0, ancestor: bool = True):
        self.delay = delay
        self.ancestor = ancestor
        self.active = 0
        self.peak = 0
        self.calls = 0

    async def _wait(self) -> None:
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.active -= 1

    async def commit_exists(self, repo, sha):
        return True

    async def is_ancestor(self, repo, ancestor, descendant):
        await self._wait()
        return self.ancestor

    async def commit_with_same_tree(self, repo, sha, branch):
        return ""


def _wire_fake_git(monkeypatch, fake: _FakeGit, ctx: dict | None = None) -> dict:
    from hub import app as hub_app

    context = dict(
        ctx if ctx is not None else {"repo": "/ws/one", "base_branch": "main"}
    )
    monkeypatch.setattr(
        hub_app.services,
        "project_git_context",
        AsyncMock(side_effect=lambda *_: dict(context)),
    )
    for name in ("commit_exists", "is_ancestor", "commit_with_same_tree"):
        monkeypatch.setattr(plugins.git_ops, name, getattr(fake, name), raising=False)
    return context


async def _board(client, db, count: int) -> list[int]:
    ids = []
    for i in range(count):
        ids.append(await _completed(client, db, f"{i + 1:040x}"))
    await repo.record_release(db, deployed_sha="f" * 40, ref="main", source="ci")
    return ids


async def test_prod_state_checks_tasks_with_bounded_parallelism(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#1603): git-проверки идут ограниченно-параллельно, релиз читается
    # один раз, порядок бакетов — порядок списка завершённых задач.
    from hub.services import prod_state as ps

    fake = _FakeGit(delay=0.02)
    _wire_fake_git(monkeypatch, fake)
    ids = await _board(client, db, 50)
    reads = 0
    real_latest = repo.latest_successful_release

    async def counting(*args, **kwargs):
        nonlocal reads
        reads += 1
        return await real_latest(*args, **kwargs)

    monkeypatch.setattr(repo, "latest_successful_release", counting)

    snapshot = await prod_state(db, limit=50)

    ceiling = getattr(ps, "MAX_CONCURRENCY", 8)
    assert fake.peak > 1, "checks ran one by one"
    assert fake.peak <= ceiling
    listed = [
        int(r["id"]) for r in await repo.list_tasks_by_status(db, "completed", limit=50)
    ]
    assert [e["task_id"] for e in snapshot["in_prod"]] == listed
    assert sorted(listed) == sorted(ids)
    assert reads == 1, f"latest_successful_release read {reads} times"


async def test_in_prod_answer_is_cached_per_full_key_only(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#1603): IN_PROD кэшируется по (workspace, base_branch, merge_sha,
    # deployed_sha); NOT_IN_PROD и UNKNOWN каждый раз проверяются заново.
    fake = _FakeGit()
    ctx = _wire_fake_git(monkeypatch, fake)
    await _board(client, db, 1)

    await prod_state(db)
    first = fake.calls
    assert first > 0
    await prod_state(db)
    assert fake.calls == first, "a definite IN_PROD answer was asked of git again"

    # другой base_branch — другой ключ
    ctx["base_branch"] = "develop"
    await prod_state(db)
    assert fake.calls > first
    after_base = fake.calls

    # новый deployed_sha — другой ключ
    await repo.record_release(db, deployed_sha="e" * 40, ref="main", source="ci")
    await prod_state(db)
    assert fake.calls > after_base
    after_release = fake.calls

    # чужой gh_repo — применимость проверена заново, кэш не отвечает
    from hub import config

    monkeypatch.setattr(config, "REPO_NAME", "agentdrover/haiplane")
    ctx["gh_repo"] = "agentdrover/other"
    snapshot = await prod_state(db)
    assert fake.calls == after_release
    assert snapshot["in_prod"] == [] and len(snapshot["unknown"]) == 1

    # NOT_IN_PROD не кэшируется
    del ctx["gh_repo"]
    fake.ancestor = False
    ctx["base_branch"] = "other-base"
    await prod_state(db)
    not_in = fake.calls
    snapshot = await prod_state(db)
    assert len(snapshot["not_in_prod"]) == 1
    assert fake.calls > not_in, "NOT_IN_PROD was served from the cache"


async def test_prod_state_budget_moves_unchecked_tasks_to_unknown(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#1603): бюджет исчерпан — непроверенное в unknown, не в not_in_prod.
    import time
    from hub.services import prod_state as ps

    budget = 0.4
    monkeypatch.setattr(ps, "BUILD_BUDGET_SECONDS", budget, raising=False)
    fake = _FakeGit(delay=0.3)
    _wire_fake_git(monkeypatch, fake)
    await _board(client, db, 12)

    started = time.monotonic()
    snapshot = await prod_state(db, limit=50)
    elapsed = time.monotonic() - started

    assert elapsed < budget + 1.0, f"snapshot took {elapsed:.1f}s"
    unchecked = [
        e for e in snapshot["unknown"] if "срок сборки снимка исчерпан" in e["reason"]
    ]
    assert unchecked, "nothing was left unchecked"
    assert snapshot["not_in_prod"] == []
    assert snapshot["examined"] == 12 - len(unchecked)
    assert str(len(unchecked)) in snapshot["note"]
    assert len(snapshot["in_prod"]) == snapshot["examined"]


def test_prod_state_budget_is_below_client_timeout():
    # AC-5 (#1603): сервер отвечает раньше, чем клиент оборвёт ожидание.
    from hub import mcp_server
    from hub.services import prod_state as ps

    budget = getattr(ps, "BUILD_BUDGET_SECONDS", None)
    assert budget is not None, "the snapshot has no build budget"
    assert budget < mcp_server._TIMEOUT_DEFAULT


async def test_budget_covers_the_sql_phase_too(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # #1603 (Codex P2): медленная подготовка тратит тот же бюджет, что и git.
    import time
    from hub.services import prod_state as ps

    budget = 0.05
    monkeypatch.setattr(ps, "BUILD_BUDGET_SECONDS", budget, raising=False)
    _wire_fake_git(monkeypatch, _FakeGit())
    await _board(client, db, 4)
    real_prepare = ps.prepare_delivery

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.35)
        return await real_prepare(*args, **kwargs)

    monkeypatch.setattr(ps, "prepare_delivery", slow)

    started = time.monotonic()
    snapshot = await prod_state(db, limit=50)
    elapsed = time.monotonic() - started

    assert elapsed < budget + 0.25, f"took {elapsed:.2f}s"
    assert snapshot["examined"] == 0
    assert len(snapshot["unknown"]) == 4
    assert all(
        "срок сборки снимка исчерпан" in e["reason"] for e in snapshot["unknown"]
    )
    assert snapshot["not_in_prod"] == [] and snapshot["in_prod"] == []
    assert "Не проверено 4 из 4" in snapshot["note"]


async def test_external_cancel_waits_for_child_cleanup(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # #1603 (Codex P2): к моменту CancelledError у родителя очистка всех
    # дочерних проверок (в проде — kill и wait процесса в proc.run) завершена.
    started = 0
    cleaned = 0

    class Slow(_FakeGit):
        async def is_ancestor(self, repo_, ancestor, descendant):
            nonlocal started, cleaned
            started += 1
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.sleep(0.2)  # kill + wait the child
                cleaned += 1
                raise
            return True

    _wire_fake_git(monkeypatch, Slow())
    await _board(client, db, 5)

    task = asyncio.ensure_future(prod_state(db, limit=50))
    for _ in range(100):
        if started >= 5:
            break
        await asyncio.sleep(0.02)
    assert started >= 5
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert cleaned == started, f"{started - cleaned} checks still cleaning up"


async def test_unknown_is_not_cached(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # #1603: UNKNOWN при повторе спрашивает git заново.
    class Unsure(_FakeGit):
        async def is_ancestor(self, repo_, ancestor, descendant):
            await self._wait()
            return None

    fake = Unsure()
    _wire_fake_git(monkeypatch, fake)
    await _board(client, db, 1)

    first = await prod_state(db)
    calls = fake.calls
    second = await prod_state(db)

    assert len(first["unknown"]) == len(second["unknown"]) == 1
    assert fake.calls > calls, "UNKNOWN was served from the cache"


async def test_in_prod_cache_is_capped_and_resets(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # #1603: кэш ограничен и сбрасывается целиком при переполнении.
    from hub.services import delivery_state as ds

    monkeypatch.setattr(ds, "_IN_PROD_CAP", 3)
    _wire_fake_git(monkeypatch, _FakeGit())
    await _board(client, db, 5)

    snapshot = await prod_state(db)

    assert len(snapshot["in_prod"]) == 5
    assert 1 <= len(ds._in_prod_cache) <= 3


async def test_cached_answer_carries_the_current_deploy_date(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # #1603 (Codex P2): тот же sha выкатили снова — дата в ответе новая.
    from hub.services.delivery_state import delivery_state

    _wire_fake_git(monkeypatch, _FakeGit())
    ids = await _board(client, db, 1)
    await db.execute(
        "UPDATE releases SET deployed_at = ?", ("2026-01-01T00:00:00+00:00",)
    )
    await db.commit()
    first = await delivery_state(db, ids[0])
    assert "2026-01-01" in first["reason"]

    await db.execute(
        "UPDATE releases SET deployed_at = ?", ("2026-02-02T00:00:00+00:00",)
    )
    await db.commit()
    second = await delivery_state(db, ids[0])

    assert second["deployed_at"].startswith("2026-02-02")
    assert "2026-02-02" in second["reason"] and "2026-01-01" not in second["reason"]
