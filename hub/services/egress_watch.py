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
import contextlib
import json
import logging
import socket
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from hub import config
from hub import repository as repo
from hub.db import fetchall, log_activity
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


def probe_url() -> str:
    url = (config.EGRESS_PROBE_URL or "").strip()
    return url if url.lower().startswith("https://") else DEFAULT_URL


def interval_seconds() -> int:
    return config.EGRESS_PROBE_SECONDS


def down_after() -> int:
    return config.EGRESS_DOWN_AFTER


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
    """
    try:
        async with asyncio.timeout(deadline):
            async with httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                timeout=deadline,
                verify=True,
            ) as client:
                await client.head(url or probe_url())
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


@dataclass
class _Snapshot:
    checked_at: datetime
    interval: int


_snapshot: _Snapshot | None = None


def reset_snapshot() -> None:
    global _snapshot
    _snapshot = None


def note_probe(checked_at: datetime, interval: int | None = None) -> None:
    global _snapshot
    _snapshot = _Snapshot(checked_at, interval or interval_seconds())


async def egress_status(db: Any, now: datetime | None = None) -> dict[str, Any]:
    """Typed state for /health, prod-state and the context. No network.

    ``unknown`` = the last probe is older than three intervals or there has
    been none in this process: the watcher is not running. An open episode's
    ``since`` and ``reason_code`` are still reported then — the last thing the
    hub knew — but the state does not claim it is current.
    """
    now = now or datetime.now(UTC)
    episode = await active_episode(db)
    snap = _snapshot
    fresh = (
        snap is not None
        and (now - snap.checked_at).total_seconds()
        <= STALE_AFTER_INTERVALS * snap.interval
    )
    state = UNKNOWN if not fresh else (DOWN if episode else UP)
    return {
        "state": state,
        "since": episode["since"] if episode else None,
        "checked_at": snap.checked_at.strftime("%Y-%m-%d %H:%M:%S") if snap else None,
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
    """One probe-and-count step; the loop around it lives in the poller."""

    def __init__(
        self,
        probe_fn: Callable[[], Awaitable[str]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._probe = probe_fn or probe
        self._clock = clock or (lambda: datetime.now(UTC))
        self._streak = 0
        self._first_fail: datetime | None = None
        self._last_code = "other"

    async def step(self, db: Any) -> None:
        # The network first, the write transaction after: a probe that hangs
        # for its whole deadline must not hold the database lock.
        code = await self._probe()
        now = self._clock()
        note_probe(now)
        if not code:
            self._streak, self._first_fail = 0, None
            await self._close_episode(db, now)
            return
        code = _safe_code(code)
        if self._streak == 0:
            self._first_fail = now
        self._streak += 1
        self._last_code = code
        if self._streak >= down_after():
            await self._open_episode(db, code)

    async def _write(self, db: Any, kind: str, summary: str, payload: dict) -> bool:
        try:
            await repo.insert_event(db, kind=kind, actor="hub", payload=payload)
            await log_activity(db, kind, summary, None)
            return True
        except Exception:
            with contextlib.suppress(Exception):
                await db.rollback()
            log.exception("Egress watch: %s not written", kind)
            return False

    async def _open_episode(self, db: Any, code: str) -> None:
        if await active_episode(db) is not None:
            return  # one episode, one egress_down — whatever the code now
        first = self._first_fail or self._clock()
        since = utc_stamp(first)
        await self._write(
            db,
            KIND_DOWN,
            f"авария — GitHub недоступен с сервера ({REASON_TEXT[code]})",
            {"since": since, "reason_code": code},
        )

    async def _close_episode(self, db: Any, now: datetime) -> None:
        episode = await active_episode(db)
        if episode is None:
            return
        minutes = minutes_since(episode["since"], now)
        held = "длительность не прочитана" if minutes is None else f"{minutes} мин"
        await self._write(
            db,
            KIND_RESTORED,
            f"связь сервера с GitHub восстановлена, была недоступна {held}",
            {
                "since": episode["since"],
                "reason_code": episode["reason_code"],
                "minutes": minutes,
            },
        )


async def run_loop(
    open_db: Callable[[], Any],
    watch: EgressWatch | None = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """The watcher task: first probe at once, then every interval.

    Sequential by construction — the next probe starts after the previous one
    ended (its deadline bounds it), so runs never overlap.
    """
    watch = watch or EgressWatch()
    async with open_db() as db:
        while True:
            try:
                await watch.step(db)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Egress watch error")
            await sleep(interval_seconds())
