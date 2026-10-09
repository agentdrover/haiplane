"""The hub looks at its own way out (#1645).

No test here touches a real network: the probe runs over ``httpx.MockTransport``
and the watcher over a fake clock.
"""

from __future__ import annotations

import asyncio
import io
import json
import socket
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
    ew.reset_snapshot()
    monkeypatch.setattr(config, "EGRESS_DOWN_AFTER", 3)
    monkeypatch.setattr(config, "EGRESS_PROBE_SECONDS", 120)
    yield
    ew.reset_snapshot()


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
    await db.commit()
    ew.note_probe(now)


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

    ew.note_probe(datetime.now(UTC))
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
    ew.note_probe(now - timedelta(seconds=3 * 120 + 5), interval=120)
    status = await ew.egress_status(db, now)
    assert status["state"] == "unknown"
    ew.note_probe(now - timedelta(seconds=3 * 120 - 5), interval=120)
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
