"""Agent API usage surfaces (#780): REST report and dashboard page."""

from __future__ import annotations

import json

from hub import repository as repo
from hub.mcp_catalog import HEADROOM_WARN_PCT


async def _seed(db, tool: str = "hub_task_status", **kwargs) -> None:
    await repo.insert_mcp_call_event(
        db,
        tool=tool,
        profile=kwargs.get("profile", "v1"),
        principal_id=kwargs.get("principal_id", 1),
        principal_role=kwargs.get("principal_role", "agent"),
        status=kwargs.get("status", "ok"),
        error_reason=kwargs.get("error_reason", ""),
        latency_ms=kwargs.get("latency_ms", 12),
        response_chars=kwargs.get("response_chars", 900),
        task_id=kwargs.get("task_id"),
    )
    await db.commit()


async def test_usage_endpoint_reports_popularity_errors_and_cost(client, db):
    await _seed(db)
    await _seed(
        db, tool="hub_claim_task", status="error", error_reason="already_claimed"
    )

    resp = await client.get("/api/metrics/mcp-usage?window_days=30")

    assert resp.status_code == 200
    data = resp.json()
    assert data["window_days"] == 30
    assert data["totals"]["calls"] == 2
    assert data["totals"]["error_calls"] == 1
    tools = {row["tool"]: row for row in data["by_tool"]}
    assert tools["hub_task_status"]["p95_response_chars"] == 900
    assert data["top_errors"][0]["error_reason"] == "already_claimed"
    # The catalog is measured live, so the report can name what nobody called.
    assert data["catalog"]["tools"] > 0
    assert "hub_health" in data["unused_tools"]


async def test_usage_endpoint_clamps_the_window_to_retention(client, db):
    resp = await client.get("/api/metrics/mcp-usage?window_days=3650")
    assert resp.json()["window_days"] == 90


async def test_usage_endpoint_can_skip_the_catalog(client, db):
    resp = await client.get("/api/metrics/mcp-usage?include_catalog=false")
    data = resp.json()
    assert "catalog" not in data
    assert data["unused_tools"] == []


async def test_catalog_endpoint_serves_the_same_check_ci_runs(client, db):
    resp = await client.get("/api/metrics/mcp-catalog")

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["breaches"] == []
    assert data["snapshot"]["tools"] == len(data["tools_list"])


async def test_catalog_endpoint_reports_headroom_spent(client, db):
    """AC-1 (#832): the API must answer the same as CI, not with nulls.

    The check already computed headroom; the endpoint simply never passed the
    recorded baseline, so every metric came back `used_pct: null` while the CI
    log printed real numbers. One check answering differently depending on
    where it is read is worse than one that does not answer at all.
    """
    resp = await client.get("/api/metrics/mcp-catalog")

    assert resp.status_code == 200
    rows = resp.json()["headroom"]
    assert rows, "the check reports headroom for every budgeted metric"
    for row in rows:
        assert row["used_pct"] is not None, row["metric"]
        assert row["measured"] is not None, row["metric"]
        assert row["remaining"] == row["ceiling"] - row["actual"]


async def test_catalog_endpoint_without_measured_says_no_baseline(
    client, db, tmp_path, monkeypatch
):
    """AC-3 (#832): an older budget file must not make the answer invent one."""
    from hub import mcp_catalog

    budget_file = tmp_path / "budget.json"
    budget_file.write_text(
        json.dumps(
            {
                "measured_at": "2026-01-01",
                "budgets": {"tools": 10_000, "description_chars": 10_000_000},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_catalog, "BUDGET_PATH", budget_file)

    resp = await client.get("/api/metrics/mcp-catalog")

    assert resp.status_code == 200
    rows = resp.json()["headroom"]
    assert rows
    assert all(row["used_pct"] is None for row in rows)
    assert all(row["measured"] is None for row in rows)


async def test_agent_api_page_shows_headroom_block(client, db):
    """AC-2 (#832): the numbers live where a human looks, not only in CI."""
    resp = await client.get("/metrics/agent-api")

    assert resp.status_code == 200
    body = resp.text
    assert "Запас под потолком" in body
    assert "Запас съеден" in body
    # The warning threshold is rendered from the module constant, so the page
    # and the CI report can never drift onto two different numbers.
    assert f"{HEADROOM_WARN_PCT}%" in body


async def test_agent_api_page_renders_usage(client, db):
    await _seed(db, response_chars=1234)

    resp = await client.get("/metrics/agent-api?window_days=14")

    assert resp.status_code == 200
    body = resp.text
    assert "Agent API usage" in body
    assert "hub_task_status" in body
    assert "1234" in body


async def test_practice_metrics_page_links_to_the_agent_api_page(client, db):
    resp = await client.get("/metrics")
    assert "/metrics/agent-api" in resp.text
