"""plain_summary (#1811): поле «о чём задача простым языком» на всех поверхностях."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
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


async def test_plain_summary_rides_the_prod_defect_request(client: AsyncClient):
    body = {
        "title": "T",
        "broken": "сломалось",
        "verify": "uv run pytest",
        "affected_areas": ["hub/x.py"],
        "plain_summary": TEXT,
    }
    ok = await client.post("/api/prod-defects", json=body)
    assert ok.status_code == 200, ok.text
    assert ok.json()["task"]["plain_summary"] == TEXT
    before = len((await client.get("/api/tasks")).json())
    too_long = await client.post(
        "/api/prod-defects", json={**body, "plain_summary": LONG}
    )
    assert too_long.status_code == 422
    assert len((await client.get("/api/tasks")).json()) == before


def test_plain_summary_with_defect_verify_goes_in_the_one_request():
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
    assert api.call_count == 1
    assert api.call_args.args[1] == "/api/prod-defects"
    assert api.call_args.args[2]["plain_summary"] == TEXT


async def test_plain_summary_defect_verify_mcp_one_request_and_300_cap():
    from hub.mcp_server import mcp

    with patch("hub.mcp_server._api_post", new_callable=AsyncMock) as post:
        post.return_value = {"task": {"id": 41}, "release_id": None}
        await hub_propose_task(
            "T", "broken", defect_verify="v", affected_areas=["a"], plain_summary=TEXT
        )
        assert post.await_count == 1
        assert post.await_args.args[1]["plain_summary"] == TEXT
        post.reset_mock()
        args = {"title": "T", "description": "d", "plain_summary": LONG}
        try:
            result = await mcp.call_tool("hub_propose_task", args)
            refused = bool(getattr(result, "isError", False))
        except Exception:
            refused = True
        assert refused
        post.assert_not_awaited()


@pytest.mark.parametrize(
    "text",
    [
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx",
        "sk-abcdefghijklmnopqrstuv вставлен в запрос.",
        "Ключ sk-abc123 в запросе.",
        "Токен ghp_abc123 в логе.",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789 в логе.",
        "gho_abcdefghijklmnopqrstuvwxyz0123456789 в логе.",
        "github_pat_11ABCDEFG0abcdefghijkl в логе.",
        "xoxb-1234-5678-abcdef в логе.",
        "AKIAIOSFODNN7EXAMPLE в логе.",
        "Authorization: Bearer abc123SECRET в запросе.",
        "PASSWORD=hunter2 в конфиге.",
        "Хеш 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08 не совпал.",
        "print(123)",
        "Падает `x = 1` на старте.",
        'Падает {"a": 1} на старте.',
        "x = 1",
        "Первая строка\nвторая строка.",
        "а" * 301,
    ],
)
def test_plain_summary_fallback_refuses_secrets_and_code(text):
    from hub.plain_summary import plain_summary_view

    view = plain_summary_view(None, text)
    assert view == {"plain_summary_shown": "", "plain_summary_source": "none"}


def test_plain_summary_fallback_takes_the_first_clean_sentence():
    from hub.plain_summary import plain_summary_view

    view = plain_summary_view(None, "Карточка не показывает суть. Второе предложение.")
    assert view == {
        "plain_summary_shown": "Карточка не показывает суть.",
        "plain_summary_source": "problem_statement",
    }
    assert plain_summary_view(None, None)["plain_summary_source"] == "none"
