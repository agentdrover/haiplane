"""Cases of the executor_autonomy section (#1980).

Kept out of tests/test_practice_metrics.py (its file-size ceiling) and imported
there, so their ids stay ``tests/test_practice_metrics.py::<name>``.
Timestamps are explicit UTC strings written to SQLite, never the Python clock.
"""

from __future__ import annotations

import json

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.services.orchestration import practice_metrics


async def _task(
    db: aiosqlite.Connection, *, title: str, project_id: int | None = None
) -> int:
    task_id = await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="",
        rationale="",
        status="draft",
        auto_review=False,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    if project_id is not None:
        await repo.update_task(db, task_id, project_id=project_id)
    return task_id


# --- executor_autonomy (#1980): weekly share of host orders submitted unaided ---


async def _autonomy_host(db: aiosqlite.Connection, name: str = "k8") -> None:
    from hub.services import admin as admin_svc

    principal = await admin_svc.create_principal(
        db, kind="service", username=f"executor-host-{name}", role_slug="slot_runner"
    )
    await db.execute(
        "INSERT INTO executor_hosts (name, principal_id, public_key_pem) "
        "VALUES (?, ?, 'test-only')",
        (name, principal["id"]),
    )


async def _autonomy_order(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    host: str = "k8",
    closed_at: str | None,
    outcome: str = "",
    close_reason: str = "",
    code: str = "",
    state: str = "closed",
    reported_at: str | None = None,
    created_at: str = "2026-10-01 00:00:00",
) -> None:
    """One executor order with explicit UTC strings (never the Python clock)."""
    run = await db.execute("INSERT INTO executor_runs (task_id) VALUES (?)", (task_id,))
    await db.execute(
        "INSERT INTO executor_orders (host, task_id, generation, executor_run_id, "
        "state, expires_at, closed_at, close_reason, report_outcome, "
        "report_reason_code, reported_at, created_at) "
        "VALUES (?, ?, 1, ?, ?, '2030-01-01 00:00:00', ?, ?, ?, ?, ?, ?)",
        (
            host,
            task_id,
            run.lastrowid,
            state,
            closed_at,
            close_reason,
            outcome,
            code,
            reported_at,
            created_at,
        ),
    )


async def _autonomy_week_fixture(db: aiosqlite.Connection) -> int:
    await _autonomy_host(db)
    task = await _task(db, title="autonomy")
    sub = ("submitted", "submitted")
    for outcome, reason in (
        sub,
        sub,
        ("rescued", "reported_rescued"),
        ("failed", "redeem_failed"),
        ("rescued", "submitted"),
    ):
        await _autonomy_order(
            db,
            task,
            closed_at="2026-10-21 10:00:00",
            outcome=outcome,
            close_reason=reason,
        )
    # Not closed, yet closed_at is filled: only the state excludes it.
    await _autonomy_order(
        db,
        task,
        closed_at="2026-10-21 11:00:00",
        outcome="submitted",
        close_reason="submitted",
        state="pending",
    )
    await _autonomy_order(db, task, closed_at=None, outcome="submitted")
    await db.commit()
    return task


_AUTONOMY_WINDOW = {"date_from": "2026-10-19", "date_to": "2026-11-01"}


async def test_executor_autonomy_weekly_rate(db: aiosqlite.Connection):
    """AC-1 (#1980): one week of k8 orders gives the share submitted unaided."""
    await _autonomy_week_fixture(db)
    section = (await practice_metrics(db, **_AUTONOMY_WINDOW))["executor_autonomy"]
    assert section["target"] == 0.9
    assert "submitted" in section["definition"]
    weeks = section["hosts"]["k8"]["weeks"]
    assert [w["week"] for w in weeks] == ["2026-W43", "2026-W44"]
    first, second = weeks
    assert first["start"] == "2026-10-19"
    assert first["orders_closed"] == 5 and first["autonomous"] == 2
    assert first["rate"] == 0.4
    assert first["by_outcome"] == {"submitted": 2, "rescued": 2, "failed": 1}
    assert first["partial"] is False and second["partial"] is False
    assert second["orders_closed"] == 0 and second["autonomous"] == 0
    assert second["rate"] is None
    assert second["by_outcome"] == {} and second["by_reason_code"] == {}


async def test_executor_autonomy_counts_orders_per_host(db: aiosqlite.Connection):
    """AC-2 (#1980): the unit is the order; another host stays in its own key."""
    await _autonomy_host(db, "k8")
    await _autonomy_host(db, "mac")
    task = await _task(db, title="two orders")
    at = "2026-10-21 10:00:00"
    await _autonomy_order(
        db, task, closed_at=at, outcome="rescued", close_reason="reported_rescued"
    )
    await _autonomy_order(
        db, task, closed_at=at, outcome="submitted", close_reason="submitted"
    )
    await _autonomy_order(
        db,
        task,
        host="mac",
        closed_at=at,
        outcome="submitted",
        close_reason="submitted",
    )
    await db.commit()
    hosts = (await practice_metrics(db, **_AUTONOMY_WINDOW))["executor_autonomy"][
        "hosts"
    ]
    k8 = hosts["k8"]["weeks"][0]
    assert (k8["orders_closed"], k8["autonomous"]) == (2, 1)
    mac = hosts["mac"]["weeks"][0]
    assert (mac["orders_closed"], mac["autonomous"]) == (1, 1)


async def test_executor_autonomy_reason_codes(db: aiosqlite.Connection):
    """AC-3 (#1980): reasons of the orders that were not unaided, ranked."""
    await _autonomy_host(db)
    task = await _task(db, title="reasons")
    at = "2026-10-21 10:00:00"
    for code in ("agent_failed", "critic_time_exhausted", "critic_time_exhausted", ""):
        await _autonomy_order(
            db, task, closed_at=at, outcome="failed", close_reason="x", code=code
        )
    await _autonomy_order(
        db,
        task,
        closed_at=at,
        outcome="submitted",
        close_reason="submitted",
        code="ignored_code",
    )
    await db.commit()
    week = (await practice_metrics(db, **_AUTONOMY_WINDOW))["executor_autonomy"][
        "hosts"
    ]["k8"]["weeks"][0]
    assert list(week["by_reason_code"].items()) == [
        ("critic_time_exhausted", 2),
        ("agent_failed", 1),
        ("unknown", 1),
    ]
    assert sum(week["by_reason_code"].values()) == (
        week["orders_closed"] - week["autonomous"]
    )


async def test_executor_autonomy_project_scope_via_epic(db: aiosqlite.Connection):
    """AC-4 (#1980): project comes from the epic; the model filter is inert."""
    await _autonomy_host(db)
    default_id = await repo.create_project(db, slug="default", name="Default")
    infra = await repo.create_project(db, slug="home-infra", name="Infra")
    epic_def = await _task(db, title="epic default", project_id=default_id)
    epic_inf = await _task(db, title="epic infra", project_id=infra)
    in_def = await _task(db, title="leaf default")
    in_inf = await _task(db, title="leaf infra")
    loose = await _task(db, title="no parent, no project")
    await repo.update_task(db, in_def, parent_id=epic_def)
    await repo.update_task(db, in_inf, parent_id=epic_inf)
    at = "2026-10-21 10:00:00"
    for task in (in_def, in_inf, loose):
        await _autonomy_order(
            db, task, closed_at=at, outcome="submitted", close_reason="submitted"
        )
    await db.commit()

    def count(result: dict) -> int:
        return result["executor_autonomy"]["hosts"]["k8"]["weeks"][0]["orders_closed"]

    window = _AUTONOMY_WINDOW
    assert count(await practice_metrics(db, project="home-infra", **window)) == 1
    assert count(await practice_metrics(db, project="default", **window)) == 2
    plain = await practice_metrics(db, **window)
    assert count(plain) == 3
    filtered = await practice_metrics(db, model="grok-4.6", **window)
    assert filtered["executor_autonomy"] == plain["executor_autonomy"]
    assert "executor_autonomy" in filtered["scope"]["model_independent"]
    assert "executor_autonomy" not in filtered["scope"]["unfiltered"]


async def test_executor_autonomy_week_boundaries(db: aiosqlite.Connection):
    """AC-5 (#1980): closed_at picks the window and the ISO week, in UTC."""
    await _autonomy_host(db)
    task = await _task(db, title="boundaries")
    done = {"outcome": "submitted", "close_reason": "submitted"}
    for created_at, closed_at in (
        ("2026-10-01 00:00:00", "2026-10-13 23:59:59"),  # before the window
        ("2026-10-01 00:00:00", "2026-10-14 08:00:00"),  # made before, closed inside
        ("2026-10-15 00:00:00", "2026-10-18 23:59:00"),  # Sunday: week 42
        ("2026-10-15 00:00:00", "2026-10-19 00:00:00"),  # Monday: week 43
        ("2026-10-15 00:00:00", "2026-10-22 00:00:00"),  # made inside, closed after
        ("2026-12-01 00:00:00", "2026-12-28 00:00:00"),
        ("2026-12-01 00:00:00", "2027-01-04 00:00:00"),
    ):
        await _autonomy_order(
            db, task, closed_at=closed_at, created_at=created_at, **done
        )
    await db.commit()

    result = await practice_metrics(db, date_from="2026-10-14", date_to="2026-10-21")
    weeks = result["executor_autonomy"]["hosts"]["k8"]["weeks"]
    assert [(w["week"], w["orders_closed"], w["partial"]) for w in weeks] == [
        ("2026-W42", 2, True),
        ("2026-W43", 1, True),
    ]

    result = await practice_metrics(db, date_from="2026-12-28", date_to="2027-01-06")
    weeks = result["executor_autonomy"]["hosts"]["k8"]["weeks"]
    assert [
        (w["week"], w["start"], w["orders_closed"], w["partial"]) for w in weeks
    ] == [
        ("2026-W53", "2026-12-28", 1, False),
        ("2027-W01", "2027-01-04", 1, True),
    ]


async def test_executor_autonomy_surface_parity(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-6 (#1980): REST, CLI and MCP carry the same section."""
    import argparse
    import io
    from contextlib import redirect_stdout
    from unittest.mock import AsyncMock, patch

    from hub import cli, mcp_server

    await _autonomy_week_fixture(db)
    query = "date_from=2026-10-19&date_to=2026-11-01"
    resp = await client.get(f"/api/metrics/practices?{query}")
    assert resp.status_code == 200, resp.text
    rest = resp.json()
    section = rest["executor_autonomy"]
    assert section["hosts"]["k8"]["weeks"][0]["autonomous"] == 2

    buf = io.StringIO()
    with patch.object(cli, "_api", return_value=rest), redirect_stdout(buf):
        rc = cli.cmd_practice_metrics(
            argparse.Namespace(
                since_days=90,
                project="",
                model="",
                date_from="2026-10-19",
                date_to="2026-11-01",
                compare=False,
                series=False,
                series_days=7,
            )
        )
    assert rc == 0
    assert json.loads(buf.getvalue())["executor_autonomy"] == section

    with patch("hub.mcp_server._api_get", new_callable=AsyncMock) as get:
        get.return_value = rest
        result = await mcp_server.hub_practice_metrics(
            date_from="2026-10-19", date_to="2026-11-01"
        )
    assert result.structuredContent["metrics"]["executor_autonomy"] == section


async def test_executor_autonomy_open_start_skips_empty_history(
    db: aiosqlite.Connection,
):
    """#1980: a window open to 1970 starts at the first order, not at 1970."""
    await _autonomy_host(db)
    task = await _task(db, title="open start")
    await _autonomy_order(
        db,
        task,
        closed_at="2026-10-21 10:00:00",
        outcome="submitted",
        close_reason="submitted",
    )
    await db.commit()
    result = await practice_metrics(db, date_to="2026-10-25")
    weeks = result["executor_autonomy"]["hosts"]["k8"]["weeks"]
    assert [w["week"] for w in weeks] == ["2026-W43"]


async def test_executor_autonomy_needs_both_signals_and_rounds(
    db: aiosqlite.Connection,
):
    """#1980: the report AND the closing both say submitted; rate has 3 digits."""
    await _autonomy_host(db)
    task = await _task(db, title="both signals")
    at = "2026-10-21 10:00:00"
    for outcome, reason in (
        ("submitted", "submitted"),
        ("submitted", "reported_rescued"),
        ("", "submitted"),
    ):
        await _autonomy_order(
            db, task, closed_at=at, outcome=outcome, close_reason=reason
        )
    await db.commit()
    week = (await practice_metrics(db, **_AUTONOMY_WINDOW))["executor_autonomy"][
        "hosts"
    ]["k8"]["weeks"][0]
    assert (week["orders_closed"], week["autonomous"], week["rate"]) == (3, 1, 0.333)
    assert week["by_outcome"] == {"submitted": 2, "none": 1}


async def test_executor_autonomy_long_window_starts_at_first_order(
    db: aiosqlite.Connection,
):
    """#1980: a window over 104 weeks starts at the first closed order's week."""
    await _autonomy_host(db)
    task = await _task(db, title="long window")
    await _autonomy_order(
        db,
        task,
        closed_at="2025-01-08 10:00:00",
        outcome="submitted",
        close_reason="submitted",
    )
    await db.commit()
    result = await practice_metrics(db, date_from="2024-01-01", date_to="2026-12-31")
    weeks = result["executor_autonomy"]["hosts"]["k8"]["weeks"]
    assert weeks[0]["week"] == "2025-W02" and weeks[0]["orders_closed"] == 1
    assert len(weeks) == 104  # 2025-W02 .. 2026-W53


async def test_executor_autonomy_future_closed_at_is_in_a_week(
    db: aiosqlite.Connection,
):
    """#1980: a relative window selects a closed_at after now; its week exists."""
    from datetime import UTC, datetime, timedelta

    await _autonomy_host(db)
    task = await _task(db, title="future")
    future = (datetime.now(UTC) + timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
    await _autonomy_order(
        db, task, closed_at=future, outcome="submitted", close_reason="submitted"
    )
    await db.commit()
    weeks = (await practice_metrics(db, since_days=30))["executor_autonomy"]["hosts"][
        "k8"
    ]["weeks"]
    assert sum(w["orders_closed"] for w in weeks) == 1
    assert weeks[-1]["orders_closed"] == 1 and weeks[-1]["partial"] is True


async def test_executor_autonomy_weeks_past_the_window_stay_partial(
    db: aiosqlite.Connection,
):
    """#1980: the grid reaches a future closed_at, partial follows the window."""
    from hub.services.executor_autonomy import executor_autonomy_metrics
    from hub.services.metrics_scope import Scope

    await _autonomy_host(db)
    task = await _task(db, title="past the window")
    await _autonomy_order(
        db,
        task,
        closed_at="2026-11-10 10:00:00",
        outcome="submitted",
        close_reason="submitted",
    )
    await db.commit()
    scope = Scope(start="2026-09-21 12:00:00", window_days=30)  # ends 10-21 12:00
    weeks = (await executor_autonomy_metrics(db, scope))["hosts"]["k8"]["weeks"]
    by_key = {w["week"]: w for w in weeks}
    assert by_key["2026-W43"]["partial"] is True  # the window ends inside it
    for key in ("2026-W44", "2026-W45"):
        assert by_key[key]["partial"] is True and by_key[key]["orders_closed"] == 0
    assert by_key["2026-W46"]["orders_closed"] == 1
    assert by_key["2026-W46"]["partial"] is True
    assert by_key["2026-W40"]["partial"] is False
