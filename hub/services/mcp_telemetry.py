"""MCP usage telemetry (#780, epic #776): what the Agent API costs, measured.

The hub publishes 60+ tools and tens of thousands of characters of catalog to
every agent on every turn, and until now the only usage it counted was
deprecated aliases. A core surface picked without measurement is picked by
taste, and taste is exactly how the surface grew this large.

Three properties decide the design, and each is a mechanism rather than a
promise:

1. **The record is metadata, never content.** There is no column an argument
   value, a token or a response body could be written into (see the
   ``mcp_call_events`` migration), and the two fields that could smuggle one in
   are closed here: ``error_reason`` accepts only a hub-produced slug, and the
   task reference is read only when the argument is already an integer. A
   secret cannot reach storage through a code path — it would take an ALTER
   TABLE, which a review can see.

2. **Measuring must not change what it measures.** One INSERT per call, no
   read-back, no aggregation in the call path, and every failure inside
   telemetry is swallowed: a hub that refuses a tool call because it could not
   record the tool call has turned its own instrumentation into an outage.

3. **Size means model-visible size.** Latency is what the agent waits for;
   characters are what it pays for on every subsequent turn. Both are recorded
   per call, so "which tools are expensive" stops being a guess.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import aiosqlite

from hub import config
from hub import repository as repo

log = logging.getLogger("hub.mcp_telemetry")

# An error_reason is a hub-produced classifier, not a message. Anything that
# does not look like a slug becomes ``unclassified``: an error string is where
# argument values surface ("task 42 assigned to <token>"), so the shape is
# checked rather than trusted.
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

UNCLASSIFIED_REASON = "unclassified"

STATUS_OK = "ok"
STATUS_ERROR = "error"

# How big a tool answer may be before we stop reading it looking for a reason
# slug. Well past any real hub envelope; a cap keeps a pathological response
# from turning the call path into a parser.
_MAX_REASON_SCAN_CHARS = 20_000


# ---------------------------------------------------------------------------
# Where the records go
# ---------------------------------------------------------------------------

# The MCP surface is mounted inside the hub app, but it is also importable on
# its own (stdio against a remote hub), where there is no database to write to.
# The connection is therefore handed in by whoever owns one — the app lifespan
# — instead of being discovered by importing the app, which would be a cycle
# and would also make "no hub here" look like a bug rather than a mode.
_db: aiosqlite.Connection | None = None


def set_telemetry_sink(db: aiosqlite.Connection | None) -> None:
    """Point telemetry at a live connection (or ``None`` to switch it off)."""
    global _db
    _db = db


def telemetry_sink() -> aiosqlite.Connection | None:
    return _db


# ---------------------------------------------------------------------------
# Classification and measurement
# ---------------------------------------------------------------------------


def normalize_reason(value: Any) -> str:
    """Return ``value`` if it is a slug, else ``unclassified``."""
    if isinstance(value, str) and _REASON_RE.match(value):
        return value
    return UNCLASSIFIED_REASON


def _exception_reason(exc: BaseException) -> str:
    """Classify by exception type. The message is deliberately not read.

    The MCP SDK wraps every tool failure in one ``ToolError``, so the type at
    the top says only "a tool failed". The chain is followed to its root
    instead: ``connect_error`` and ``timeout_exception`` are different
    operational facts, and the type name carries no argument values the way
    the message it replaces would.
    """
    root = exc
    for _ in range(3):
        cause = root.__cause__
        if cause is None:
            break
        root = cause
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", type(root).__name__).lower()
    return normalize_reason(snake)


def _block_chars(block: Any) -> int:
    """Model-visible size of one content block."""
    text = getattr(block, "text", None)
    if isinstance(text, str):
        return len(text)
    data = getattr(block, "data", None)
    if isinstance(data, str):
        return len(data)
    dump = getattr(block, "model_dump_json", None)
    if callable(dump):
        try:
            return len(dump(exclude_none=True))
        except Exception:
            return 0
    if isinstance(block, str):
        return len(block)
    return len(str(block))


def _json_chars(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False))
    except Exception:
        return len(str(value))


def measure_response(result: Any) -> int:
    """Characters this answer costs the model.

    FastMCP hands back one of three shapes depending on the tool — a sequence
    of content blocks, a ``(blocks, structured)`` pair, or a whole
    ``CallToolResult`` — and all three are what the agent actually reads, so
    all three are measured the same way instead of one being counted as zero.
    """
    if result is None:
        return 0
    content = getattr(result, "content", None)
    if content is not None:
        total = sum(_block_chars(block) for block in content)
        structured = getattr(result, "structuredContent", None)
        if structured:
            total += _json_chars(structured)
        return total
    if isinstance(result, tuple) and len(result) == 2:
        blocks, structured = result
        total = sum(_block_chars(block) for block in blocks or [])
        if structured:
            total += _json_chars(structured)
        return total
    if isinstance(result, (list, tuple)):
        return sum(_block_chars(block) for block in result)
    if isinstance(result, dict):
        return _json_chars(result)
    return _block_chars(result)


def _first_text(result: Any) -> str:
    """The first text block of an answer, capped. Used only to read a reason."""
    blocks: Any = None
    content = getattr(result, "content", None)
    if content is not None:
        blocks = content
    elif isinstance(result, tuple) and len(result) == 2:
        blocks = result[0]
    elif isinstance(result, (list, tuple)):
        blocks = result
    if not blocks:
        return ""
    text = getattr(blocks[0], "text", None)
    if not isinstance(text, str):
        return ""
    return text[:_MAX_REASON_SCAN_CHARS]


def classify_result(result: Any) -> tuple[str, str]:
    """Did this call succeed, and if not, why — from the hub's own envelope.

    Hub tools answer a refused call with a JSON envelope carrying a ``reason``
    slug and HTTP-style success otherwise, so a call that "returned normally"
    is not the same thing as a call that worked. Only the ``reason`` key is
    ever read out of that envelope; ``message`` and ``hint`` are free text and
    are left where they are.
    """
    if getattr(result, "isError", False):
        return STATUS_ERROR, normalize_reason(None)
    text = _first_text(result)
    if not text.startswith("{"):
        return STATUS_OK, ""
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return STATUS_OK, ""
    if not isinstance(payload, dict):
        return STATUS_OK, ""
    reason = payload.get("reason")
    if reason is None:
        return STATUS_OK, ""
    return STATUS_ERROR, normalize_reason(reason)


def task_reference(arguments: Any) -> int | None:
    """The task a call is about, when the argument is already a number.

    A task id is the one piece of routing worth having in a usage report. It is
    read only when the caller passed an integer: accepting a string here would
    open the one door this table is built to keep shut.
    """
    if not isinstance(arguments, dict):
        return None
    value = arguments.get("task_id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


# ---------------------------------------------------------------------------
# The write path
# ---------------------------------------------------------------------------


async def record_call(
    *,
    tool: str,
    arguments: Any,
    latency_ms: int,
    result: Any = None,
    error: BaseException | None = None,
    principal_id: int | None = None,
    principal_role: str = "",
    profile: str = "",
) -> bool:
    """Record one MCP call. Returns whether a row was written.

    Never raises. A tool call that already produced its answer must not fail
    because the hub could not write down that it happened.
    """
    if not config.MCP_TELEMETRY_ENABLED:
        return False
    db = _db
    if db is None:
        return False
    if error is not None:
        status, reason = STATUS_ERROR, _exception_reason(error)
        chars = 0
    else:
        status, reason = classify_result(result)
        chars = measure_response(result)
    try:
        await repo.insert_mcp_call_event(
            db,
            tool=tool,
            profile=profile or config.MCP_PROFILE,
            principal_id=principal_id,
            principal_role=principal_role,
            status=status,
            error_reason=reason,
            latency_ms=max(0, int(latency_ms)),
            response_chars=max(0, int(chars)),
            task_id=task_reference(arguments),
        )
        await db.commit()
        return True
    except Exception:
        log.debug("mcp telemetry write failed for %s", tool, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# The read path
# ---------------------------------------------------------------------------


def normalize_window(window_days: Any) -> int:
    """Clamp a requested window to what the hub actually keeps.

    Retention is longer than this ceiling by design, so the longest window a
    caller can ask for is one the data fully covers. A report that silently
    answers from a horizon shorter than it claims is worse than a refusal.
    """
    try:
        days = int(window_days)
    except (TypeError, ValueError):
        days = 14
    return max(1, min(days, config.MCP_TELEMETRY_MAX_WINDOW_DAYS))


def _rate(part: int, total: int) -> float:
    return round(part / total, 4) if total else 0.0


def _with_rates(row: dict[str, Any]) -> dict[str, Any]:
    calls = int(row.get("calls") or 0)
    ok_calls = int(row.get("ok_calls") or 0)
    error_calls = int(row.get("error_calls") or 0)
    out = dict(row)
    out["calls"] = calls
    out["ok_calls"] = ok_calls
    out["error_calls"] = error_calls
    out["success_rate"] = _rate(ok_calls, calls)
    out["error_rate"] = _rate(error_calls, calls)
    out["total_chars"] = int(row.get("total_chars") or 0)
    for key in (
        "p50_latency_ms",
        "p95_latency_ms",
        "p50_response_chars",
        "p95_response_chars",
    ):
        if key in out:
            out[key] = int(out[key] or 0)
    return out


async def usage_report(
    db: aiosqlite.Connection,
    *,
    window_days: int = 14,
    catalog: dict[str, Any] | None = None,
    error_limit: int = 20,
) -> dict[str, Any]:
    """Popularity, error rate and cost of the Agent API over a window.

    ``catalog`` is the published tool catalog when the caller has one. It is
    what turns the report from "the tools that were used" into "the tools that
    exist" — a tool nobody called all quarter is the finding this feature is
    for, and it is invisible in call records alone.
    """
    days = normalize_window(window_days)
    by_tool = [
        _with_rates(row) for row in await repo.mcp_usage_by_tool(db, window_days=days)
    ]
    by_profile = [
        _with_rates(row)
        for row in await repo.mcp_usage_by_profile(db, window_days=days)
    ]
    top_errors = await repo.mcp_usage_errors(db, window_days=days, limit=error_limit)

    calls = sum(row["calls"] for row in by_tool)
    ok_calls = sum(row["ok_calls"] for row in by_tool)
    error_calls = sum(row["error_calls"] for row in by_tool)
    total_chars = sum(row["total_chars"] for row in by_tool)
    principals = max((int(row.get("principals") or 0) for row in by_profile), default=0)

    used = {row["tool"] for row in by_tool}
    catalog_tools = list((catalog or {}).get("tools_list") or [])
    published = [entry["name"] for entry in catalog_tools if isinstance(entry, dict)]
    unused = sorted(name for name in published if name not in used)

    report: dict[str, Any] = {
        "window_days": days,
        "retention_days": config.MCP_TELEMETRY_RETENTION_DAYS,
        "enabled": config.MCP_TELEMETRY_ENABLED,
        "totals": {
            "calls": calls,
            "tools_used": len(used),
            "principals": principals,
            "ok_calls": ok_calls,
            "error_calls": error_calls,
            "error_rate": _rate(error_calls, calls),
            "response_chars": total_chars,
            "avg_response_chars": round(total_chars / calls) if calls else 0,
        },
        "by_tool": by_tool,
        "by_profile": by_profile,
        "top_errors": [dict(row) for row in top_errors],
        "unused_tools": unused,
        "published_tools": len(published),
    }
    if catalog is not None:
        # The per-tool list stays out: a usage report is read on every
        # dashboard load, and the catalog totals are the part that answers
        # "what does the surface itself cost".
        report["catalog"] = {
            key: value for key, value in catalog.items() if key != "tools_list"
        }
    return report
