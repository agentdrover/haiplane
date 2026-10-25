"""Chat-pair: a one-time code from the hub, spent for a short session (#961).

The channel exists because Cursor on iOS and cursor.com/agents cannot attach a
custom MCP: without an identity the agent in that chat gets 401, and a
long-lived token pasted into the chat stays in the transcript forever.

Two properties hold the whole design together:

* the code and the session token exist in plaintext exactly once — in the
  response that hands them out. The database stores hashes, so a dump of it
  cannot be replayed against the hub;
* the session carries a FIXED permission set (:data:`hub.config.CHAT_PAIR_PERMS`),
  never the issuer's own rights. The same principal is usually ``admin`` in
  production, and a code living in somebody else's transcript must not be worth
  an admin token.

What the session may reach is decided one layer up, by the deny-by-default
route allowlist in :mod:`hub.auth` — permissions alone would not have been
enough, because several hub branches read "not an agent" as "a human".
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import aiosqlite

from hub import config
from hub import repository as repo
from hub.auth import LoginRateLimiter
from hub.config import TokenIdentity
from hub.db import fetchall, write_transaction
from hub.services import memory_wait, order_deadline

log = logging.getLogger("hub.services.chat_pair")

# Crockford base32: no I, L, O or U — the pairs a person mistypes when copying
# a code off a screen, plus the letter that turns codes into words.
CODE_ALPHABET: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH: Final[int] = 8
CODE_PREFIX: Final[str] = "AH"

TOKEN_PREFIX: Final[str] = "ahcp_"

#: Redeem is public, so it gets a limiter of its own — writing into
#: ``login_limiter`` would share one per-IP bucket with the web login and let a
#: burst of pairing attempts lock the operator out of their own hub.
chat_pair_limiter = LoginRateLimiter(
    max_attempts=config.CHAT_PAIR_REDEEM_MAX,
    window_seconds=config.CHAT_PAIR_REDEEM_WINDOW_SECONDS,
)


def redeem_limit_reached(key: str) -> bool:
    """Whether this client already spent its attempts in the current window.

    The threshold is read from config on every call rather than frozen at
    import: tests (and a drop-in on the VM) change it at runtime, and a limiter
    that answers by a value captured at boot would quietly ignore them.
    """
    chat_pair_limiter._max_attempts = config.CHAT_PAIR_REDEEM_MAX
    chat_pair_limiter._window_seconds = config.CHAT_PAIR_REDEEM_WINDOW_SECONDS
    return chat_pair_limiter.is_blocked(key)


def record_redeem_attempt(key: str) -> None:
    chat_pair_limiter.record(key)


# ---------------------------------------------------------------------------
# Code shape
# ---------------------------------------------------------------------------


def normalize_pair_code(raw: str) -> str:
    """Bring any form a person may paste to the one form we hash.

    ``AH-7K2M9QRS``, ``ah 7k2m9qrs`` and ``7K2M9QRS`` are the same code; the
    prefix and the dash are there to be read aloud and typed, not to be stored.
    Everything downstream — the hash, the lookup, the comparison — sees only
    what this function returns, so there is exactly one normalisation.
    """
    cleaned = "".join(ch for ch in (raw or "").upper() if ch.isalnum())
    if len(cleaned) == CODE_LENGTH + len(CODE_PREFIX) and cleaned.startswith(
        CODE_PREFIX
    ):
        cleaned = cleaned[len(CODE_PREFIX) :]
    return cleaned


def format_pair_code(normalized: str) -> str:
    """The form the operator sees: ``AH-XXXXYYYY``."""
    return f"{CODE_PREFIX}-{normalized}"


def hash_pair_code(value: str) -> str:
    """One hash for both secrets of this channel — codes and session tokens."""
    return hashlib.sha256(value.encode()).hexdigest()


def generate_pair_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def generate_session_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def _as_iso(stamp: str | None) -> str:
    """SQLite's ``datetime('now')`` shape rendered as UTC ISO-8601."""
    if not stamp:
        return ""
    return stamp.replace(" ", "T") + "Z"


# ---------------------------------------------------------------------------
# Issuing a code
# ---------------------------------------------------------------------------


async def issue_code(
    db: aiosqlite.Connection,
    principal_id: int,
    *,
    kind: str = "intake",
    bound_task_id: int | None = None,
    bound_generation: int | None = None,
    channel: str = "",
    slot: str = "",
    executor_run_id: int | None = None,
    commit: bool = True,
) -> tuple[str, int]:
    """Burn unused codes in this (principal, kind, bound_task_id) bucket, mint one.

    ``commit=False`` (#1712) leaves the transaction to the caller: the host
    order issues a code, a run row and the order itself as ONE unit, and a
    commit in the middle would make a failed order leave a live code behind.

    Intake and implementer do not kill each other: burn is scoped to the
    same kind (and, for implementer, the same bound task).

    ``channel``/``slot`` (#1434) ride along with the code into its session and
    land on the task card at capture; empty keeps today's behaviour.
    """
    kind = (kind or "intake").strip().lower() or "intake"
    if kind in config.CHAT_PAIR_TASK_BOUND_KINDS:
        # #1120 review: burn is scoped to the GENERATION too, when there is
        # one. Without it, minting a code for generation N+1 killed the
        # unused code of a run already flying for generation N — a
        # resubmission mid-run disarmed a paid agent, which then reached the
        # door with a dead code and could only wait out its deadline.
        #
        # Same bucket, same generation: two live codes for one submission
        # remain impossible, which is what burning is for.
        if bound_generation is not None:
            await db.execute(
                "DELETE FROM chat_pair_codes WHERE principal_id = ? AND kind = ? "
                "AND bound_task_id = ? AND bound_generation = ? "
                "AND redeemed_at IS NULL",
                (principal_id, kind, bound_task_id, bound_generation),
            )
        else:
            await db.execute(
                "DELETE FROM chat_pair_codes WHERE principal_id = ? AND kind = ? "
                "AND bound_task_id = ? AND redeemed_at IS NULL",
                (principal_id, kind, bound_task_id),
            )
    else:
        await db.execute(
            "DELETE FROM chat_pair_codes WHERE principal_id = ? AND kind = ? "
            "AND redeemed_at IS NULL",
            (principal_id, kind),
        )
    ttl = (
        config.CHAT_PAIR_REVIEWER_CODE_SECONDS
        if kind == "reviewer"
        else config.CHAT_PAIR_CODE_SECONDS
    )
    code = generate_pair_code()
    await db.execute(
        "INSERT INTO chat_pair_codes "
        "(principal_id, kind, bound_task_id, bound_generation, code_hash, "
        " expires_at, channel, slot, executor_run_id) "
        "VALUES (?, ?, ?, ?, ?, datetime('now', ?), ?, ?, ?)",
        (
            principal_id,
            kind,
            bound_task_id if kind in config.CHAT_PAIR_TASK_BOUND_KINDS else None,
            bound_generation,
            hash_pair_code(code),
            f"+{int(ttl)} seconds",
            channel if kind == "implementer" else "",
            slot if kind == "implementer" else "",
            executor_run_id if kind == "implementer" else None,
        ),
    )
    if commit:
        await db.commit()
    return format_pair_code(code), ttl


# ---------------------------------------------------------------------------
# Spending it
# ---------------------------------------------------------------------------


async def issue_run_code(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    *,
    issued_by_principal_id: int | None,
    executor_run_id: int | None = None,
    channel: str = "cloud",
    slot: str = "",
    commit: bool = True,
) -> tuple[str, int]:
    """Код implementer, который выписывает сам хаб на прогон исполнителя (#1439).

    ``channel``/``slot`` (#1712): облако по умолчанию, слот хоста — по запуску
    с целью slot:<имя>. ``commit=False`` оставляет транзакцию вызывающему.

    Выписывается от имени агента chat-pair — того принципала, которым сессия
    implementer и так действует, — и привязан к задаче и к поколению сдачи,
    которую делает прогон. Отдельной двери сюда нет: зовёт только запуск
    исполнителя, поэтому «выписывает только код диспетчера» держится по
    построению (F4 #1368). В аудит — кто нажал запуск, задача и поколение;
    сам код не пишется никуда.

    Нет агента chat-pair — ``LookupError``: вызывающий называет причину
    отказа, а не выписывает код от чужого имени.
    """
    from hub.services import admin as admin_svc

    acting = await get_acting_agent(db)
    if acting is None:
        raise LookupError("нет активного агента chat-pair (CHAT_PAIR_AGENT)")
    code, ttl = await issue_code(
        db,
        int(acting["id"]),
        kind="implementer",
        bound_task_id=int(task_id),
        bound_generation=int(generation),
        # #1434: диспетчер хаба запускает исполнителя — канал известен по
        # построению и ложится в карточку при захвате.
        channel=channel,
        slot=slot,
        # #1683: код помнит заказ, ради которого выписан; сессия наследует.
        executor_run_id=executor_run_id,
        commit=False,
    )
    on_slot = channel == "slot"
    await admin_svc.write_audit(
        db,
        actor_id=issued_by_principal_id,
        action="implementer_code_dispatched",
        target_type="task",
        target_id=str(int(task_id)),
        summary=(
            f"implementer code issued by the hub dispatcher for task #{task_id}, "
            f"generation {generation}, valid {ttl}s"
            + (f", channel=slot slot={slot} host={slot}" if on_slot else "")
        ),
        detail=(
            json.dumps(
                {
                    "channel": channel,
                    "slot": slot,
                    "host": slot,
                    "launched_by": issued_by_principal_id,
                    "generation": int(generation),
                }
            )
            if on_slot
            else None
        ),
        commit=False,
    )
    if commit:
        await db.commit()
    return code, ttl


#: Куда возвращают работу исполнителю (#1439): находки ревью, решение
#: человека по конфликту с базой (#1362), живой pair-прогон. Не review (там
#: отчёт ревьюера, а не работа), не claimed (чужая бронь), не финальные.
IMPLEMENTER_RETURN_STATUSES = frozenset({"running", "fix_requested", "needs_decision"})


def _implementer_may_redeem(task: dict[str, Any], pinned: Any) -> bool:
    """Можно ли обменять код implementer на эту задачу сейчас (#980, #1439).

    open — как раньше; если код выписал хаб (есть поколение), оно обязано
    быть следующим: код «через одно» — не для этой сдачи. Не-open — только
    код хаба на СЛЕДУЮЩЕЕ поколение возвращённой задачи: без поколения код
    ручной выдачи нельзя потратить на задачу, ушедшую из open.
    """
    status = str(task.get("status") or "")
    next_generation = int(task.get("submission_generation") or 0) + 1
    if status == "open":
        return pinned is None or int(pinned) == next_generation
    if status not in IMPLEMENTER_RETURN_STATUSES or pinned is None:
        return False
    return int(pinned) == next_generation


@dataclass(frozen=True)
class RedeemRefusal:
    """Why a redeem was refused — for the closed admin audit, never the client.

    The client always gets the one indistinguishable 401. ``reason`` is a
    closed word (no code, no hash, no address); ``kind`` and the binding are
    what the code itself says, so they exist only when a row was found.
    """

    reason: str
    kind: str = ""
    bound_task_id: int | None = None
    bound_generation: int | None = None
    principal_id: int | None = None


#: Unknown codes are the one refusal an outsider can mint at will, so the audit
#: keeps at most one per client address per window (#1694); every other reason
#: needs a real row and is written each time.
UNKNOWN_AUDIT_WINDOW_SECONDS: Final[float] = 60.0
#: Hard cap of the thinning counter (#1694): addresses are attacker-chosen, so
#: the table cannot grow with them. Insertion order IS time order (an address is
#: re-inserted at the tail only when its entry has lapsed), so expiry pops from
#: the head and each call is O(1) amortised. A full table suppresses NEW
#: ``unknown`` rows — a skipped audit row is acceptable, unbounded memory is not.
UNKNOWN_AUDIT_CAPACITY: Final[int] = 1024
_unknown_audit_seen: OrderedDict[str, float] = OrderedDict()


def _clock() -> float:
    return time.monotonic()


def unknown_refusal_audit_due(key: str) -> bool:
    """Whether an ``unknown`` refusal from this address may enter the audit now.

    A counter of its own, in process memory: the limiter counts attempts to
    block, this one counts audit rows to spare the journal — sharing them
    would make a limiter change silently change what the audit keeps.
    """
    now = _clock()
    seen = _unknown_audit_seen
    while seen:
        oldest = next(iter(seen))
        if now - seen[oldest] < UNKNOWN_AUDIT_WINDOW_SECONDS:
            break
        del seen[oldest]
    if key in seen:
        return False
    if len(seen) >= UNKNOWN_AUDIT_CAPACITY:
        return False
    seen[key] = now
    return True


async def audit_redeem_refusal(
    db: aiosqlite.Connection,
    caller: str,
    refusal: RedeemRefusal | None,
    *,
    thinned: bool = False,
) -> None:
    """Why a redeem was refused — closed admin audit only (#1694).

    Not the event feed (agents read it) and not the response (it would be an
    oracle for guessing codes). No code, no hash, no address in the row; the
    address only keys the in-memory thinning of ``unknown``. A failed write
    must not turn the 401 into a 500.
    """
    if refusal is None:
        return
    if (
        refusal.reason == "unknown"
        and not thinned
        and not unknown_refusal_audit_due(caller)
    ):
        return
    from hub.services import admin as admin_svc

    try:
        await admin_svc.write_audit(
            db,
            actor_id=refusal.principal_id,
            action="chat_pair_redeem_refused",
            target_type="chat_pair",
            target_id=(
                str(refusal.bound_task_id) if refusal.bound_task_id is not None else ""
            ),
            summary=f"pairing code redeem refused: {refusal.reason}",
            detail=json.dumps(
                {
                    "reason": refusal.reason,
                    "kind": refusal.kind,
                    "bound_task_id": refusal.bound_task_id,
                    "bound_generation": refusal.bound_generation,
                }
            ),
        )
    except Exception:  # noqa: BLE001 - диагностика не имеет права ломать ответ
        log.warning("chat-pair: could not audit a refused redeem (%s)", refusal.reason)


async def audit_redeem_refusal_detached(
    app: Any, caller: str, refusal: RedeemRefusal | None
) -> None:
    """Background half of the audit: runs after the 401 has been sent (#1694).

    Writing inline would make the response time name the reason (a repeated
    ``unknown`` skips the write, the rest wait for INSERT and commit). The
    request's own connection is closed by the time this runs, so it takes one
    of its own — the same way a detached provisioning does.
    """
    if refusal is None:
        return
    if refusal.reason == "unknown" and not unknown_refusal_audit_due(caller):
        return
    conn = None
    dsn = getattr(app.state, "dsn", None)
    try:
        if dsn:
            from hub import db as db_module

            conn = await db_module.connect(dsn)
        await audit_redeem_refusal(conn or app.state.db, caller, refusal, thinned=True)
    except Exception:  # noqa: BLE001 - диагностика не имеет права ломать ответ
        log.warning("chat-pair: could not audit a refused redeem (%s)", refusal.reason)
    finally:
        if conn is not None:
            await conn.close()


def _row_refusal(row: dict[str, Any], reason: str) -> RedeemRefusal:
    return RedeemRefusal(
        reason,
        kind=(row.get("kind") or "intake").strip().lower() or "intake",
        bound_task_id=row.get("bound_task_id"),
        bound_generation=row.get("bound_generation"),
        principal_id=row.get("principal_id"),
    )


async def _task_row(db: aiosqlite.Connection, task_id: int) -> dict[str, Any] | None:
    rows = await fetchall(
        db,
        "SELECT status, submission_generation FROM tasks WHERE id = ?",
        (int(task_id),),
    )
    return dict(rows[0]) if rows else None


async def _implementer_refusal(
    db: aiosqlite.Connection, row: dict[str, Any]
) -> tuple[dict[str, Any] | None, RedeemRefusal | None]:
    """Implementer branch: the acting agent, or the reason it is refused."""
    bound_task_id = row.get("bound_task_id")
    if bound_task_id is None:
        return None, _row_refusal(row, "implementer_unbound")
    task = await _task_row(db, int(bound_task_id))
    if task is None or not _implementer_may_redeem(task, row.get("bound_generation")):
        return None, _row_refusal(row, "implementer_refused")
    if task.get("status") == "open":
        # #1433: код на open-задачу — вход в её захват, третий рядом с
        # claim и pair_start; сверка та же и до траты кода. Отказ — не
        # None: он возможен только с действительным кодом и называет
        # соседа, а не угадывание кода.
        from hub.services.lifecycle import capture_areas

        async with capture_areas(db, int(bound_task_id), "open"):
            pass
    acting = await get_acting_agent(db)
    if acting is None:
        return None, _row_refusal(row, "no_acting_agent")
    return acting, None


async def _reviewer_refusal(
    db: aiosqlite.Connection, row: dict[str, Any]
) -> RedeemRefusal | None:
    """Reviewer branch (#1084): the reason the code cannot be spent, or None.

    Minted by the dispatch, not by a person, and spent by the cloud run it was
    minted for. The task must be IN REVIEW: a code outliving its submission
    would let a run file a report against work that has moved on. And the
    SUBMISSION, not just the status: a resubmission during a live run keeps the
    task in review, so status alone would let a code minted for generation N be
    spent against N+1 — a report about the old diff, recorded as one about the
    new.
    """
    bound_task_id = row.get("bound_task_id")
    if bound_task_id is None:
        return _row_refusal(row, "reviewer_unbound")
    task = await _task_row(db, int(bound_task_id))
    if task is None or task.get("status") != "review":
        return _row_refusal(row, "task_not_in_review")
    pinned = row.get("bound_generation")
    if pinned is not None and int(pinned) != int(
        task.get("submission_generation") or 0
    ):
        return _row_refusal(row, "generation_mismatch")
    return None


#: #1712: a code of a REGISTERED host is spent only against an open order of an
#: enabled host. One statement, not a read followed by a write: closing the
#: order or disabling the host takes the write lock too, so the two are
#: serialised and the code either goes before them or not at all. A slot name
#: nobody registered keeps the manual door's old behaviour.
_SLOT_ORDER_GUARD: Final[str] = (
    " AND (channel <> 'slot' "  # nosec B608 - constants only
    "OR NOT EXISTS (SELECT 1 FROM executor_hosts h WHERE h.name = chat_pair_codes.slot) "
    "OR EXISTS (SELECT 1 FROM executor_hosts h "
    "JOIN principals p ON p.id = h.principal_id "
    "JOIN executor_orders o ON o.host = h.name "
    "WHERE h.name = chat_pair_codes.slot AND h.enabled = 1 AND p.status = 'active' "
    "AND o.pair_code_id = chat_pair_codes.id "
    f"AND {memory_wait.REDEEMABLE_SQL}))"
)


_SPEND_CODE_SQL: Final[str] = (
    "UPDATE chat_pair_codes SET redeemed_at = datetime('now') "  # nosec B608 - constants
    "WHERE id = ? AND redeemed_at IS NULL" + _SLOT_ORDER_GUARD
)


async def _slot_order_refusal(
    db: aiosqlite.Connection, row: dict[str, Any]
) -> RedeemRefusal | None:
    """Why a code of a registered host cannot be spent (#1712); None — it can.

    Names the reason for the closed audit only. The atomic check is
    ``_SLOT_ORDER_GUARD`` in the spend itself; this read exists so the audit
    can say which of the two it was.
    """
    hosts = await fetchall(
        db,
        "SELECT h.enabled, p.status FROM executor_hosts h "
        "JOIN principals p ON p.id = h.principal_id WHERE h.name = ?",
        (row.get("slot") or "",),
    )
    if not hosts:
        return None
    host = dict(hosts[0])
    if not host["enabled"] or host["status"] != "active":
        return _row_refusal(row, "slot_host_disabled")
    orders = await fetchall(
        db,
        "SELECT 1 FROM executor_orders WHERE pair_code_id = ? "
        f"AND state IN {memory_wait.UNREDEEMED_IN_SQL}",  # nosec B608 - constant
        (row["id"],),
    )
    if not orders:
        return _row_refusal(row, "slot_order_closed")
    return None


async def _spend_code(
    db: aiosqlite.Connection,
    row: dict[str, Any],
    *,
    kind: str,
    acting_id: int | None,
    acting_username: str,
) -> dict[str, Any] | None:
    """Mark the code spent and mint the session; None if someone spent it first."""
    bound_task_id = row.get("bound_task_id")
    cursor = await db.execute(_SPEND_CODE_SQL, (row["id"],))
    if not cursor.rowcount:
        await db.rollback()
        return None
    if row.get("channel") == "slot" or (
        kind == "implementer" and row.get("executor_run_id") is None
    ):
        from hub.services import run_claim

        # Под write-локом этого выкупа: код слота не отдаётся, если задача уже занята
        # (#1783) — иначе второй исполнитель встал бы на ту же ветку.
        if await run_claim.redeem_conflict(db, row):
            await db.rollback()
            return None

    token = generate_session_token()
    ttl = await order_deadline.session_ttl(db, row)  # #1960
    inserted = await db.execute(
        "INSERT INTO chat_pair_sessions "
        "(principal_id, acting_principal_id, kind, bound_task_id, "
        " bound_generation, token_hash, expires_at, channel, slot, "
        " executor_run_id, claimed_session, dispatch_id, code_id) "
        "VALUES (?, ?, ?, ?, ?, ?, datetime('now', ?), ?, ?, ?, NULL, "
        "(SELECT dispatch_id FROM chat_pair_codes WHERE id = ?), ?)",
        (
            row["principal_id"],
            acting_id,
            kind,
            bound_task_id if kind in config.CHAT_PAIR_TASK_BOUND_KINDS else None,
            row.get("bound_generation"),
            hash_pair_code(token),
            f"+{int(ttl)} seconds",
            row.get("channel") or "",
            row.get("slot") or "",
            row.get("executor_run_id"),
            row["id"],  # #1946: привязку кода читает ЭТА транзакция, не прежний SELECT
            row["id"],
        ),
    )
    if row.get("channel") == "slot":
        # #1712: the order moves with the spend, in the same transaction.
        # #1825: and records the FIRST redeem — the retry window is counted
        # from it and never extended, and the session it minted.
        await db.execute(
            "UPDATE executor_orders SET state = 'redeemed', "
            "redeemed_at = datetime('now'), first_redeemed_at = datetime('now'), "
            "retry_until = datetime('now', ?), session_id = ? "
            f"WHERE pair_code_id = ? AND state IN {memory_wait.UNREDEEMED_IN_SQL}",  # nosec B608
            (
                f"+{min(int(config.HOST_REDEEM_RETRY_SECONDS), int(ttl))} seconds",
                inserted.lastrowid,
                row["id"],
            ),
        )
    await db.commit()

    stored = await fetchall(
        db,
        "SELECT expires_at FROM chat_pair_sessions WHERE token_hash = ?",
        (hash_pair_code(token),),
    )
    return {
        "token": token,
        "principal_id": row["principal_id"],
        "username": acting_username,
        "kind": kind,
        "bound_task_id": int(bound_task_id) if bound_task_id is not None else None,
        "expires_at": _as_iso(dict(stored[0])["expires_at"] if stored else None),
    }


async def redeem_code(db: aiosqlite.Connection, raw_code: str) -> dict[str, Any] | None:
    """Exchange a code for a session. ``None`` for every way that can fail.

    The caller that needs to know WHY uses :func:`redeem_code_explained`.
    """
    session, _refusal = await redeem_code_explained(db, raw_code)
    return session


async def redeem_code_explained(
    db: aiosqlite.Connection, raw_code: str, *, via_host: bool = False
) -> tuple[dict[str, Any] | None, RedeemRefusal | None]:
    """Exchange a code for a session, naming the reason when it fails (#1694).

    The reason goes to the closed admin audit and nowhere else: unknown, spent
    and expired stay ONE answer to the client on purpose, because three
    distinguishable refusals would tell a caller enumerating codes which
    guesses were close. Priority when several apply: spent, expired, then the
    rest.

    The one exception is the capture check of #1433: a VALID implementer code
    on an open task whose areas overlap started work under
    ``claim_area_check=require`` raises the structured 409 naming the
    neighbour, and the code stays unspent.
    """
    normalized = normalize_pair_code(raw_code)
    if len(normalized) != CODE_LENGTH:
        return None, RedeemRefusal("unknown")

    rows = await fetchall(
        db,
        "SELECT c.id, c.principal_id, c.kind, c.bound_task_id, "
        "c.bound_generation, c.channel, c.slot, c.executor_run_id, "
        "c.redeemed_at IS NOT NULL AS spent, "
        "c.expires_at <= datetime('now') AS expired, "
        "p.username, p.status "
        "FROM chat_pair_codes c JOIN principals p ON p.id = c.principal_id "
        "WHERE c.code_hash = ?",
        (hash_pair_code(normalized),),
    )
    if not rows:
        return None, RedeemRefusal("unknown")
    row = dict(rows[0])
    if not via_host and config.HOST_ORDER_PUBLIC_REDEEM_CLOSED:
        from hub.services.host_redeem import is_host_order_code

        if await is_host_order_code(db, row):  # #1825: host route only
            return None, _row_refusal(row, "host_code_public_closed")
    if row["spent"]:
        return None, _row_refusal(row, "spent")
    if row["expired"]:
        return None, _row_refusal(row, await memory_wait.why(db, row, "expired"))
    if row["status"] != "active":
        return None, _row_refusal(row, "principal_inactive")

    kind = (row.get("kind") or "intake").strip().lower() or "intake"
    acting_id: int | None = None
    acting_username = row["username"]
    if kind == "implementer":
        if row.get("channel") == "slot":
            refusal = await _slot_order_refusal(db, row)
            if refusal is not None:
                return None, refusal
        acting, refusal = await _implementer_refusal(db, row)
        if refusal is not None or acting is None:
            return None, refusal
        acting_id = int(acting["id"])
        acting_username = str(acting["username"])
    elif kind == "reviewer":
        refusal = await _reviewer_refusal(db, row)
        if refusal is not None:
            return None, refusal

    session = await _spend_code(
        db, row, kind=kind, acting_id=acting_id, acting_username=acting_username
    )
    if session is None:
        return None, _row_refusal(row, await memory_wait.why(db, row, "spent"))
    return session, None


async def redeem_for_request(request: Any, raw_code: str) -> Any:
    """The body of ``POST /api/auth/chat-pair/redeem`` (moved out of app.py).

    The only unauthenticated write in the hub, and every guard here exists
    because of that: a limiter of its own (not the login one — a shared bucket
    would lock the operator out of /login), one indistinguishable refusal for
    unknown/spent/expired, and a code that is gone the moment it is spent.

    A refusal is the same 401 whatever the reason; the reason goes to the
    closed audit AFTER the answer is sent (#1694), because inline the response
    time would name it.
    """
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse
    from starlette.background import BackgroundTask

    from hub.actionable_errors import (
        chat_pair_invalid_detail,
        chat_pair_rate_limited_detail,
    )
    from hub.auth import client_ip
    from hub.hub_instance import hub_base_url
    from hub.models import ChatPairRedeemed
    from hub.services import admin as admin_svc

    caller = client_ip(request)
    if redeem_limit_reached(caller):
        raise HTTPException(429, detail=chat_pair_rate_limited_detail())

    db = getattr(request.state, "db", None) or request.app.state.db
    session, refusal = await redeem_code_explained(db, raw_code)
    if session is None:
        record_redeem_attempt(caller)
        return JSONResponse(
            status_code=401,
            content={"detail": chat_pair_invalid_detail()},
            background=BackgroundTask(
                audit_redeem_refusal_detached, request.app, caller, refusal
            ),
        )

    await admin_svc.write_audit(
        db,
        actor_id=session["principal_id"],
        action="chat_pair_redeem",
        target_type="chat_pair",
        target_id=str(session["principal_id"]),
        summary="pairing code redeemed for a chat session",
    )
    implementer = session.get("kind") == "implementer"
    return ChatPairRedeemed(
        token=session["token"],
        expires_at=session["expires_at"],
        username=session["username"],
        role="agent" if implementer else "human",
        base_url=hub_base_url(),
        permissions=sorted(
            config.CHAT_PAIR_IMPLEMENTER_PERMS
            if implementer
            else config.CHAT_PAIR_PERMS
        ),
        kind=session.get("kind") or "intake",
        bound_task_id=session.get("bound_task_id"),
    )


async def ordered_skill_of_session(
    db: aiosqlite.Connection, token: str, task_id: int
) -> dict[str, Any] | None:
    """Заказ, которым стартовала ВЫЗЫВАЮЩАЯ сессия implementer (#1683).

    Цепочка: токен → chat_pair_sessions.executor_run_id (унаследован от
    chat_pair_codes.executor_run_id при обмене) → executor_runs. Возвращает
    ``{"executor_run_id", "skill_version"}`` или ``None``. ``None`` — на любую
    неопределённость: нет токена, не implementer, ручной код, сессия отозвана
    или истекла, строка заказа исчезла, заказ другой задачи или другого
    поколения, версия не записана. Читатель тогда отдаёт навык целиком:
    «отдали дважды» дешевле, чем «скрыли навык, которого не получали».
    """
    if not token or not isinstance(task_id, int):
        return None
    rows = await fetchall(
        db,
        "SELECT executor_run_id, bound_task_id, bound_generation "
        "FROM chat_pair_sessions WHERE token_hash = ? AND kind = 'implementer' "
        "AND revoked_at IS NULL AND expires_at > datetime('now')",
        (hash_pair_code(token),),
    )
    if len(rows) != 1:
        return None
    session = dict(rows[0])
    run_id, generation = session.get("executor_run_id"), session.get("bound_generation")
    if run_id is None or generation is None or session.get("bound_task_id") != task_id:
        return None
    runs = await fetchall(
        db,
        "SELECT id, task_id, submission_generation, ordered_skill_version "
        "FROM executor_runs WHERE id = ?",
        (int(run_id),),
    )
    if len(runs) != 1:
        return None
    run = dict(runs[0])
    if (
        run["task_id"] != task_id
        or run["submission_generation"] != generation
        or run["ordered_skill_version"] is None
    ):
        return None
    return {
        "executor_run_id": int(run["id"]),
        "skill_version": int(run["ordered_skill_version"]),
    }


async def bind_claim(
    db: aiosqlite.Connection, token: str, task_id: int, nonce: str
) -> None:
    """Сессия implementer запоминает метку захвата, который она сделала (#1783).

    Метка (``tasks.claim_nonce``) случайна и новая у каждого claim/pair-start:
    реапер отдаёт задачу мёртвой сессии, только если на задаче стоит ЕЁ метка.
    Независимый захват тем же принципалом, под тем же session_id и в ту же
    секунду, метки не наследует.
    """
    if token and nonce:
        await db.execute(
            "UPDATE chat_pair_sessions SET claimed_session = ? "
            "WHERE token_hash = ? AND kind = 'implementer' AND bound_task_id = ?",
            (nonce, hash_pair_code(token), int(task_id)),
        )


async def live_session(db: aiosqlite.Connection, token: str, task_id: int) -> bool:
    """Токен — живая implementer-сессия этой задачи (захват делает сессия, #1783)."""
    if not token:
        return False
    return bool(
        await fetchall(
            db,
            "SELECT 1 FROM chat_pair_sessions WHERE token_hash = ? "
            "AND kind = 'implementer' AND bound_task_id = ? AND revoked_at IS NULL "
            "AND expires_at > datetime('now')",
            (hash_pair_code(token), int(task_id)),
        )
    )


async def adopt_claim(
    db: aiosqlite.Connection,
    token: str,
    task_id: int,
    holder: tuple[str | None, str | None],
) -> None:
    """Сессия implementer, продолжающая захват (повторный claim, pair-start из claimed,
    в том числе после claim токеном среды), делает его своим: метка появляется
    у захвата и записывается в сессию.

    Только если захват под write-локом всё ещё тот, что видел вызывающий
    (``holder`` = claimed_by и claim_session_id): отпущенный и взятый заново
    чужой захват метку не получает (#1783).
    """
    if not await live_session(db, token, task_id):
        return
    cursor = await db.execute(
        "UPDATE tasks SET claim_nonce = COALESCE(claim_nonce, ?) WHERE id = ? "
        "AND status IN ('claimed', 'running') AND claimed_by IS ? "
        "AND claim_session_id IS ?",
        (secrets.token_hex(8), int(task_id), holder[0], holder[1]),
    )
    if not cursor.rowcount:
        return
    rows = await fetchall(
        db, "SELECT claim_nonce FROM tasks WHERE id = ?", (int(task_id),)
    )
    await bind_claim(db, token, task_id, str(rows[0]["claim_nonce"]))


async def claim_is_session_bound(db: aiosqlite.Connection, nonce: str | None) -> bool:
    """Захват сделан сессией implementer (в ней записана его метка, #1783).

    Отчёт ``done`` свободным именем агента такой захват не снимает: имя не
    доказывает, что отчёт шлёт та самая сессия.
    """
    if not nonce:
        return False
    return bool(
        await fetchall(
            db,
            "SELECT 1 FROM chat_pair_sessions WHERE claimed_session = ?",
            (nonce,),
        )
    )


async def ended_run_session(db: aiosqlite.Connection, token: str, task_id: int) -> bool:
    """Токен сессии прогона, который уже закончен или отозван (#1783).

    Задержанный запрос зомби-агента не должен занять задачу как ручную: такой
    захват никто не докажет и не снимет. Сессия ручная или прогон идёт — False.
    """
    if not token or not isinstance(task_id, int):
        return False
    rows = await fetchall(
        db,
        "SELECT 1 FROM chat_pair_sessions s JOIN executor_runs r "
        "ON r.id = s.executor_run_id WHERE s.token_hash = ? "
        "AND s.kind = 'implementer' AND s.bound_task_id = ? "
        "AND (r.outcome <> 'running' OR r.cost_wait_since IS NOT NULL "
        "OR s.revoked_at IS NOT NULL OR s.expires_at <= datetime('now'))",
        (hash_pair_code(token), int(task_id)),
    )
    return bool(rows)


async def run_of_session(
    db: aiosqlite.Connection, token: str, task_id: int
) -> int | None:
    """Прогон хаба, чей заказ породил ВЫЗЫВАЮЩУЮ сессию implementer (#1783).

    Доказательство «задачу берёт прогон хаба»: токен → живая сессия этой задачи
    с ``executor_run_id`` → строка прогона ТОЙ ЖЕ задачи, ещё идущая. Любая
    неопределённость (нет токена, ручной код, отозванная или чужая сессия,
    закончившийся прогон) — ``None``: захват тогда ручной и освобождается по
    TTL, как до задачи.
    """
    if not token or not isinstance(task_id, int):
        return None
    rows = await fetchall(
        db,
        "SELECT s.executor_run_id AS run_id FROM chat_pair_sessions s "
        "JOIN executor_runs r ON r.id = s.executor_run_id "
        "WHERE s.token_hash = ? AND s.kind = 'implementer' "
        "AND s.bound_task_id = ? AND s.revoked_at IS NULL "
        "AND s.expires_at > datetime('now') "
        "AND r.task_id = s.bound_task_id AND r.outcome = 'running' "
        "AND r.cost_wait_since IS NULL",
        (hash_pair_code(token), int(task_id)),
    )
    if len(rows) != 1:
        return None
    return int(rows[0]["run_id"])


async def get_acting_agent(db: aiosqlite.Connection) -> dict[str, Any] | None:
    """The implementer acting principal, or None if missing/inactive."""
    username = (config.CHAT_PAIR_AGENT or "cloud").strip() or "cloud"
    rows = await fetchall(
        db,
        "SELECT id, username, status FROM principals WHERE username = ?",
        (username,),
    )
    if not rows:
        return None
    row = dict(rows[0])
    if row["status"] != "active":
        return None
    return row


async def resolve_session(
    db: aiosqlite.Connection, token: str, *, dsn: str | None = None
) -> TokenIdentity | None:
    """Identity behind a chat-pair bearer token, or ``None``.

    Intake keeps ``role='human'`` as presentational naming of the issuer.
    Implementer walks as ``role='agent'`` under the acting principal's name,
    while ``principal_id`` stays the issuer so revoke and audit still land
    on the human who issued the code (#980).
    """
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    rows = await fetchall(
        db,
        "SELECT s.principal_id, s.acting_principal_id, s.kind, s.bound_task_id, "
        "s.bound_generation, s.first_used_at, s.dispatch_id, s.code_id, "
        "p.username AS issuer_username, p.status AS issuer_status "
        "FROM chat_pair_sessions s JOIN principals p ON p.id = s.principal_id "
        "WHERE s.token_hash = ? AND s.revoked_at IS NULL "
        "AND s.expires_at > datetime('now')",
        (hash_pair_code(token),),
    )
    if not rows:
        return None
    row = dict(rows[0])
    if row["issuer_status"] != "active":
        return None
    if row["first_used_at"] is None:
        from hub.services.host_redeem import admit_first_use

        if not await admit_first_use(db, hash_pair_code(token), dsn):
            return None
    kind = (row.get("kind") or "intake").strip().lower() or "intake"
    bound = row.get("bound_task_id")
    bound_id = int(bound) if bound is not None else None
    if kind == "implementer":
        acting_id = row.get("acting_principal_id")
        if acting_id is None:
            return None
        acting_rows = await fetchall(
            db,
            "SELECT username, status FROM principals WHERE id = ?",
            (int(acting_id),),
        )
        if not acting_rows:
            return None
        acting = dict(acting_rows[0])
        if acting["status"] != "active":
            return None
        return TokenIdentity(
            username=acting["username"],
            role="agent",
            principal_id=row["principal_id"],
            permissions=config.CHAT_PAIR_IMPLEMENTER_PERMS,
            auth_source="chat_pair",
            chat_pair_kind="implementer",
            chat_pair_task_id=bound_id,
        )
    if kind == "reviewer":
        # Walks as an agent under its issuer: no separate acting principal,
        # because the issuer IS the reviewer principal (see redeem_code).
        return TokenIdentity(
            username=row["issuer_username"],
            role="agent",
            principal_id=row["principal_id"],
            permissions=config.CHAT_PAIR_REVIEWER_PERMS,
            auth_source="chat_pair",
            chat_pair_kind="reviewer",
            chat_pair_task_id=bound_id,
            chat_pair_generation=(
                int(row["bound_generation"])
                if row.get("bound_generation") is not None
                else None
            ),
            chat_pair_dispatch_id=row.get("dispatch_id"),
            chat_pair_code_id=row.get("code_id"),
        )
    if kind in config.STEWARD_PAIR_KINDS:
        # #1601: тот же допуск стюарда, но вид сессии сохраняется — по нему
        # суждение судьи и суждение советника разведены.
        # #1120: the steward run authenticates the same way the reviewer does
        # since #1084 — Cursor drops mcpServers, so the header never arrives
        # and a one-time code in the prompt is the only channel that does.
        #
        # Role and permissions come from the STEWARD side, not from chat-pair:
        # the session is the steward principal reaching the hub by another
        # road, not a new kind of actor with its own rights (#1021).
        return TokenIdentity(
            username=row["issuer_username"],
            role="steward",
            principal_id=row["principal_id"],
            permissions=config.STEWARD_PERMS,
            auth_source="chat_pair",
            chat_pair_kind=kind,
            chat_pair_task_id=bound_id,
            chat_pair_generation=(
                int(row["bound_generation"])
                if row.get("bound_generation") is not None
                else None
            ),
        )
    return TokenIdentity(
        username=row["issuer_username"],
        role="human",
        principal_id=row["principal_id"],
        permissions=config.CHAT_PAIR_PERMS,
        auth_source="chat_pair",
        chat_pair_kind="intake",
        chat_pair_task_id=None,
    )


async def revoke_sessions(
    db: aiosqlite.Connection,
    principal_id: int,
    *,
    kind: str = "intake",
    bound_task_id: int | None = None,
) -> int:
    """Close live chat-pair sessions of this principal, scoped by kind.

    Intake revoke must not kill a running implementer, and the other way
    around (#980). ``bound_task_id`` further narrows implementer revoke when
    the caller names a task.
    """
    kind = (kind or "intake").strip().lower() or "intake"
    if kind == "implementer" and bound_task_id is not None:
        cursor = await db.execute(
            "UPDATE chat_pair_sessions SET revoked_at = datetime('now') "
            "WHERE principal_id = ? AND kind = ? AND bound_task_id = ? "
            "AND revoked_at IS NULL",
            (principal_id, kind, bound_task_id),
        )
    else:
        cursor = await db.execute(
            "UPDATE chat_pair_sessions SET revoked_at = datetime('now') "
            "WHERE principal_id = ? AND kind = ? AND revoked_at IS NULL",
            (principal_id, kind),
        )
    await db.commit()
    return int(cursor.rowcount or 0)


async def return_task_to_open(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    from_status: str,
    note: str,
    event_kind: str,
    holders: frozenset[int] | None = None,
    session_runs: frozenset[int | None] | None = None,
    session_claim: bool = False,
) -> bool:
    """Put a pair task whose implementer is gone back to open; no commit.

    One body for the session reaper and for the host order that closes with
    its task still held (#1712): two copies of "what returning to open
    involves" would drift apart. The order of the held session is closed in
    the same transaction (the reaper's half of the booking).

    The caller decided to release from a read taken OUTSIDE the write lock, and
    between that read and this write a person can release the claim and another
    agent take the task. So the write lock is taken first (a statement that
    matches no row still takes it) and the facts are read again under it: the
    status, the live-session check, and — when ``holders`` is given — that the
    task's current holder is one of those principals (an unknown holder counts
    as the session's, as before). A task somebody else holds now is left alone.
    """
    await db.execute("UPDATE tasks SET updated_at = updated_at WHERE id = -1")
    rows = await fetchall(
        db,
        "SELECT status, implementer_principal_id, job_id, review_job_id, "
        "claimed_by, claim_nonce, executor_run_id "
        "FROM tasks WHERE id = ?",
        (int(task_id),),
    )
    if not rows or rows[0]["status"] != from_status:
        return False
    # #1783 (круг 2): сессия отдаёт задачу, только если захват СТОИТ ЗА её
    # прогоном: доказательство задачи совпадает с прогоном сессии (ручная
    # сессия — без прогона и без доказательства). Захват человека, ручной claim
    # после release и уже снятый run_claim чужой сессией не освобождаются.
    if session_runs is not None and rows[0]["executor_run_id"] not in session_runs:
        return False
    # Сессия отдаёт только захват, сделанный через implementer-сессию (метка
    # claim_nonce есть только у таких): захват человека и обычного токена,
    # включая claim под тем же агентом и session_id, метки не имеет и не снимается
    # (#1783, круг 4).
    if session_claim and rows[0]["claim_nonce"] is None:
        return False
    # #1783: задание и идущий прогон хаба — признаки работы, появившиеся после
    # выборки reaper'а; под локом они видны, и освобождение не состоится.
    if (rows[0]["job_id"] or "").strip() or (rows[0]["review_job_id"] or "").strip():
        return False
    # Прогон слота живёт сессией хоста (#1712): мёртвая сессия — мёртвый прогон,
    # и его закрывает заказ в этой же операции. Облачный идёт у провайдера и
    # от сессии хаба не зависит.
    if await fetchall(
        db,
        "SELECT 1 FROM executor_runs WHERE task_id = ? AND outcome = 'running' "
        "AND channel <> 'slot' AND cost_wait_since IS NULL",
        (int(task_id),),
    ):
        return False
    holder = rows[0]["implementer_principal_id"]
    if holders is not None and holder is not None and int(holder) not in holders:
        return False
    if holders is not None and holder is None and (rows[0]["claimed_by"] or "").strip():
        # #1783 (круг 2): держатель не назван принципалом — это захват человека
        # или env-токена, а не сессии. Сессия отдаёт только захват под именем
        # своего принципала; «неизвестный держатель — её» остаётся лишь для
        # задачи, взятой без claim (pair-start из open: claimed_by пусто).
        names = await fetchall(
            db,
            "SELECT username FROM principals WHERE id IN (%s)"  # nosec B608 - ids
            % ", ".join("?" for _ in holders or {-1}),
            tuple(holders) or (-1,),
        )
        if (rows[0]["claimed_by"] or "").strip() not in {r["username"] for r in names}:
            return False
    live = await fetchall(
        db,
        "SELECT 1 FROM chat_pair_sessions WHERE kind = 'implementer' "
        "AND bound_task_id = ? AND revoked_at IS NULL "
        "AND expires_at > datetime('now')",
        (int(task_id),),
    )
    if live:
        return False
    if not await repo.transition_status_if(
        db, task_id, expected_from=from_status, new_status="open"
    ):
        return False
    await repo.update_task(
        db,
        task_id,
        claimed_by=None,
        claim_session_id=None,
        claimed_at=None,
        implementer_principal_id=None,
    )
    # #1817: освобождаемый прогон — прогон заказа хоста: итоговую запись даёт
    # закрытие заказа (оно идёт следом), своей записи возврат не пишет. Любой
    # другой прогон (ручная pair-попытка после закрытого слот-заказа) пишет обычную.
    ordered = rows[0]["executor_run_id"] is not None and await fetchall(
        db,
        "SELECT 1 FROM executor_orders WHERE executor_run_id = ?",
        (int(rows[0]["executor_run_id"]),),
    )
    if not ordered:
        await repo.add_task_update(db, task_id, "hub", "status", note)
    await repo.insert_event(
        db,
        kind=event_kind,
        task_id=task_id,
        actor="hub",
        payload={"reason": event_kind, "from": from_status},
    )
    from hub.services import executor_orders

    await executor_orders.close_for_released_task(db, task_id)
    return True


async def _release_atomically(
    db: aiosqlite.Connection, task_id: int, **kwargs: Any
) -> bool:
    """``return_task_to_open`` целиком или никак — и в чужой транзакции тоже.

    ``write_transaction`` во вложенном вызове ничего не откатывает (владелец
    снаружи); SAVEPOINT откатывает именно эту задачу, а владелец потом коммитит
    остальное (#1712).
    """
    async with write_transaction(db):
        await db.execute("SAVEPOINT reaper_release")
        try:
            freed = await return_task_to_open(db, task_id, **kwargs)
        except BaseException:
            await db.execute("ROLLBACK TO reaper_release")
            await db.execute("RELEASE reaper_release")
            raise
        await db.execute("RELEASE reaper_release")
        return freed


async def release_expired_implementer_tasks(
    db: aiosqlite.Connection, failed: set[int] | None = None
) -> list[int]:
    """Return bound pair tasks to open when their implementer session is dead.

    Intake TTL is unchanged (#983 out of scope). A live token must not trip
    this. Review/completed stay: the work already left the pairing window.
    Headless ``job_id`` rows are not pairing sessions and are left alone.
    A dead sibling session must not yank a task that still has another
    unexpired, unrevoked implementer session on the same bound task.
    """
    rows = await fetchall(
        db,
        "SELECT s.bound_task_id AS task_id, t.status AS status, t.job_id AS job_id, "
        "s.principal_id AS principal_id, s.executor_run_id AS run_id "
        "FROM chat_pair_sessions s "
        "JOIN tasks t ON t.id = s.bound_task_id "
        "WHERE s.kind = 'implementer' AND s.bound_task_id IS NOT NULL "
        "AND (s.expires_at < datetime('now') OR s.revoked_at IS NOT NULL) "
        "AND t.status IN ('running', 'claimed') "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM chat_pair_sessions live "
        "  WHERE live.kind = 'implementer' "
        "  AND live.bound_task_id = s.bound_task_id "
        "  AND live.revoked_at IS NULL "
        "  AND live.expires_at > datetime('now')"
        ")",
    )
    released: list[int] = []
    pending: dict[int, tuple[str, set[int], set[int | None]]] = {}
    for raw in rows:
        row = dict(raw)
        if row.get("job_id"):
            continue
        status, principals, runs = pending.setdefault(
            int(row["task_id"]), (str(row["status"]), set(), set())
        )
        principals.add(int(row["principal_id"]))
        runs.add(row["run_id"])
    for task_id, (from_status, principals, runs) in pending.items():
        # One transaction per task, rolled back as a whole (#1712): the release
        # also closes the host order of the run, and a failure between the two
        # must not leave half of it behind — nor an open write transaction that
        # the next pass would commit as if it were its own.
        try:
            freed = await _release_atomically(
                db,
                task_id,
                from_status=from_status,
                note="Implementer pairing session expired; task returned to open.",
                event_kind="chat_pair_implementer_expired",
                # #1712: a task another agent holds now is not this session's.
                holders=frozenset(principals),
                session_runs=frozenset(runs),
                session_claim=True,
            )
        except Exception:  # noqa: BLE001 - one task must not stop the others
            log.exception("Chat-pair reaper: releasing task #%d failed", task_id)
            if failed is not None:
                failed.add(task_id)  # its dead sessions are the retry evidence
            continue
        if freed:
            released.append(task_id)
    released += await _release_orphans(db, failed, exclude=set(released))
    if released:
        log.info(
            "Chat-pair reaper: released %d implementer-bound task(s) to open",
            len(released),
        )
    await release_stuck_run_claims(db, failed)
    return released


async def _release_orphans(
    db: aiosqlite.Connection, failed: set[int] | None, *, exclude: set[int]
) -> list[int]:
    """Захват implementer-сессии, у которого не осталось ни одной живой сессии (#1783).

    Первый проход идёт от мёртвых сессий, а purge их удаляет: задача, чьи сессии
    исчезли раньше, чем проход её освободил (вторая сессия того же человека, чужое
    ожидание), становилась тупиком. Кандидат — сама задача: метка ``claim_nonce``
    (захват сделан через implementer-сессию; у захвата человека и обычного токена
    её нет), нет доказательства прогона хаба и нет ни одной живой сессии.
    """
    rows = await fetchall(
        db,
        "SELECT t.id, t.status FROM tasks t WHERE t.status IN ('running', 'claimed') "
        "AND t.claim_nonce IS NOT NULL AND t.executor_run_id IS NULL "
        "AND (t.claimed_by IS NOT NULL OR t.claim_session_id IS NOT NULL) "
        "AND COALESCE(t.job_id, '') = '' AND NOT EXISTS ("
        "  SELECT 1 FROM chat_pair_sessions live WHERE live.kind = 'implementer' "
        "  AND live.bound_task_id = t.id AND live.revoked_at IS NULL "
        "  AND live.expires_at > datetime('now'))",
    )
    freed: list[int] = []
    for raw in rows:
        task_id = int(raw["id"])
        if task_id in exclude:
            continue
        try:
            done = await _release_atomically(
                db,
                task_id,
                from_status=str(raw["status"]),
                note="Implementer pairing session expired; task returned to open.",
                event_kind="chat_pair_implementer_expired",
                session_runs=frozenset({None}),
                session_claim=True,
            )
        except Exception:  # noqa: BLE001 - одна задача не останавливает остальные
            log.exception("Chat-pair reaper: orphan claim of #%d failed", task_id)
            if failed is not None:
                failed.add(task_id)
            continue
        if done:
            freed.append(task_id)
    return freed


async def release_stuck_run_claims(
    db: aiosqlite.Connection, failed: set[int] | None = None
) -> list[int]:
    """Второй проход reaper'а — от задач, а не от сессий (#1783).

    Первый проход идёт от мёртвых сессий, а purge их удаляет; здесь кандидат -
    сама задача с захватом закончившегося прогона, решает ``run_claim`` под
    write-локом. Тем же проходом - брошенный возврат на доработку (#2053).
    """
    from hub.services import run_claim

    freed: list[int] = []
    for task_id in await run_claim.stuck_candidates(db):
        try:
            async with write_transaction(db):
                await db.execute("SAVEPOINT stuck_release")
                try:
                    done = await run_claim.release_stuck_task(db, task_id)
                except BaseException:
                    await db.execute("ROLLBACK TO stuck_release")
                    await db.execute("RELEASE stuck_release")
                    raise
                await db.execute("RELEASE stuck_release")
        except Exception:  # noqa: BLE001 - одна задача не останавливает остальные
            log.exception("Chat-pair reaper: stuck claim of #%d failed", task_id)
            if failed is not None:
                failed.add(task_id)
            continue
        if done:
            freed.append(task_id)
    await run_claim.reopen_abandoned_rework(db)  # #2053
    return freed


async def purge_expired(db: aiosqlite.Connection) -> int:
    """Drop what can no longer be used: the reaper's half of the channel.

    Spent codes linger for a retention window so "was this code used?" has an
    answer for a while; everything expired or revoked goes immediately — except reviewer codes,
    which are kept the same window for diagnosis (#1694).
    Dead implementer sessions unstick their bound pair task first (#983),
    otherwise purge would delete the only row that still named the task.
    """
    unreleased: set[int] = set()
    await release_expired_implementer_tasks(db, unreleased)
    hours = int(config.CHAT_PAIR_SPENT_RETENTION_HOURS)
    # Reviewer rows stay a window past their end (#1694): a refused redeem is
    # explained by looking the row up, and a row dropped the minute it expired
    # turns "expired" into "unknown". Spent reviewer rows count from redeemed_at.
    codes = await db.execute(
        "DELETE FROM chat_pair_codes WHERE ("
        "(kind = 'reviewer' AND ((redeemed_at IS NULL AND expires_at < datetime('now', ?)) "
        "  OR (redeemed_at IS NOT NULL AND redeemed_at < datetime('now', ?)))) "
        "OR (kind <> 'reviewer' AND (expires_at < datetime('now') "
        "  OR (redeemed_at IS NOT NULL AND redeemed_at < datetime('now', ?))))) "
        # #1825: a code whose order can still be retried is kept until retry_until.
        "AND NOT EXISTS (SELECT 1 FROM executor_orders o "
        "WHERE o.pair_code_id = chat_pair_codes.id AND o.retry_until > datetime('now'))",
        (f"-{hours} hours", f"-{hours} hours", f"-{hours} hours"),
    )
    # A task whose release just failed keeps its dead sessions: they are the
    # only thing the next pass can find it by (#1712).
    keep = ", ".join("?" for _ in unreleased)
    # #1783: пока захват задачи ссылается на прогон, строки сессий этого прогона
    # остаются — по ним задачу находит и объясняет проход reaper'а.
    sessions = await db.execute(
        "DELETE FROM chat_pair_sessions WHERE (expires_at < datetime('now') "  # nosec B608 - placeholders only
        "OR revoked_at IS NOT NULL) "
        "AND NOT EXISTS (SELECT 1 FROM tasks t WHERE "
        "t.executor_run_id IS NOT NULL "
        "AND t.executor_run_id = chat_pair_sessions.executor_run_id "
        "AND t.id = chat_pair_sessions.bound_task_id "
        "AND t.status IN ('claimed', 'running', 'review', 'fix_requested', "
        "'needs_decision', 'ci_check', 'pending_report', 'completed'))"
        + (f" AND COALESCE(bound_task_id, -1) NOT IN ({keep})" if unreleased else ""),
        tuple(sorted(unreleased)),
    )
    removed = int(codes.rowcount or 0) + int(sessions.rowcount or 0)
    await db.commit()
    return removed


def render_agent_prompt(code: str, base_url: str) -> str:
    """Instruction pasted into a cloud chat with the code (#1715).

    One source for the text: ``chat_pair_prompt.txt``. Plain text, so no
    autoescape (only .html templates are escaped) — the code and base_url come from the hub itself, and HTML
    escaping would corrupt a curl line the agent has to run verbatim.
    """
    from jinja2 import (
        Environment,
        FileSystemLoader,
        StrictUndefined,
        select_autoescape,
    )

    env = Environment(
        loader=FileSystemLoader(str(Path(__file__).parent.parent / "templates")),
        autoescape=select_autoescape(enabled_extensions=("html",)),
        undefined=StrictUndefined,
        keep_trailing_newline=False,
    )
    return env.get_template("chat_pair_prompt.txt").render(code=code, base_url=base_url)
