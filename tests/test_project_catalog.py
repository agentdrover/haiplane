import asyncio
import json
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from threading import Event
from unittest.mock import Mock

import pytest

from hub import cli, config, git_policy, mcp_server, repository
from hub.app import app
from hub.config import TokenIdentity
from hub.services import project_catalog, project_policy

_CACHED = "/api/projects?branch_check=cached"


@pytest.fixture
async def catalog(monkeypatch):
    cache = project_catalog.ProjectCatalog()
    monkeypatch.setattr(app.state, "project_catalog", cache, raising=False)
    try:
        yield cache
    finally:
        await cache.stop()


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(project_catalog, "monotonic", lambda: now[0])
    monkeypatch.setattr(git_policy, "monotonic", lambda: now[0])
    return now


@pytest.fixture
def mcp_rest(client, monkeypatch):
    @asynccontextmanager
    async def rest_client(_timeout):
        yield client

    monkeypatch.setattr(mcp_server, "_rest_client", rest_client)
    monkeypatch.setattr(mcp_server, "_hub_url", lambda: str(client.base_url))


async def _projects(client, path=_CACHED, **kwargs):
    response = await client.get(path, **kwargs)
    response.raise_for_status()
    return response.json()


async def _idle(catalog):
    await asyncio.wait_for(catalog._queue.join(), timeout=5)


def _observed(state, branch):
    return git_policy.ConfirmedBranchObservation(
        git_policy.BranchSyncState(state, "observed", branch, branch),
        datetime.now(UTC),
        git_policy.monotonic(),
    )


def _match(branch):
    return _observed(git_policy.BRANCH_IN_SYNC, branch)


@pytest.mark.parametrize("expired", [False, True])
async def test_catalog_returns_while_branch_refresh_is_blocked(
    client, db, catalog, mcp_rest, clock, monkeypatch, expired
):
    await repository.create_project(
        db, slug="sample", name="Sample", workspace_path="/sample"
    )
    await repository.create_project(db, slug="other", name="Other")
    started = asyncio.Event()
    unblock = asyncio.Event()

    async def check(workspace, branch, **_kwargs):
        assert workspace == "/sample"
        if not expired or clock[0] >= 60:
            started.set()
            await unblock.wait()
        return _match(branch)

    monkeypatch.setattr(git_policy, "observe_clone_branch", check)
    live = Mock(side_effect=AssertionError("live Git check in cached handler"))
    monkeypatch.setattr(project_policy, "clone_branch_state", live)
    previous = None
    if expired:
        await _projects(client)
        await _idle(catalog)
        previous = (await _projects(client))[1]["clone_branch_observation"][
            "checked_at"
        ]
        clock[0] = 60

    try:
        # The timeout protects a broken test; the assertion is the event order.
        result = await asyncio.wait_for(mcp_server.hub_list_projects(), timeout=5)
        await asyncio.wait_for(started.wait(), timeout=5)
        assert not unblock.is_set()
        rows = result.structuredContent["projects"]
        assert [row["slug"] for row in rows] == ["other", "sample"]
        assert rows[1]["clone_branch_observation"] == {
            "status": "stale" if expired else "unknown",
            "checked_at": previous,
        }
        assert rows[1]["clone_branch"]["state"] == ("match" if expired else "unknown")
        live.assert_not_called()
    finally:
        unblock.set()
    await _idle(catalog)


async def test_catalog_snapshot_reports_last_observation(
    client, db, tmp_path, catalog, mcp_rest, clock, monkeypatch
):
    project_id = await repository.create_project(
        db, slug="demo", name="Demo", workspace_path=str(tmp_path)
    )
    await repository.create_project(db, slug="no-workspace", name="No workspace")
    no_branch_id = await repository.create_project(
        db,
        slug="no-branch",
        name="No branch",
        workspace_path=str(tmp_path),
    )
    await repository.update_project(db, no_branch_id, default_branch="")
    requests = []
    remote = (0, "abc\trefs/heads/develop")
    configured_branch = "develop"
    origin = (0, "https://example.invalid/title.git")

    def run_git(path, *args):
        assert path == str(tmp_path)
        requests.append(args[0])
        if args[0] == "remote":
            return origin
        if args[0] == "config":
            return (0, configured_branch)
        if args[0] == "ls-remote":
            return remote
        assert args == ("rev-parse", "--git-dir")
        return (0, ".git")

    monkeypatch.setattr(git_policy, "_git", run_git)
    await _projects(client)
    await _idle(catalog)
    first, no_branch, no_workspace = await _projects(client)
    assert requests == ["rev-parse", "config", "remote", "ls-remote"]
    for unconfigured in (no_branch, no_workspace):
        assert unconfigured["clone_branch_observation"] == {
            "status": "unknown",
            "checked_at": None,
        }
        assert unconfigured["clone_branch"]["state"] == "unknown"
    observed = first["clone_branch_observation"]
    assert observed["status"] == "fresh"
    assert datetime.fromisoformat(observed["checked_at"]).utcoffset() == timedelta(0)
    assert first["clone_branch"]["state"] == "match"

    clock[0] = 59.999
    assert (await _projects(client))[0]["clone_branch_observation"] == observed
    remote = (1, "Timeout connecting to origin: secret-for-test")
    clock[0] = 60
    boundary = (await _projects(client))[0]
    await _idle(catalog)
    after_error = (await _projects(client))[0]
    for stale in (boundary, after_error):
        assert stale["clone_branch"] == first["clone_branch"]
        assert stale["clone_branch_observation"] == {
            "status": "stale",
            "checked_at": observed["checked_at"],
        }

    await repository.update_project(db, project_id, forge="gitverse")
    changed = (await _projects(client))[0]
    await _idle(catalog)
    assert changed["clone_branch_observation"] == {
        "status": "unknown",
        "checked_at": None,
    }
    assert changed["clone_branch"]["state"] == "unknown"
    configured_branch = "wrong-branch"
    origin = (0, "https://gitverse.ru/different/repo.git")
    await repository.update_project(db, project_id, repo="different/repo")
    await _projects(client)
    await _idle(catalog)
    result = await mcp_server.hub_list_projects()
    failed = result.structuredContent["projects"][0]
    assert failed["clone_branch"]["state"] == "unknown"
    assert "локальная настройка ветки отличается" in failed["clone_branch"]["reason"]
    assert "secret-for-test" not in json.dumps(failed, ensure_ascii=False)
    assert "branch=unknown unknown checked=never" in result.content[0].text

    clock[0] = 120
    remote = (0, "")
    await _projects(client)
    await _idle(catalog)
    result = await mcp_server.hub_list_projects()
    missing = result.structuredContent["projects"][0]
    assert missing["clone_branch"]["state"] == "missing"
    missing_meta = missing["clone_branch_observation"]
    assert missing_meta["status"] == "fresh"
    assert missing_meta["checked_at"] != observed["checked_at"]
    assert (
        f"branch=missing fresh checked={missing_meta['checked_at']}"
        in result.content[0].text
    )

    git_policy.forget_remote_presence(str(tmp_path), "unrelated")
    assert (await _projects(client))[0]["clone_branch_observation"] == missing_meta
    git_policy.forget_remote_presence(str(tmp_path), "develop")
    origin = (2, "secret-for-test: no origin")
    before = len(requests)
    invalidated = (await _projects(client))[0]
    await _idle(catalog)
    assert requests[before:] == ["rev-parse", "config", "remote"]
    assert invalidated["clone_branch_observation"] == {
        "status": "stale",
        "checked_at": missing_meta["checked_at"],
    }
    assert (await _projects(client))[0]["clone_branch_observation"]["status"] == "stale"


async def test_in_flight_result_cannot_undo_invalidation(
    client, db, catalog, clock, monkeypatch
):
    await repository.create_project(
        db, slug="demo", name="Demo", workspace_path="/sample"
    )
    started = asyncio.Event()
    unblock = asyncio.Event()
    calls = []

    async def check(path, branch, **_kwargs):
        calls.append((path, branch))
        if len(calls) == 1:
            started.set()
            await unblock.wait()
            return _observed(git_policy.BRANCH_MISSING, branch)
        return _match(branch)

    monkeypatch.setattr(git_policy, "observe_clone_branch", check)
    await _projects(client)
    await asyncio.wait_for(started.wait(), timeout=5)
    git_policy.forget_remote_presence("/sample", "develop")
    await _projects(client)
    unblock.set()
    await _idle(catalog)
    discarded = (await _projects(client))[0]
    assert discarded["clone_branch_observation"] == {
        "status": "unknown",
        "checked_at": None,
    }
    await _idle(catalog)
    accepted = (await _projects(client))[0]
    assert accepted["clone_branch_observation"]["status"] == "fresh"
    assert accepted["clone_branch"]["state"] == "match"
    assert calls == [("/sample", "develop")] * 2


async def test_project_retarget_does_not_confirm_stale_origin(
    client, db, tmp_path, catalog, clock, monkeypatch
):
    project_id = await repository.create_project(
        db,
        slug="demo",
        name="Demo",
        repo_name="owner/old",
        workspace_path=str(tmp_path),
    )
    origin = "git@github.com:owner/old.git"
    probes = []

    def run_git(_path, *args):
        if args[0] == "remote":
            return (0, origin)
        if args[0] == "ls-remote":
            probes.append(origin)
            return (0, "")  # Missing at whichever remote was contacted.
        return (0, "develop")

    monkeypatch.setattr(git_policy, "_git", run_git)
    await _projects(client)
    await _idle(catalog)
    assert (await _projects(client))[0]["clone_branch"]["state"] == "missing"

    await repository.update_project(db, project_id, repo="owner/new")
    await _projects(client)
    await _idle(catalog)
    unknown = (await _projects(client))[0]
    assert unknown["clone_branch"]["state"] == "unknown"
    assert "origin клона не подтверждает" in unknown["clone_branch"]["reason"]
    assert unknown["clone_branch_observation"] == {
        "status": "unknown",
        "checked_at": None,
    }
    assert probes == ["git@github.com:owner/old.git"]

    clock[0] = 60
    origin = "ssh://git@ssh.github.com:443/owner/new.git"
    await _projects(client)
    await _idle(catalog)
    confirmed = (await _projects(client))[0]
    assert confirmed["clone_branch"]["state"] == "missing"
    assert confirmed["clone_branch_observation"]["status"] == "fresh"
    assert probes[-1] == origin

    await repository.update_project(db, project_id, forge="gitverse")
    await _projects(client)
    await _idle(catalog)
    assert (await _projects(client))[0]["clone_branch"]["state"] == "unknown"
    assert len(probes) == 2


async def test_catalog_keeps_age_from_remote_success(
    client, db, tmp_path, catalog, clock, monkeypatch
):
    await repository.create_project(
        db,
        slug="demo",
        name="Demo",
        workspace_path=str(tmp_path),
    )
    real_probe = git_policy.observe_clone_branch

    async def delay_delivery(*args, **kwargs):
        sample = await real_probe(*args, **kwargs)
        clock[0] = 65
        return sample

    def run_git(_path, *args):
        if args[0] == "remote":
            return (0, "https://github.com/owner/demo.git")
        return (0, "develop")

    monkeypatch.setattr(git_policy, "_git", run_git)
    monkeypatch.setattr(git_policy, "observe_clone_branch", delay_delivery)
    await _projects(client)
    await _idle(catalog)
    sample = (await _projects(client))[0]
    assert sample["clone_branch"]["state"] == "match"
    assert sample["clone_branch_observation"]["status"] == "stale"
    assert sample["clone_branch_observation"]["checked_at"] is not None


async def test_catalog_throttles_a_return_to_a_recent_configuration(
    catalog, clock, monkeypatch
):
    calls = []

    async def check(path, branch, **_kwargs):
        calls.append(path)
        return _match(branch)

    monkeypatch.setattr(git_policy, "observe_clone_branch", check)
    first = _row(1)
    second = _row(1, workspace_path="/different")
    for row in (first, second, first):
        returned = catalog.list_views([row])[0]
        assert returned.clone_branch_observation.status == "unknown"
        await _idle(catalog)
    assert calls == ["/1", "/different"]
    clock[0] = 60
    catalog.list_views([first])
    await _idle(catalog)
    assert calls == ["/1", "/different", "/1"]


def _row(project_id, **kwargs):
    return {
        "id": project_id,
        "slug": f"project-{project_id}",
        "name": f"Project {project_id}",
        "repo": "",
        "workspace_path": f"/{project_id}",
        "default_branch": "develop",
        "forge": "github",
        **kwargs,
    }


async def test_branch_refresh_bounds_and_coalesces_checks(catalog, clock, monkeypatch):
    started = asyncio.Event()
    unblock = asyncio.Event()
    calls = Counter()
    active = 0

    async def check(path, branch, **_kwargs):
        nonlocal active
        active += 1
        calls[path] += 1
        try:
            assert active <= 3
            if active == 3:
                started.set()
            await unblock.wait()
            if path == "/1":
                raise OSError("secret-for-test")
            return _match(branch)
        finally:
            active -= 1

    monkeypatch.setattr(git_policy, "observe_clone_branch", check)
    rows = [_row(i) for i in range(1, 71)]
    assert len(catalog.list_views(rows)) == 70
    assert catalog._queue.qsize() == 64
    await asyncio.wait_for(started.wait(), timeout=5)
    assert active == 3

    async def read():
        return catalog.list_views(rows)

    concurrent = await asyncio.gather(*(read() for _ in range(5)))
    assert all(len(view) == 70 for view in concurrent)
    assert catalog._queue.qsize() == 64
    unblock.set()
    await _idle(catalog)
    assert len(calls) == 67
    later = catalog.list_views(rows)
    assert later[0].clone_branch.state == "unknown"
    assert "secret-for-test" not in later[0].clone_branch.reason
    assert later[1].clone_branch_observation.status == "fresh"
    await _idle(catalog)
    assert calls == Counter(f"/{i}" for i in range(1, 71))

    clock[0] = 59.999
    catalog.list_views(rows[:1])
    await _idle(catalog)
    assert calls["/1"] == 1
    clock[0] = 60
    catalog.list_views(rows[:1])
    await _idle(catalog)
    assert calls["/1"] == 2


async def test_catalog_stop_drops_queue_and_leaves_running_git_bounded(
    catalog, monkeypatch, tmp_path
):
    started = asyncio.Event()
    finished = asyncio.Event()
    unblock = Event()
    loop = asyncio.get_running_loop()
    commands = []

    def run_git(path, *args):
        commands.append((path, args[0]))
        loop.call_soon_threadsafe(started.set)
        unblock.wait(timeout=5)
        loop.call_soon_threadsafe(finished.set)
        return (0, ".git")

    monkeypatch.setattr(git_policy, "_git", run_git)
    rows = [_row(i, workspace_path=str(tmp_path)) for i in range(1, 10)]
    catalog.list_views(rows)
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.wait_for(catalog.stop(), timeout=5)
        assert not finished.is_set()
        assert catalog._queue.empty()
        assert not catalog._pending
        assert all(w.done() for w in catalog._workers)
    finally:
        unblock.set()
    await asyncio.wait_for(finished.wait(), timeout=5)
    assert 1 <= len(commands) <= 3
    assert {command for _, command in commands} == {"rev-parse"}
    assert all(
        row.clone_branch_observation.status == "unknown"
        for row in catalog.list_views(rows)
    )


async def test_project_catalog_rest_cli_mcp_compatibility(
    client,
    db,
    catalog,
    mcp_rest,
    monkeypatch,
    capsys,
):
    archive = await repository.create_project(db, slug="beta", name="Beta")
    await repository.create_project(db, slug="alpha", name="Alpha", forge="gitverse")
    await repository.update_project(db, archive, archived=True)
    legacy_branch = git_policy.BranchSyncState(
        git_policy.BRANCH_DIVERGED, "live", "develop", "main"
    )
    live = Mock(return_value=legacy_branch)
    monkeypatch.setattr(project_policy, "clone_branch_state", live)
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "agent-key": TokenIdentity("agent", "agent"),
            "human-key": TokenIdentity("human", "human"),
        },
    )
    assert (await client.get(_CACHED)).status_code == 401
    for token in ("agent-key", "human-key"):
        headers = {"Authorization": f"Bearer {token}"}
        legacy = await _projects(client, "/api/projects", headers=headers)
        cached = await _projects(client, headers=headers)
        assert (
            [row["slug"] for row in legacy]
            == [row["slug"] for row in cached]
            == ["alpha"]
        )
        assert set(cached[0]) - {"clone_branch_observation"} == set(legacy[0])
        assert "clone_branch_observation" not in legacy[0]
        assert legacy[0]["clone_branch"]["reason"] == "live"
        assert cached[0]["clone_branch_observation"]["status"] == "unknown"

    headers = {"Authorization": "Bearer agent-key"}
    monkeypatch.setattr(mcp_server, "_auth_headers", lambda: headers)
    with_archived = await _projects(
        client, f"{_CACHED}&include_archived=true", headers=headers
    )
    result = await mcp_server.hub_list_projects(include_archived=True)
    assert result.structuredContent["projects"] == with_archived
    assert [row["slug"] for row in with_archived] == ["alpha", "beta"]
    assert "@gitverse" in result.content[0].text
    assert "[archived] | branch=unknown unknown checked=never" in result.content[0].text
    live_count = live.call_count
    assert live_count == 2

    api = Mock(return_value=legacy)
    monkeypatch.setattr(cli, "_api", api)
    default = cli.build_parser().parse_args(["projects", "list"])
    assert default.func(default) == 0
    api.assert_called_once_with("GET", "/api/projects")
    assert json.loads(capsys.readouterr().out) == legacy
    api.reset_mock()
    api.return_value = with_archived
    cached_cli = cli.build_parser().parse_args(
        ["projects", "list", "--cached-branches", "--include-archived"]
    )
    assert cached_cli.func(cached_cli) == 0
    api.assert_called_once_with(
        "GET", "/api/projects?include_archived=true&branch_check=cached"
    )
    assert json.loads(capsys.readouterr().out) == with_archived
