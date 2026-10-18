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


async def test_plain_summary_none_keeps_old_idempotency_hash():
    from hub.models import TaskCreate
    from hub.services.task_idempotency import hash_task_create_payload

    old = TaskCreate(title="t")
    payload = old.model_dump(mode="json", exclude={"client_request_id"})
    payload.pop("plain_summary")
    payload.pop("freeze_rationale")
    import hashlib
    import json

    legacy = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert hash_task_create_payload(old) == legacy
    assert hash_task_create_payload(TaskCreate(title="t", plain_summary="x")) != legacy


def test_plain_summary_with_defect_verify_is_written_after_filing(capsys):
    from unittest.mock import MagicMock, patch

    from hub import cli

    api = MagicMock(return_value={"task": {"id": 41}})
    with (
        patch(
            "sys.argv",
            [
                "oc-hub",
                "propose",
                "--title",
                "T",
                "--defect-verify",
                "v",
                "--area",
                "hub/x.py",
                "--plain-summary",
                TEXT,
            ],
        ),
        patch.object(cli, "_api", api),
    ):
        assert cli.main() == 0
    assert api.call_args_list[-1].args == (
        "POST",
        "/api/tasks/41/refine",
        {"plain_summary": TEXT},
    )


async def test_plain_summary_defect_verify_mcp_refines_the_filed_draft():
    with patch("hub.mcp_server._api_post", new_callable=AsyncMock) as post:
        post.return_value = {"task": {"id": 41}, "release_id": None}
        await hub_propose_task(
            "T", "broken", defect_verify="v", affected_areas=["a"], plain_summary=TEXT
        )
    assert post.await_args_list[-1].args == (
        "/api/tasks/41/refine",
        {"plain_summary": TEXT},
    )


def test_plain_summary_fallback_is_one_short_line_without_secrets():
    from hub.plain_summary import plain_summary_view

    noisy = "$ curl -H 'Authorization: Bearer abc123SECRET' x\n" + "строка\n" * 200
    view = plain_summary_view(None, noisy)
    assert "abc123SECRET" not in view["plain_summary_shown"]
    assert len(view["plain_summary_shown"]) <= 300
    assert "\n" not in view["plain_summary_shown"]
    assert plain_summary_view(None, "x" * 900)["plain_summary_shown"].endswith("…")
    assert len(plain_summary_view(None, "x" * 900)["plain_summary_shown"]) == 300
