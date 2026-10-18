"""plain_summary (#1811): поле «о чём задача простым языком» на всех поверхностях."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from httpx import AsyncClient

from tests.cli_support import _run_main
from hub.mcp_server import hub_propose_task, hub_refine_task
from hub.services.dor_snapshot import load_dor_snapshot

TEXT = "Показываем на карточке, о чём задача, обычными словами."
LONG = "я" * 301


async def test_plain_summary_contract_all_surfaces(client: AsyncClient, db):
    # REST: создание, чтение, PATCH, bulk.
    created = await client.post(
        "/api/tasks", json={"title": "t", "plain_summary": TEXT}
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["id"]
    assert (await client.get(f"/api/tasks/{task_id}")).json()["plain_summary"] == TEXT

    before = (await load_dor_snapshot(db, task_id)).fingerprint()
    resp = await client.post(
        f"/api/tasks/{task_id}/refine", json={"plain_summary": "Другой текст."}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["plain_summary"] == "Другой текст."
    after = (await load_dor_snapshot(db, task_id)).fingerprint()
    assert before != after

    bulk = await client.post(
        "/api/tasks/refine-bulk",
        json={"items": [{"task_id": task_id, "plain_summary": "Из bulk."}]},
    )
    assert bulk.status_code == 200, bulk.text
    assert (await client.get(f"/api/tasks/{task_id}")).json()["plain_summary"] == (
        "Из bulk."
    )

    # Верхняя граница 300 символов: 422 на каждом пути записи.
    assert (
        await client.post("/api/tasks", json={"title": "t", "plain_summary": LONG})
    ).status_code == 422
    assert (
        await client.post(f"/api/tasks/{task_id}/refine", json={"plain_summary": LONG})
    ).status_code == 422
    assert (
        await client.post(
            "/api/tasks/refine-bulk",
            json={"items": [{"task_id": task_id, "plain_summary": LONG}]},
        )
    ).status_code == 422

    # CLI.
    rc, api = _run_main(["refine", "7", "--plain-summary", TEXT])
    assert rc == 0
    assert api.call_args.args[2]["plain_summary"] == TEXT
    rc, api = _run_main(
        ["propose", "--title", "T", "--plain-summary", TEXT], api_result={"id": 9}
    )
    assert rc == 0
    assert api.call_args.args[2]["plain_summary"] == TEXT

    # MCP: предложение и уточнение.
    with patch("hub.mcp_server._api_post", new_callable=AsyncMock) as post:
        post.return_value = {"id": 5}
        await hub_propose_task("T", "D", plain_summary=TEXT)
        assert post.await_args.args[1]["plain_summary"] == TEXT
        post.return_value = {"id": 5, "readiness_score": 50, "dor_passed": False}
        await hub_refine_task(5, plain_summary=TEXT)
        assert post.await_args.args[1] == {"plain_summary": TEXT}


async def test_plain_summary_empty_falls_back_to_problem_statement(
    client: AsyncClient,
):
    created = await client.post(
        "/api/tasks", json={"title": "t", "problem_statement": "Проблема."}
    )
    view = created.json()
    assert view["plain_summary"] is None
    assert view["plain_summary_shown"] == "Проблема."
    assert view["plain_summary_source"] == "problem_statement"
    refined = await client.post(
        f"/api/tasks/{view['id']}/refine", json={"plain_summary": TEXT}
    )
    assert refined.json()["plain_summary_shown"] == TEXT
    assert refined.json()["plain_summary_source"] == "plain_summary"
