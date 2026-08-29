"""MCP usage telemetry (#780): what is recorded, and what can never be.

AC-1 is proved the only way worth proving it: a real MCP call is made with a
secret in its arguments, and the whole telemetry table is then scanned for that
secret. Asserting on the columns alone would pass just as happily on a schema
that also had a ``payload`` column somebody forgot about.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hub import config
from hub import repository as repo
from hub.mcp_server import mcp
from hub.services import mcp_telemetry as telemetry

# A value that must never reach storage: it is passed as a normal tool
# argument, exactly the way a token or a message body would be.
SENTINEL = "sk-live-sentinel-9f3a-do-not-store"


@pytest.fixture
async def sink(db):
    """Point telemetry at the test database for the duration of one test."""
    telemetry.set_telemetry_sink(db)
    yield db
    telemetry.set_telemetry_sink(None)


async def _events(db) -> list[dict]:
    rows = await db.execute_fetchall("SELECT * FROM mcp_call_events ORDER BY id")
    return [dict(row) for row in rows]


async def _dump(db) -> str:
    """Every value in the telemetry table as one string, for the scan."""
    rows = await _events(db)
    return " ".join(str(value) for row in rows for value in row.values())


async def _call_claim(task_id: int = 41, agent: str = "composer") -> None:
    with (
        patch("hub.mcp_server._api_post", new_callable=AsyncMock) as post,
        patch("hub.mcp_server._api_get", new_callable=AsyncMock) as get,
    ):
        post.return_value = {"status": "claimed", "claimed_by": agent}
        get.side_effect = [
            {"id": task_id, "status": "open"},
            {"id": task_id, "status": "claimed", "claimed_by": agent},
        ]
        await mcp.call_tool(
            "hub_claim_task", {"task_id": task_id, "agent": agent, "session_id": ""}
        )


# ---------------------------------------------------------------------------
# AC-1: the record is metadata, and the secret is not in it
# ---------------------------------------------------------------------------


async def test_successful_call_is_recorded_with_allowlist_metadata(sink):
    await _call_claim()

    events = await _events(sink)
    assert len(events) == 1
    event = events[0]
    assert event["tool"] == "hub_claim_task"
    assert event["profile"] == config.MCP_PROFILE
    assert event["status"] == "ok"
    assert event["error_reason"] == ""
    assert event["latency_ms"] >= 0
    assert event["response_chars"] > 0
    assert event["task_id"] == 41
    assert event["unknown_arg_count"] == 0
    assert set(event) == {"id", "created_at"} | set(repo.MCP_CALL_EVENT_COLUMNS)


async def test_unknown_arguments_are_counted_without_storing_values(sink):
    """AC-4 (#1015): a discard is countable metadata; the dropped value is not stored."""
    with (
        patch("hub.mcp_server._api_post", new_callable=AsyncMock) as post,
        patch("hub.mcp_server._api_get", new_callable=AsyncMock) as get,
    ):
        post.return_value = {"status": "claimed", "claimed_by": "composer"}
        get.side_effect = [
            {"id": 41, "status": "open"},
            {"id": 41, "status": "claimed", "claimed_by": "composer"},
        ]
        await mcp.call_tool(
            "hub_claim_task",
            {
                "task_id": 41,
                "agent": "composer",
                "session_id": "",
                "bogus": SENTINEL,
            },
        )

    events = await _events(sink)
    assert len(events) == 1
    assert events[0]["unknown_arg_count"] == 1
    assert SENTINEL not in await _dump(sink)


async def test_argument_values_never_reach_storage(sink):
    """AC-1: a secret passed as an argument is absent from the whole table."""
    await _call_claim(agent=SENTINEL)

    events = await _events(sink)
    assert len(events) == 1
    assert SENTINEL not in await _dump(sink)


async def test_failed_call_is_recorded_and_still_hides_the_argument(sink):
    """A refusal is the interesting case: its message quotes the arguments."""
    from hub.mcp_server import HubApiError

    with (
        patch("hub.mcp_server._api_post", new_callable=AsyncMock) as post,
        patch("hub.mcp_server._api_get", new_callable=AsyncMock) as get,
    ):
        get.return_value = {"id": 7, "status": "claimed"}
        post.side_effect = HubApiError(
            {
                "reason": "already_claimed",
                "message": f"task #7 is held by {SENTINEL}",
                "hint": "release it first",
            }
        )
        await mcp.call_tool(
            "hub_claim_task", {"task_id": 7, "agent": SENTINEL, "session_id": ""}
        )

    events = await _events(sink)
    assert len(events) == 1
    assert events[0]["status"] == "error"
    assert events[0]["error_reason"] == "already_claimed"
    assert SENTINEL not in await _dump(sink)


async def test_reason_that_is_not_a_slug_is_not_stored_verbatim(sink):
    """The reason column takes a classifier, never free text."""
    assert telemetry.normalize_reason("already_claimed") == "already_claimed"
    for hostile in (
        f"task held by {SENTINEL}",
        "Reason: Denied",
        {"reason": "nested"},
        None,
        123,
    ):
        assert telemetry.normalize_reason(hostile) == telemetry.UNCLASSIFIED_REASON


async def test_raised_tool_error_is_classified_by_its_root_cause(sink):
    with (
        patch("hub.mcp_server._api_post", new_callable=AsyncMock),
        patch("hub.mcp_server._api_get", new_callable=AsyncMock) as get,
    ):
        get.side_effect = TimeoutError("connect timed out")
        with pytest.raises(Exception):
            await mcp.call_tool("hub_task_status", {"task_id": 5})

    events = await _events(sink)
    assert len(events) == 1
    assert events[0]["status"] == "error"
    # The SDK wraps everything in ToolError; the root cause is what is useful.
    assert events[0]["error_reason"] == "timeout_error"
    assert events[0]["tool"] == "hub_task_status"


def test_acronym_class_names_become_readable_slugs():
    """AC-1 (#809): an acronym is a word, not a run of separate letters.

    Both spellings group calls equally well — the slug is only ever a key.
    Only one of them can be read in the report the column exists to fill,
    which is why this is worth a test rather than a preference.
    """

    class HTTPStatusError(Exception):
        pass

    class JSONDecodeError(Exception):
        pass

    assert telemetry._exception_reason(HTTPStatusError()) == "http_status_error"
    assert telemetry._exception_reason(JSONDecodeError()) == "json_decode_error"
    assert telemetry._exception_reason(OSError()) == "os_error"


def test_existing_reason_slugs_survive_the_fix():
    """AC-2 (#809): the readability fix must not re-key what already worked.

    A changed slug for an unchanged cause would split one reason across two
    names in the report — trading an unreadable answer for a wrong one.
    """
    assert telemetry._exception_reason(TimeoutError()) == "timeout_error"
    assert telemetry._exception_reason(ConnectionError()) == "connection_error"
    assert telemetry._exception_reason(Exception()) == "exception"

    # Slugs the hub produces itself pass through untouched: they never went
    # near the camel-case rule, and AC-2 pins that they still do not.
    assert telemetry.normalize_reason("already_claimed") == "already_claimed"
    assert telemetry.normalize_reason("human_decision_required") == (
        "human_decision_required"
    )


async def test_task_reference_is_taken_only_from_an_integer_argument():
    assert telemetry.task_reference({"task_id": 42}) == 42
    assert telemetry.task_reference({"task_id": "42"}) is None
    assert telemetry.task_reference({"task_id": f"42 {SENTINEL}"}) is None
    assert telemetry.task_reference({"task_id": True}) is None
    assert telemetry.task_reference({}) is None
    assert telemetry.task_reference("not a dict") is None


async def test_caller_identity_is_recorded_when_the_middleware_set_one(sink):
    from hub.mcp_internal_auth import identity_context_reset, identity_context_set

    handle = identity_context_set(7, "agent")
    try:
        await _call_claim()
    finally:
        identity_context_reset(handle)

    events = await _events(sink)
    assert events[0]["principal_id"] == 7
    assert events[0]["principal_role"] == "agent"


# ---------------------------------------------------------------------------
# Telemetry must never be able to break a tool call
# ---------------------------------------------------------------------------


async def test_call_succeeds_with_no_sink_configured(db):
    telemetry.set_telemetry_sink(None)
    await _call_claim()
    assert await _events(db) == []


async def test_disabled_telemetry_records_nothing(sink, monkeypatch):
    monkeypatch.setattr(config, "MCP_TELEMETRY_ENABLED", False)
    await _call_claim()
    assert await _events(sink) == []


async def test_write_failure_does_not_fail_the_call(sink, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(repo, "insert_mcp_call_event", boom)
    await _call_claim()  # must not raise
    assert await _events(sink) == []


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def test_measure_response_counts_text_and_structured_content():
    from mcp.types import CallToolResult, TextContent

    blocks = [TextContent(type="text", text="12345")]
    assert telemetry.measure_response(blocks) == 5
    assert telemetry.measure_response((blocks, {"a": "b"})) == 5 + len('{"a": "b"}')
    result = CallToolResult(content=blocks, structuredContent={"a": "b"})
    assert telemetry.measure_response(result) == 5 + len('{"a": "b"}')
    assert telemetry.measure_response(None) == 0


async def test_structured_tool_response_size_is_not_counted_as_zero(sink):
    """Structured tools answer with a different shape — it is still cost."""
    with (
        patch("hub.mcp_server._api_post", new_callable=AsyncMock),
        patch("hub.mcp_server._api_get", new_callable=AsyncMock) as get,
    ):
        get.return_value = {
            "id": 5,
            "title": "t",
            "status": "open",
            "created_at": "2026-08-21",
            "updates": [],
        }
        await mcp.call_tool("hub_task_status", {"task_id": 5})

    events = await _events(sink)
    assert events[0]["response_chars"] > 0


# ---------------------------------------------------------------------------
# AC-2: the report
# ---------------------------------------------------------------------------


async def _seed(db, rows: list[dict]) -> None:
    for row in rows:
        await repo.insert_mcp_call_event(
            db,
            tool=row.get("tool", "hub_task_status"),
            profile=row.get("profile", "v1"),
            principal_id=row.get("principal_id", 1),
            principal_role=row.get("principal_role", "agent"),
            status=row.get("status", "ok"),
            error_reason=row.get("error_reason", ""),
            latency_ms=row.get("latency_ms", 10),
            response_chars=row.get("response_chars", 100),
            task_id=row.get("task_id"),
        )
    await db.commit()


async def test_usage_report_answers_popularity_errors_latency_and_size(db):
    await _seed(
        db,
        [
            {"tool": "hub_task_status", "latency_ms": i, "response_chars": i * 10}
            for i in range(1, 21)
        ]
        + [
            {
                "tool": "hub_claim_task",
                "status": "error",
                "error_reason": "already_claimed",
                "principal_id": 2,
                "latency_ms": 5,
                "response_chars": 50,
            }
        ],
    )

    report = await telemetry.usage_report(db, window_days=14)

    assert report["window_days"] == 14
    assert report["totals"]["calls"] == 21
    assert report["totals"]["error_calls"] == 1
    assert report["totals"]["error_rate"] == pytest.approx(1 / 21, abs=1e-4)
    assert report["totals"]["principals"] == 2

    by_tool = {row["tool"]: row for row in report["by_tool"]}
    status = by_tool["hub_task_status"]
    assert status["calls"] == 20
    assert status["principals"] == 1
    assert status["error_calls"] == 0
    # Nearest rank over 1..20: p50 = 10th value, p95 = 19th.
    assert status["p50_latency_ms"] == 10
    assert status["p95_latency_ms"] == 19
    assert status["p50_response_chars"] == 100
    assert status["p95_response_chars"] == 190

    claim = by_tool["hub_claim_task"]
    assert claim["error_rate"] == 1.0
    assert report["top_errors"][0]["error_reason"] == "already_claimed"

    profiles = {row["profile"]: row for row in report["by_profile"]}
    assert profiles["v1"]["calls"] == 21
    assert profiles["v1"]["tools"] == 2


async def test_usage_report_breaks_down_calls_by_role(db):
    """AC-1 (#816): the report answers who is calling, not only what.

    Without this the "nobody called it" list cannot be read: a review tool
    with zero calls is indistinguishable from a review tool whose caller
    never comes through MCP at all.
    """
    await _seed(
        db,
        [
            {"tool": "hub_task_status", "principal_role": "agent", "principal_id": 1},
            {"tool": "hub_claim_task", "principal_role": "agent", "principal_id": 1},
            {
                "tool": "hub_submit_review",
                "principal_role": "reviewer",
                "principal_id": 2,
                "status": "error",
                "error_reason": "not_reviewer",
                "response_chars": 40,
            },
        ],
    )

    report = await telemetry.usage_report(db, window_days=14)

    roles = {row["principal_role"]: row for row in report["by_role"]}
    assert set(roles) == {"agent", "reviewer"}
    assert roles["agent"]["calls"] == 2
    assert roles["agent"]["tools"] == 2
    assert roles["agent"]["principals"] == 1
    assert roles["agent"]["error_calls"] == 0
    assert roles["reviewer"]["calls"] == 1
    assert roles["reviewer"]["error_rate"] == 1.0
    assert roles["reviewer"]["total_chars"] == 40
    # Same window rules as every other section of the report.
    assert sum(row["calls"] for row in report["by_role"]) == report["totals"]["calls"]


async def test_usage_report_names_tools_nobody_called(db):
    await _seed(db, [{"tool": "hub_task_status"}])
    catalog = {
        "tools": 3,
        "tools_list": [
            {"name": "hub_task_status", "total_chars": 100},
            {"name": "hub_dispatch_jobs", "total_chars": 200},
            {"name": "hub_archive_task", "total_chars": 300},
        ],
        "model_visible_chars": 1000,
    }

    report = await telemetry.usage_report(db, window_days=30, catalog=catalog)

    assert report["unused_tools"] == ["hub_archive_task", "hub_dispatch_jobs"]
    assert report["published_tools"] == 3
    assert report["catalog"]["model_visible_chars"] == 1000
    assert "tools_list" not in report["catalog"]


async def test_report_window_is_clamped_to_what_retention_covers(db):
    report = await telemetry.usage_report(db, window_days=3650)
    assert report["window_days"] == config.MCP_TELEMETRY_MAX_WINDOW_DAYS
    assert report["retention_days"] > config.MCP_TELEMETRY_MAX_WINDOW_DAYS
    assert telemetry.normalize_window("nonsense") == 14
    assert telemetry.normalize_window(0) == 1


async def test_report_window_excludes_older_records(db):
    await _seed(db, [{"tool": "hub_task_status"}])
    await db.execute(
        "UPDATE mcp_call_events SET created_at = datetime('now', '-40 days')"
    )
    await db.commit()

    assert (await telemetry.usage_report(db, window_days=14))["totals"]["calls"] == 0
    assert (await telemetry.usage_report(db, window_days=90))["totals"]["calls"] == 1


async def test_retention_prunes_only_what_is_past_the_horizon(db):
    await _seed(db, [{"tool": "a"}, {"tool": "b"}])
    await db.execute(
        "UPDATE mcp_call_events SET created_at = datetime('now', '-200 days') "
        "WHERE tool = 'a'"
    )
    await db.commit()

    removed = await repo.prune_mcp_call_events(db, keep_days=120)
    await db.commit()

    assert removed == 1
    assert [row["tool"] for row in await _events(db)] == ["b"]


def test_error_reason_prefers_hub_envelope_over_transport() -> None:
    """#882 AC-2: the hub said why; the column must not say 'http_status_error'.

    A refusal reaches telemetry as ``ToolError(...) from HubApiError(...) from
    HTTPStatusError``. Classifying by root type walks straight past the answer
    to the transport underneath it — 12 of the first window's 48 errors were
    filed that way, none naming a cause.
    """
    from hub.mcp_server import HubApiError
    from hub.services import mcp_telemetry as telemetry

    class HTTPStatusError(Exception):
        pass

    refusal = HubApiError({"reason": "human_decision_required", "message": "gate"})
    refusal.__cause__ = HTTPStatusError()
    wrapped = Exception("Error executing tool hub_refine_task: gate")
    wrapped.__cause__ = refusal

    assert telemetry._exception_reason(wrapped) == "human_decision_required"
    # ...and the transport is still what is written when nobody said why.
    assert telemetry._exception_reason(HTTPStatusError()) == "http_status_error"


def test_envelope_reason_accepts_only_slugs() -> None:
    """A payload is hub-produced, but this column takes slugs from nobody.

    ``error_reason`` is the one field an argument value could reach storage
    through, so the shape is checked rather than the source trusted: a message
    that happens to sit under ``reason`` falls back to the type, it does not
    become a key.
    """
    from hub.mcp_server import HubApiError
    from hub.services import mcp_telemetry as telemetry

    prose = HubApiError({"reason": "task 42 assigned to somebody", "message": "x"})
    assert telemetry._exception_reason(prose) == "hub_api_error"

    missing = HubApiError({"message": "no reason at all"})
    assert telemetry._exception_reason(missing) == "hub_api_error"
