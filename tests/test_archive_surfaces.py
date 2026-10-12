"""CLI и MCP несут причину архива до REST (#1673): сами правила — в test_api."""

from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from hub import cli


def _archive_body(*argv: str) -> dict:
    api = MagicMock(return_value={"id": 5})
    with patch.object(sys, "argv", ["oc-hub", "archive", "5", *argv]):
        with patch.object(cli, "_api", api):
            assert cli.main() == 0
    method, path, body = api.call_args.args
    assert (method, path) == ("POST", "/api/tasks/5/archive")
    return body


def test_cli_archive_sends_the_reason_only_when_given() -> None:
    assert _archive_body("--reason", "эпик закрыт") == {
        "cascade": True,
        "reason": "эпик закрыт",
    }
    assert _archive_body("--no-cascade", "--reason", "x")["cascade"] is False
    assert _archive_body() == {"cascade": True}


async def test_mcp_archive_passes_the_reason_and_shows_the_refusal() -> None:
    from hub.mcp_server import HubApiError, hub_archive_task

    post = AsyncMock(return_value={"id": 5, "status": "open"})
    with (
        patch("hub.mcp_server._api_get", AsyncMock(return_value={"id": 5})),
        patch("hub.mcp_server._api_post", post),
    ):
        await hub_archive_task(5, cascade=False, reason="владелец")
    assert post.await_args.args == (
        "/api/tasks/5/archive",
        {"cascade": False, "reason": "владелец"},
    )

    refusal = {
        "reason": "archive_active_work",
        "message": "архив затронет работу в ходу: #5",
        "blocking": [{"task_id": 5, "status": "running"}],
        "actor_hint": "human",
    }
    post = AsyncMock(side_effect=HubApiError(refusal))
    with (
        patch("hub.mcp_server._api_get", AsyncMock(return_value={"id": 5})),
        patch("hub.mcp_server._api_post", post),
    ):
        payload = json.loads(await hub_archive_task(5))
    assert payload["reason"] == "archive_active_work"
    assert payload["blocking"][0]["task_id"] == 5
