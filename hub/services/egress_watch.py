"""The hub looks at its own way out (#1645).

On 09.10.2026 the server's whole foreign traffic timed out for 8 h 12 min (the
tunnel of the external geo-routing lost its node, fail-closed). The hub said
nothing: verdicts hung on ``git fetch``, delivery answered ``gh_error``, the
owner learned from commands that "hung". This module is the missing look:

- ``probe`` — one HTTPS HEAD to a control point (default api.github.com) with
  no credentials, over the route the hub's own HTTP client uses (``trust_env``
  keeps HTTP(S)_PROXY / NO_PROXY as for any httpx client of the hub). ANY HTTP
  answer is success — 401/403/429 prove the way out works. Only a transport
  failure is a failure, and it is reduced to a fixed ``reason_code``: the text
  of an exception can carry a URL with credentials or a local path, and this
  state is shown on the public ``/health``.
- ``EgressWatch`` — the watcher's step: probe (network, NO db transaction
  open), then count. ``down_after`` failures in a row open an episode, one
  success closes it. The episode lives in ``events`` (``egress_down`` /
  ``egress_restored``, instance-wide), like the release alert (#1420), so it
  survives a restart without being written twice.
- ``egress_status`` — what readers print; reads the stored episode and the
  time of the last probe, never the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import ssl
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from hub import config
from hub import repository as repo
from hub.db import fetchall, log_activity, write_transaction
from hub.services.release_alert import minutes_since, utc_stamp

log = logging.getLogger("hub")

KIND_DOWN, KIND_RESTORED = repo.EGRESS_KINDS

DEFAULT_URL = "https://api.github.com/"
DEADLINE_SECONDS = 5.0
MIN_INTERVAL, MAX_INTERVAL, DEFAULT_INTERVAL = 30, 900, 120
DEFAULT_DOWN_AFTER = 3
# A state older than this many intervals means the watcher itself is not
# running — and "up" from a dead watcher is the lie this module exists to end.
STALE_AFTER_INTERVALS = 3

UP, DOWN, UNKNOWN = "up", "down", "unknown"

# The only values that ever leave the hub. Text per code is a fixed template.
REASON_TEXT = {
    "dns": "не разрешается имя",
    "connect": "соединение не устанавливается",
    "tls": "ошибка TLS",
    "timeout": "тайм-аут",
    "proxy": "ошибка прокси",
    "other": "сетевая ошибка",
}


def reason_code_of(exc: BaseException) -> str:
    """Reduce a transport failure to a code from REASON_TEXT, never to text."""
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, httpx.ProxyError):
        return "proxy"
    chain: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and len(chain) < 8:
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    if any(isinstance(e, socket.gaierror) for e in chain):
        return "dns"
    if any(isinstance(e, ssl.SSLError) for e in chain):
        return "tls"
    if isinstance(exc, httpx.ConnectError):
        return "connect"
    return "other"


def _safe_url(raw: str | None) -> str:
    """The URL if it is plain https with a host and NO userinfo, else the default.

    httpx turns ``a URL with userinfo`` into an ``Authorization: Basic``
    header, and the probe is defined as unauthenticated.
    """
    url = (raw or "").strip()
    try:
        parts = urlsplit(url)
        ok = (
            parts.scheme == "https" and bool(parts.hostname) and "@" not in parts.netloc
        )
    except ValueError:
        ok = False
    return url if ok else DEFAULT_URL


def watch_enabled() -> bool:
    """``HAIPLANE_EGRESS_WATCH=off`` keeps the watcher from starting (#1667).

    Read at call time, not at import: the switch is for a process that must
    not touch the network (a test child, a stopped server) and the answer
    follows the environment the process was started with.
    """
    return config.env_get("EGRESS_WATCH", "on").strip().lower() != "off"


def probe_url() -> str:
    return _safe_url(config.EGRESS_PROBE_URL)


def interval_seconds() -> int:
    return config.EGRESS_PROBE_SECONDS


def down_after() -> int:
    return config.EGRESS_DOWN_AFTER


def _head_in_thread(url: str, deadline: float) -> str:
    """The probe itself, blocking: runs in a thread of its own, see ``probe``."""
    try:
        with httpx.Client(
            follow_redirects=False, timeout=deadline, verify=True
        ) as client:
            client.head(url)
        return ""
    except Exception as exc:  # noqa: BLE001 - every failure becomes a code
        return reason_code_of(exc)


async def _probe_in_daemon_thread(url: str, deadline: float) -> str:
    """Run the blocking probe in a DAEMON thread and wait for it cancellably.

    ``getaddrinfo`` cannot be interrupted. In the loop's default executor (or
    in anyio's worker, which httpx's async client uses and which is not
    abandoned on cancel) a resolver that does not return holds the wait itself
    and then the exit of the interpreter (#1667). A daemon thread holds
    neither: the awaiting side is a plain future, ``asyncio.timeout`` and
    task cancellation both reach it, and the interpreter leaves without the
    thread.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def deliver(code: str) -> None:
        if not future.done():
            future.set_result(code)

    def work() -> None:
        code = _head_in_thread(url, deadline)
        try:
            loop.call_soon_threadsafe(deliver, code)
        except RuntimeError:  # the loop is already closed: nobody waits
            pass

    threading.Thread(target=work, name="egress-probe", daemon=True).start()
    return await future


async def probe(
    url: str | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    deadline: float = DEADLINE_SECONDS,
) -> str:
    """``""`` when any HTTP answer came back, else a fixed reason code.

    The deadline covers DNS, connect, TLS and the answer together (httpx's own
    timeouts apply per phase, so they alone would allow several times that).
    No redirects, no retries. Never raises.

    Without an injected ``transport`` the request goes out from a daemon
    thread (``_probe_in_daemon_thread``): DNS cannot be cancelled, and a
    resolver that hangs must neither outlive the deadline nor hold the exit of
    the process.
    """
    target = _safe_url(url) if url else probe_url()
    try:
        async with asyncio.timeout(deadline):
            if transport is None:
                return await _probe_in_daemon_thread(target, deadline)
            async with httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                timeout=deadline,
                verify=True,
            ) as client:
                await client.head(target)
        return ""
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure becomes a code
        return reason_code_of(exc)


# -- the stored episode -------------------------------------------------------


def _payload_of(row: Any) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_code(value: Any) -> str:
    return value if value in REASON_TEXT else "other"


async def active_episode(db: Any) -> dict[str, Any] | None:
    """The open episode (its newest pair event is ``egress_down``), or None."""
    rows = await fetchall(
        db,
        "SELECT kind, payload, created_at FROM events "
        "WHERE kind IN (?, ?) ORDER BY id DESC LIMIT 1",
        (KIND_DOWN, KIND_RESTORED),
    )
    if not rows or rows[0]["kind"] != KIND_DOWN:
        return None
    payload = _payload_of(rows[0])
    return {
        "since": str(payload.get("since") or rows[0]["created_at"] or ""),
        "reason_code": _safe_code(payload.get("reason_code")),
    }


_TS = "%Y-%m-%d %H:%M:%S"


def _parse(stamp: Any) -> datetime | None:
    try:
        return datetime.strptime(str(stamp), _TS).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


async def _heartbeat(db: Any) -> Any:
    rows = await fetchall(
        db,
        "SELECT checked_at, interval_seconds, streak, first_fail_at "
        "FROM egress_state WHERE id=1",
    )
    return rows[0] if rows else None


async def record_heartbeat(
    db: Any,
    checked_at: datetime,
    *,
    ok: bool = True,
    reason_code: str = "",
    streak: int = 0,
    first_fail_at: str = "",
    interval: int | None = None,
) -> None:
    """Write the single heartbeat row. No commit: the caller's transaction."""
    await db.execute(
        "INSERT INTO egress_state (id, checked_at, interval_seconds, ok, "
        "reason_code, streak, first_fail_at) VALUES (1, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET checked_at=excluded.checked_at, "
        "interval_seconds=excluded.interval_seconds, ok=excluded.ok, "
        "reason_code=excluded.reason_code, streak=excluded.streak, "
        "first_fail_at=excluded.first_fail_at",
        (
            utc_stamp(checked_at),
            interval or interval_seconds(),
            int(ok),
            reason_code,
            streak,
            first_fail_at,
        ),
    )


async def egress_status(db: Any, now: datetime | None = None) -> dict[str, Any]:
    """Typed state for /health, prod-state and the context. No network.

    ``unknown`` = no heartbeat, or the last one is older than three intervals:
    the watcher is not running, or its writes fail. The heartbeat is stored
    with the episode transition (same transaction), so it is never fresher than
    the state it vouches for. An open episode's ``since`` and ``reason_code``
    are still reported when unknown — the last thing the hub knew — but the
    state does not claim it is current.
    """
    now = now or datetime.now(UTC)
    episode = await active_episode(db)
    beat = await _heartbeat(db)
    seen = _parse(beat["checked_at"]) if beat else None
    fresh = seen is not None and (
        now - seen
    ).total_seconds() <= STALE_AFTER_INTERVALS * int(beat["interval_seconds"])
    state = UNKNOWN if not fresh else (DOWN if episode else UP)
    return {
        "state": state,
        "since": episode["since"] if episode else None,
        "checked_at": str(beat["checked_at"]) if beat else None,
        "reason_code": episode["reason_code"] if episode else None,
    }


def egress_lines(egress: Any, now: datetime | None = None) -> list[str]:
    """The alert line, from typed fields only; empty when there is no alert."""
    if not isinstance(egress, dict) or not egress.get("since"):
        return []
    if egress.get("state") not in (DOWN, UNKNOWN):
        return []
    minutes = egress.get("minutes")
    if minutes is None:
        minutes = minutes_since(str(egress["since"]), now)
    held = "время не прочитано" if minutes is None else f"{minutes} мин"
    reason = REASON_TEXT[_safe_code(egress.get("reason_code"))]
    line = (
        f"GitHub недоступен с сервера {held} ({reason}): "
        "вердикты и доставка PR будут ждать тайм-аутов"
    )
    if egress["state"] == UNKNOWN:
        line += " · проба давно не отвечала, данные могли устареть"
    return [line]


# -- the step -----------------------------------------------------------------


class EgressWatch:
    """One probe-and-count step; the loop around it lives in the poller.

    The count, the episode and the heartbeat all live in the database and move
    in ONE ``BEGIN IMMEDIATE`` transaction: two workers (or a worker and a
    restart) cannot both see "no episode" and both open one, and a write that
    fails leaves the heartbeat stale instead of vouching for a state that was
    never stored. Every process may run its own watcher; the database, not
    process ownership, makes that safe.
    """

    def __init__(
        self,
        probe_fn: Callable[[], Awaitable[str]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._probe = probe_fn or probe
        self._clock = clock or (lambda: datetime.now(UTC))

    async def step(self, db: Any) -> bool:
        """Probe, then store. True when the result was stored."""
        # The network first, the write transaction after: a probe that hangs
        # for its whole deadline must not hold the database lock.
        code = await self._probe()
        now = self._clock()
        code = _safe_code(code) if code else ""
        try:
            if db.in_transaction:
                await db.commit()  # this connection is the watcher's own
            async with write_transaction(db):
                await self._apply(db, code, now)
        except Exception:
            log.exception("Egress watch: result not stored")
            return False
        return True

    async def _apply(self, db: Any, code: str, now: datetime) -> None:
        beat = await _heartbeat(db)
        streak, first = 0, ""
        seen = _parse(beat["checked_at"]) if beat else None
        # A count left by a watcher that then went silent is not "in a row".
        if (
            beat
            and seen
            and (now - seen).total_seconds()
            <= (STALE_AFTER_INTERVALS * int(beat["interval_seconds"]))
        ):
            streak, first = int(beat["streak"]), str(beat["first_fail_at"] or "")
        episode = await active_episode(db)
        stamp = utc_stamp(now)
        if not code:
            if episode is not None:
                await self._close(db, episode, now)
            streak, first = 0, ""
        else:
            if streak == 0 or not first:
                first = stamp
            streak += 1
            if streak >= down_after() and episode is None:
                await self._open(db, code, first)
        await record_heartbeat(
            db,
            now,
            ok=not code,
            reason_code=code,
            streak=streak,
            first_fail_at=first,
        )

    async def _open(self, db: Any, code: str, since: str) -> None:
        # One episode, one egress_down — whatever the code is now.
        await repo.insert_event(
            db,
            kind=KIND_DOWN,
            actor="hub",
            payload={"since": since, "reason_code": code},
        )
        await log_activity(
            db,
            KIND_DOWN,
            f"авария — GitHub недоступен с сервера ({REASON_TEXT[code]})",
            None,
            commit=False,
        )

    async def _close(self, db: Any, episode: dict[str, Any], now: datetime) -> None:
        minutes = minutes_since(episode["since"], now)
        held = "длительность не прочитана" if minutes is None else f"{minutes} мин"
        await repo.insert_event(
            db,
            kind=KIND_RESTORED,
            actor="hub",
            payload={
                "since": episode["since"],
                "reason_code": episode["reason_code"],
                "minutes": minutes,
            },
        )
        await log_activity(
            db,
            KIND_RESTORED,
            f"связь сервера с GitHub восстановлена, была недоступна {held}",
            None,
            commit=False,
        )


async def run_loop(
    open_db: Callable[[], Any],
    watch: EgressWatch | None = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """The watcher task: sleep one interval, probe, repeat.

    The first probe is NOT at start (#1667): a probe at once put a DNS lookup
    into the first seconds of every process, including a short-lived one that
    only starts the app and stops it. ``/health`` reads "unknown" until the
    first beat, which is the true state of a watcher that has not looked yet.

    Sequential by construction — the next probe starts after the previous one
    ended (its deadline bounds it), so runs never overlap.
    """
    watch = watch or EgressWatch()
    async with open_db() as db:
        while True:
            await sleep(interval_seconds())
            try:
                await watch.step(db)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Egress watch error")
