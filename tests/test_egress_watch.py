"""The hub looks at its own way out (#1645).

No test here touches a real network: the probe runs over ``httpx.MockTransport``
and the watcher over a fake clock.
"""

from __future__ import annotations

import asyncio
import io
import json
import socket
import ssl
import time
from contextlib import asynccontextmanager, redirect_stdout
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from hub import config
from hub import repository as repo
from hub.services import egress_watch as ew
from hub.services.release_alert import utc_stamp

BASE = datetime(2026, 10, 9, 10, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh_watch(monkeypatch):
    monkeypatch.setattr(config, "EGRESS_DOWN_AFTER", 3)
    monkeypatch.setattr(config, "EGRESS_PROBE_SECONDS", 120)


class Script:
    """A transport that answers from a script: a status code or an exception."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = 0

    def __call__(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "HEAD"
            step = self.steps[self.calls]
            self.calls += 1
            if isinstance(step, Exception):
                raise step
            return httpx.Response(step)

        return httpx.MockTransport(handler)


def _gai() -> httpx.ConnectError:
    err = httpx.ConnectError("name or service not known")
    err.__cause__ = socket.gaierror("x")
    return err


class Clock:
    def __init__(self):
        self.now = BASE

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: int = 120) -> None:
        self.now += timedelta(seconds=seconds)


async def _events(db, kind: str) -> list[dict]:
    cur = await db.execute(
        "SELECT payload, project_id FROM events WHERE kind=? ORDER BY id", (kind,)
    )
    return [
        {**json.loads(r["payload"]), "project_id": r["project_id"]}
        for r in await cur.fetchall()
    ]


async def test_one_episode_one_down_one_restored(db):
    script = Script(
        [
            httpx.ConnectError("boom"),  # one failure, then success: no episode
            401,  # an answer of any kind is success
            httpx.ConnectTimeout("t"),  # the triple starts here
            httpx.ConnectError("tls", request=None),
            _gai(),  # a different code each time
            httpx.ReadTimeout("t"),  # the episode goes on
            403,  # restored
        ]
    )
    clock = Clock()
    watch = ew.EgressWatch(lambda: ew.probe(transport=script()), clock)
    for _ in script.steps:
        await watch.step(db)
        clock.tick()

    downs = await _events(db, ew.KIND_DOWN)
    restored = await _events(db, ew.KIND_RESTORED)
    assert len(downs) == 1
    assert len(restored) == 1
    first_of_triple = utc_stamp(BASE + timedelta(minutes=4))
    assert downs[0]["since"] == first_of_triple
    assert restored[0]["since"] == first_of_triple
    assert restored[0]["minutes"] == 8  # 10:04 -> 10:12
    assert downs[0]["project_id"] is None  # instance-wide, not per project
    assert await ew.active_episode(db) is None


async def test_probe_codes_are_fixed_and_a_status_is_success():
    cases = [
        (httpx.ConnectTimeout("x"), "timeout"),
        (_gai(), "dns"),
        (httpx.ConnectError("refused"), "connect"),
        (httpx.ProxyError("p"), "proxy"),
        (RuntimeError("secret https://u:p@h/"), "other"),
        (500, ""),
        (429, ""),
    ]
    for step, code in cases:
        assert await ew.probe(transport=Script([step])()) == code
    assert set(ew.REASON_TEXT) == {"dns", "connect", "tls", "timeout", "proxy", "other"}


async def test_probe_follows_no_redirect():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://other.example/"})

    assert await ew.probe(transport=httpx.MockTransport(handler)) == ""
    assert len(calls) == 1


async def test_probe_is_bounded_and_never_blocks_the_loop(client, db):
    async def hang(request):
        await asyncio.sleep(30)
        return httpx.Response(200)

    transport = httpx.MockTransport(hang)
    started = time.monotonic()
    probing = asyncio.create_task(ew.probe(transport=transport, deadline=0.4))
    # The hub keeps answering while the probe waits for its whole deadline.
    slowest = 0.0
    while not probing.done():
        t0 = time.monotonic()
        assert (await client.get("/healthz")).status_code == 200
        slowest = max(slowest, time.monotonic() - t0)
        await asyncio.sleep(0.02)
    assert await probing == "timeout"
    assert time.monotonic() - started < 0.4 + 0.5
    assert slowest < 0.3

    # No overlap, and shutdown cancels the watcher.
    running = 0
    peak = 0
    runs = 0

    async def slow_probe() -> str:
        nonlocal running, peak, runs
        running += 1
        peak = max(peak, running)
        runs += 1
        await asyncio.sleep(0.05)
        running -= 1
        return ""

    @asynccontextmanager
    async def open_db():
        yield db

    async def quick_sleep(_seconds):
        await asyncio.sleep(0)

    watcher = asyncio.create_task(
        ew.run_loop(open_db, ew.EgressWatch(slow_probe), sleep=quick_sleep)
    )
    await asyncio.sleep(0.3)
    watcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watcher
    assert runs >= 3
    assert peak == 1


async def test_start_poller_returns_the_watcher_to_shutdown():
    from hub import poller

    async def idle(*_a, **_k):
        await asyncio.sleep(60)

    loop_mock = AsyncMock(side_effect=idle)
    app = SimpleNamespace(state=SimpleNamespace(db=None))
    with (
        patch.object(poller, "_poll_running_tasks", idle),
        patch.object(poller, "_session_reaper", idle),
        patch.object(poller, "_drift_watch", idle),
        patch.object(poller, "_red_base_watch", idle),
        patch.object(poller, "arm_workspace_hooks", idle),
        patch.object(ew, "run_loop", loop_mock),
    ):
        main = poller.start_poller(app)
        await asyncio.sleep(0)
        watcher = app.state.egress_task
        assert watcher is not main
        loop_mock.assert_called_once()
        watcher.cancel()
        main.cancel()


async def _seed_open_episode(db, minutes_ago: int = 7, code: str = "timeout"):
    now = datetime.now(UTC)
    await repo.insert_event(
        db,
        kind=ew.KIND_DOWN,
        actor="hub",
        payload={
            "since": utc_stamp(now - timedelta(minutes=minutes_ago)),
            "reason_code": code,
        },
    )
    await ew.record_heartbeat(db, now)
    await db.commit()


def _text(result) -> str:
    from mcp.types import CallToolResult, TextContent

    if isinstance(result, CallToolResult):
        return "\n".join(b.text for b in result.content if isinstance(b, TextContent))
    return str(result)


async def test_open_episode_is_shown_on_every_surface(client, db):
    from hub import cli, mcp_server
    from hub.services.prod_state import format_prod_state

    await _seed_open_episode(db)
    needle = "GitHub недоступен с сервера 7 мин"

    api = (await client.get("/api/prod-state")).json()
    assert api["egress"]["state"] == "down"
    assert needle in format_prod_state(api)

    card = (await client.get("/")).text
    card = card[card.index('id="prod-state"') :]
    assert needle in card
    assert card.index(needle) < card.index("Успешных выкатов")

    health = (await client.get("/health")).json()["egress"]
    out = io.StringIO()
    with (
        patch.object(cli, "_api", return_value={**_health_stub(), "egress": health}),
        redirect_stdout(out),
    ):
        assert cli.cmd_health(SimpleNamespace(json=False)) == 0
    assert needle in out.getvalue()
    assert needle in mcp_server._format_health({**_health_stub(), "egress": health})

    async def via_rest(path, *args, **kwargs):
        if path == "/health":
            return (await client.get(path)).json()
        if path == "/api/diagnostics/identity" or path == "/api/whoami":
            raise mcp_server.HubApiError({"message": "identity down"})
        return {"tasks": [], "next_cursor": None}

    for budget in (None, 600):  # prose is cut from the tail
        with patch.object(mcp_server, "_api_get", side_effect=via_rest):
            text = _text(await mcp_server.hub_my_context(max_chars=budget))
        if text.startswith("{"):  # a capped response wraps the text in JSON
            text = json.loads(text)["message"]
        lines = text.splitlines()
        assert lines[0] == "## Hub Context (no task)"
        assert lines[1].startswith("Instance:")
        assert needle in lines[2], lines[:4]


def _health_stub() -> dict:
    return {
        "status": "ok",
        "app_version": "x",
        "bind_host": "h",
        "bind_port": 1,
        "auth_required": False,
        "auth_disabled": False,
        "env_tokens_configured": False,
        "vast_enabled": False,
    }


async def test_no_alert_line_without_an_episode(client, db):
    from hub.services.prod_state import format_prod_state

    await ew.record_heartbeat(db, datetime.now(UTC))
    await db.commit()
    api = (await client.get("/api/prod-state")).json()
    assert api["egress"]["state"] == "up"
    assert "GitHub недоступен" not in format_prod_state(api)


async def test_open_episode_survives_prune_and_restart(db):
    async def seed(kind, payload, days):
        eid = await repo.insert_event(db, kind=kind, actor="hub", payload=payload)
        await db.execute(
            "UPDATE events SET created_at=datetime('now', ?) WHERE id=?",
            (f"-{days} days", eid),
        )

    await seed(ew.KIND_DOWN, {"since": "2026-09-01 10:00:00", "reason_code": "dns"}, 40)
    await seed(
        ew.KIND_RESTORED,
        {"since": "2026-09-01 10:00:00", "reason_code": "dns", "minutes": 5},
        40,
    )
    await seed(ew.KIND_DOWN, {"since": "2026-09-19 10:00:00", "reason_code": "tls"}, 20)
    await seed("something_else", {}, 30)
    await db.commit()

    await repo.prune_events(db)
    await db.commit()
    cur = await db.execute("SELECT kind FROM events ORDER BY id")
    assert [r["kind"] for r in await cur.fetchall()] == [ew.KIND_DOWN]
    assert (await ew.active_episode(db))["since"] == "2026-09-19 10:00:00"

    # A restart that still sees the outage writes no second egress_down.
    failing = Script([httpx.ConnectTimeout("t")] * 3)
    clock = Clock()
    again = ew.EgressWatch(lambda: ew.probe(transport=failing()), clock)
    for _ in range(3):
        await again.step(db)
    assert len(await _events(db, ew.KIND_DOWN)) == 1

    # ...and one that sees the way out working closes the original episode once.
    fresh = ew.EgressWatch(lambda: ew.probe(transport=Script([200])()), Clock())
    await fresh.step(db)
    await fresh.step(db)
    restored = await _events(db, ew.KIND_RESTORED)
    assert len(restored) == 1
    assert restored[0]["since"] == "2026-09-19 10:00:00"
    assert len(await _events(db, ew.KIND_DOWN)) == 1
    assert await ew.active_episode(db) is None


async def test_status_is_unknown_when_the_watcher_is_silent(db):
    await _seed_open_episode(db)
    now = datetime.now(UTC)
    await ew.record_heartbeat(db, now - timedelta(seconds=3 * 120 + 5), interval=120)
    await db.commit()
    status = await ew.egress_status(db, now)
    assert status["state"] == "unknown"
    await ew.record_heartbeat(db, now - timedelta(seconds=3 * 120 - 5), interval=120)
    await db.commit()
    assert (await ew.egress_status(db, now))["state"] == "down"


def test_config_is_clamped(monkeypatch):
    monkeypatch.setenv("HAIPLANE_EGRESS_PROBE_SECONDS", "5")
    assert config._int_env("EGRESS_PROBE_SECONDS", 120, 30, 900) == 120
    monkeypatch.setenv("HAIPLANE_EGRESS_PROBE_SECONDS", "300")
    assert config._int_env("EGRESS_PROBE_SECONDS", 120, 30, 900) == 300
    monkeypatch.setenv("HAIPLANE_EGRESS_PROBE_SECONDS", "x")
    assert config._int_env("EGRESS_PROBE_SECONDS", 120, 30, 900) == 120
    monkeypatch.setattr(config, "EGRESS_PROBE_URL", "http://insecure.example/")
    assert ew.probe_url() == ew.DEFAULT_URL


async def test_tls_failure_is_found_in_the_exception_chain():
    # A real ssl.SSLError behind an httpx.ConnectError, by cause and by context.
    by_cause = httpx.ConnectError("handshake")
    by_cause.__cause__ = ssl.SSLError("certificate verify failed")
    by_context = httpx.ConnectError("handshake")
    by_context.__context__ = ssl.SSLCertVerificationError("expired")
    assert await ew.probe(transport=Script([by_cause])()) == "tls"
    assert await ew.probe(transport=Script([by_context])()) == "tls"
    # ...and a bare ConnectError is not TLS.
    assert await ew.probe(transport=Script([httpx.ConnectError("x")])()) == "connect"


async def test_probe_url_with_userinfo_is_never_used(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    for bad in (
        "https://user:pass@example.org/",  # pragma: allowlist secret
        "https://tok@example.org/",
        "http://example.org/",
        "https:///nohost",
        "ftp://example.org",
    ):
        monkeypatch.setattr(config, "EGRESS_PROBE_URL", bad)
        assert ew.probe_url() == ew.DEFAULT_URL, bad
        await ew.probe(transport=transport)
        await ew.probe(bad, transport=transport)
    assert {u for u, _ in seen} == {ew.DEFAULT_URL}
    assert all(auth is None for _, auth in seen)

    monkeypatch.setattr(config, "EGRESS_PROBE_URL", "https://status.example.org/ping")
    assert ew.probe_url() == "https://status.example.org/ping"


async def test_concurrent_workers_open_and_close_one_episode(db, db_dsn):
    from hub.db import connect

    other = await connect(db_dsn)
    try:
        clock = Clock()
        fail = Script([httpx.ConnectTimeout("t")] * 10)
        ok = Script([200] * 10)
        a = ew.EgressWatch(lambda: ew.probe(transport=fail()), clock)
        b = ew.EgressWatch(lambda: ew.probe(transport=fail()), clock)
        await a.step(db)
        await a.step(db)  # streak 2 stored; the third failure opens it
        await asyncio.gather(a.step(db), b.step(other))
        assert len(await _events(db, ew.KIND_DOWN)) == 1

        a = ew.EgressWatch(lambda: ew.probe(transport=ok()), clock)
        b = ew.EgressWatch(lambda: ew.probe(transport=ok()), clock)
        await asyncio.gather(a.step(db), b.step(other))
        assert len(await _events(db, ew.KIND_RESTORED)) == 1
        assert len(await _events(db, ew.KIND_DOWN)) == 1
    finally:
        await other.close()


async def test_failed_write_does_not_refresh_the_heartbeat(db):
    clock = Clock()
    ok = ew.EgressWatch(lambda: ew.probe(transport=Script([200])()), clock)
    assert await ok.step(db) is True
    first = (await ew.egress_status(db, BASE))["checked_at"]
    assert first == utc_stamp(BASE)

    fail = Script([httpx.ConnectTimeout("t")] * 6)
    watch = ew.EgressWatch(lambda: ew.probe(transport=fail()), clock)
    for _ in range(2):  # two stored failures: the count is 2, no episode yet
        clock.tick()
        assert await watch.step(db) is True
    stored = (await ew.egress_status(db, clock.now))["checked_at"]
    with patch.object(repo, "insert_event", AsyncMock(side_effect=OSError("disk"))):
        clock.tick()
        assert await watch.step(db) is False  # the 3rd cannot store the episode
    assert await _events(db, ew.KIND_DOWN) == []
    status = await ew.egress_status(db, clock.now + timedelta(seconds=3 * 120))
    assert status["checked_at"] == stored  # not refreshed by a failed write
    assert status["state"] == "unknown"  # never a false "up"


async def test_heartbeat_and_count_survive_a_restart(db, db_dsn):
    from hub.db import connect

    clock = Clock()
    one = ew.EgressWatch(
        lambda: ew.probe(transport=Script([httpx.ConnectTimeout("t")] * 2)()), clock
    )
    # Two failures stored by the first process...
    for _ in range(2):
        clock.tick()
        # fresh transport per step: Script above only answers its first call
        one = ew.EgressWatch(
            lambda: ew.probe(transport=Script([httpx.ConnectTimeout("t")])()), clock
        )
        await one.step(db)
    # ...a new process on a new connection sees heartbeat and count.
    fresh_conn = await connect(db_dsn)
    try:
        status = await ew.egress_status(fresh_conn, clock.now)
        assert status["state"] == "up"
        assert status["checked_at"] == utc_stamp(clock.now)
        clock.tick()
        two = ew.EgressWatch(
            lambda: ew.probe(transport=Script([httpx.ConnectTimeout("t")])()), clock
        )
        await two.step(fresh_conn)  # the third in a row, across the restart
        assert len(await _events(db, ew.KIND_DOWN)) == 1
        assert (await ew.egress_status(fresh_conn, clock.now))["state"] == "down"
    finally:
        await fresh_conn.close()


async def test_old_count_from_a_silent_watcher_is_not_in_a_row(db):
    clock = Clock()
    for _ in range(2):
        await ew.EgressWatch(
            lambda: ew.probe(transport=Script([httpx.ConnectTimeout("t")])()), clock
        ).step(db)
    clock.tick(3 * 120 + 60)  # the watcher was silent for longer than 3 intervals
    await ew.EgressWatch(
        lambda: ew.probe(transport=Script([httpx.ConnectTimeout("t")])()), clock
    ).step(db)
    assert await _events(db, ew.KIND_DOWN) == []


async def test_context_keeps_the_alert_with_a_real_identity_and_a_tight_cap(client, db):
    from hub import mcp_server

    await _seed_open_episode(db)

    async def via_rest(path, *args, **kwargs):
        if path == "/health":
            return (await client.get(path)).json()
        if path == "/api/diagnostics/identity":
            return {
                "username": "steward",
                "role": "agent",
                "principal_id": 1,
                "auth_source": "db",
                "permissions_count": 12,
                "base_url": "https://agenthai.ru",
                "server_id": "vm-5c8197",
                "connected_via": "https://agenthai.ru",
                "workspace_path": "/srv/haiplane/workspace/repo",
                "workspace_branch": "develop",
                "workspace_mode": "worktree",
            }
        return {"tasks": [], "next_cursor": None}

    with patch.object(mcp_server, "_api_get", side_effect=via_rest):
        text = _text(await mcp_server.hub_my_context(max_chars=600))
    if text.startswith("{"):
        text = json.loads(text)["message"]
    lines = text.splitlines()
    assert lines[0] == "## Hub Context (no task)"
    assert lines[1].startswith("Instance:")
    assert "GitHub недоступен с сервера 7 мин" in lines[2], text[:300]


async def test_context_says_when_the_egress_state_could_not_be_read():
    from hub import mcp_server

    unread = "Состояние связи сервера с GitHub не прочитано"

    async def broken(path, *args, **kwargs):
        if path == "/health":
            raise mcp_server.HubApiError({"message": "HTTP 502"})
        return {}

    async def slow(path, *args, **kwargs):
        if path == "/health":
            await asyncio.sleep(30)
        return {}

    with patch.object(mcp_server, "_api_get", side_effect=broken):
        assert unread in _text(await mcp_server.hub_my_context())
    with (
        patch.object(mcp_server, "_api_get", side_effect=slow),
        patch.object(mcp_server, "_EGRESS_READ_SECONDS", 0.05),
    ):
        started = time.monotonic()
        text = _text(await mcp_server.hub_my_context())
    assert unread in text
    assert time.monotonic() - started < 5


async def _run_lifespan_with_watcher(watcher_factory):
    import aiosqlite

    from hub.db import _SCHEMA, _migrate

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_SCHEMA)
    await _migrate(conn)

    from hub.app import app, lifespan

    @asynccontextmanager
    async def noop(_app):
        yield

    holder = {}

    def fake_start_poller(a):
        holder["task"] = a.state.egress_task = asyncio.create_task(watcher_factory())
        return SimpleNamespace(cancel=lambda: None)

    with (
        patch("hub.app.get_db", AsyncMock(return_value=conn)),
        patch("hub.app.start_poller", fake_start_poller),
        patch(
            "hub.app._mcp_streamable_app",
            SimpleNamespace(router=SimpleNamespace(lifespan_context=noop)),
        ),
    ):
        async with lifespan(app):
            await asyncio.sleep(0)  # let the watcher start
    return holder["task"]


async def test_shutdown_waits_for_the_watcher_to_unwind():
    cleaned = []

    async def watcher():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # cleanup that needs the loop
            cleaned.append("closed")
            raise

    task = await _run_lifespan_with_watcher(watcher)
    assert cleaned == ["closed"]
    assert task.done()


async def test_shutdown_survives_a_watcher_that_already_failed():
    async def watcher():
        raise RuntimeError("died early")

    task = await _run_lifespan_with_watcher(watcher)  # must not raise
    assert task.done()


_HANGING_DNS_CHILD = """
import asyncio, socket, sys, time
def hanging_getaddrinfo(*a, **k):
    time.sleep(60)
socket.getaddrinfo = hanging_getaddrinfo
from hub.services import egress_watch as ew
code = asyncio.run(ew.probe(deadline=1.0))
print("code=" + code, flush=True)
"""


def test_a_hanging_dns_probe_does_not_hold_process_exit(tmp_path):
    """Резолв, который не возвращается 60 с, не держит выход процесса (#1667)."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if not k.startswith("HAIPLANE_")}
    env["PYTHONPATH"] = str(root)
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-u", "-c", _HANGING_DNS_CHILD],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(root),
        timeout=40,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "code=timeout" in proc.stdout, proc.stdout + proc.stderr
    assert time.monotonic() - started < 30


async def test_the_watcher_does_not_probe_at_startup():
    """Первая проба — через интервал, не при старте (#1667)."""
    probes = []

    async def probe_fn() -> str:
        probes.append(1)
        return ""

    class Stop(Exception):
        pass

    slept = []

    async def first_sleep(seconds):
        slept.append(seconds)
        raise Stop

    @asynccontextmanager
    async def open_db():
        yield SimpleNamespace(in_transaction=False)

    with pytest.raises(Stop):
        await ew.run_loop(open_db, ew.EgressWatch(probe_fn), sleep=first_sleep)
    assert probes == []
    assert slept == [ew.interval_seconds()]


async def test_the_switch_off_keeps_the_watcher_from_starting(monkeypatch):
    from hub import poller

    async def idle(*_a, **_k):
        await asyncio.sleep(60)

    loop_mock = AsyncMock(side_effect=idle)
    monkeypatch.setenv("HAIPLANE_EGRESS_WATCH", "off")
    app = SimpleNamespace(state=SimpleNamespace(db=None))
    with (
        patch.object(poller, "_poll_running_tasks", idle),
        patch.object(poller, "_session_reaper", idle),
        patch.object(poller, "_drift_watch", idle),
        patch.object(poller, "_red_base_watch", idle),
        patch.object(poller, "arm_workspace_hooks", idle),
        patch.object(ew, "run_loop", loop_mock),
    ):
        main = poller.start_poller(app)
        await asyncio.sleep(0)
        loop_mock.assert_not_called()
        assert app.state.egress_task is None
        assert "hub-egress-watch" not in app.state.background_tasks
        for t in app.state.background_tasks.values():
            t.cancel()
        main.cancel()


@pytest.mark.parametrize("value", ["", "on", "1", "ON"])
def test_the_watcher_is_on_unless_switched_off(monkeypatch, value):
    monkeypatch.setenv("HAIPLANE_EGRESS_WATCH", value)
    assert ew.watch_enabled() is True


@pytest.mark.parametrize("value", ["off", "OFF", " Off "])
def test_the_watcher_off_switch_values(monkeypatch, value):
    monkeypatch.setenv("HAIPLANE_EGRESS_WATCH", value)
    assert ew.watch_enabled() is False


async def test_hung_probes_do_not_pile_up_worker_threads():
    """P2 (#1667): не больше одного незавершённого worker пробы."""
    import threading

    release = threading.Event()
    started = []

    def blocked(url, deadline):
        started.append(1)
        release.wait(30)
        return ""

    def alive() -> int:
        return sum(
            1
            for t in threading.enumerate()
            if t.name == "egress-probe" and t.is_alive()
        )

    ew._probe_thread = None
    try:
        with patch.object(ew, "_head_in_thread", blocked):
            codes = [await ew.probe(deadline=0.1) for _ in range(5)]
            assert alive() <= 1
        assert codes == ["timeout"] * 5
        assert len(started) == 1  # the other four did not start a request
    finally:
        release.set()
        if ew._probe_thread is not None:
            ew._probe_thread.join(5)
        ew._probe_thread = None
