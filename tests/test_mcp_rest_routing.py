"""REST routing for tools served by the mounted MCP transport."""

import httpx
import pytest

from hub import config
from hub.app import app
from hub.mcp_server import _api_get
from tests.test_mcp_server import (
    _MCP_HEADERS,
    _call_tool,
    _hub_process,
    _rpc_result,
    _tool_text,
)


@pytest.mark.parametrize(
    ("bind_host", "server_port", "rest_authority"),
    [
        ("127.0.0.1", None, "127.0.0.1:9071"),
        ("0.0.0.0", 9102, "127.0.0.1:9102"),
        ("::", 9102, "[::1]:9102"),
        ("192.0.2.8", 9102, "192.0.2.8:9102"),
    ],
)
async def test_mounted_mcp_uses_local_listener(
    client, monkeypatch, bind_host, server_port, rest_authority
):
    caller = "agent-test-token"
    monkeypatch.setenv("HAIPLANE_HUB_TOKEN", "service-test-token")
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://public.example")
    monkeypatch.setattr(config, "HUB_HOST", bind_host)
    monkeypatch.setattr(config, "HUB_PORT", 9071)
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(
        config, "HUB_TOKENS", {caller: config.TokenIdentity("test-agent", "agent")}
    )
    client.base_url = f"https://test:{server_port}" if server_port else "https://test"
    client.headers["Host"] = "mcp-alias.example:8443"
    attempts = []
    trusts_env = []
    available = True

    def responder(request):
        attempts.append((request.method, str(request.url)))
        assert request.headers["Host"] == "mcp-alias.example:8443"
        assert request.headers["Authorization"] == f"Bearer {caller}"
        if not available:
            raise httpx.ConnectError("listener unavailable", request=request)
        if request.url.path == "/api/diagnostics/identity":
            return httpx.Response(
                200,
                json={
                    "username": "test-agent",
                    "role": "agent",
                    "auth_source": "env",
                    "instance": "prod",
                    "base_url": "https://public.example",
                    "connected_via": "http://mcp-alias.example:8443",
                    "config_mismatch": True,
                    "app_version": "test",
                },
            )
        if request.method == "POST":
            return httpx.Response(200, json={"id": 7})
        return httpx.Response(200, json={"id": 42, "status": "running"})

    async_client = httpx.AsyncClient

    def intercept(**kwargs):
        trusts_env.append(kwargs.get("trust_env", True))
        return async_client(transport=httpx.MockTransport(responder), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", intercept)
    async with _hub_process():
        read = await _call_tool(client, caller, "hub_admin_my_identity", {})
        written = await _call_tool(
            client,
            caller,
            "hub_task_update",
            {"task_id": 42, "content": "via mounted MCP"},
        )
        available = False
        failed = await _call_tool(client, caller, "hub_admin_my_identity", {})

    assert "Connected via: https://mcp-alias.example:8443" in _tool_text(
        _rpc_result(read)
    )
    assert "CONFIG MISMATCH" in _tool_text(_rpc_result(read))
    assert "Update #7 added" in _tool_text(_rpc_result(written))
    assert "transport_error" in _tool_text(_rpc_result(failed))
    rest = f"http://{rest_authority}"
    assert attempts == [
        ("GET", f"{rest}/api/diagnostics/identity"),
        ("GET", f"{rest}/api/tasks/42"),
        ("POST", f"{rest}/api/tasks/42/updates"),
        ("GET", f"{rest}/api/tasks/42"),
        ("GET", f"{rest}/api/diagnostics/identity"),
    ]
    assert trusts_env == [False] * len(attempts)


async def test_stdio_mcp_uses_configured_remote_rest(monkeypatch):
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://remote.example")
    monkeypatch.setenv("HAIPLANE_HUB_TOKEN", "stdio-test-token")

    def responder(request):
        assert str(request.url) == "https://remote.example/api/whoami"
        assert request.headers["Authorization"] == "Bearer stdio-test-token"
        return httpx.Response(200, json={"username": "stdio"})

    async_client = httpx.AsyncClient

    def intercept(**kwargs):
        assert kwargs.get("trust_env", True) is True
        return async_client(transport=httpx.MockTransport(responder), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", intercept)
    assert await _api_get("/api/whoami") == {"username": "stdio"}


@pytest.mark.parametrize(
    ("credential", "expected_status"), [("watcher", 403), (None, 401)]
)
async def test_mounted_mcp_preserves_caller_permissions(
    client, monkeypatch, credential, expected_status
):
    writer = "service-writer-token"
    monkeypatch.setenv("HAIPLANE_HUB_TOKEN", writer)
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://public.example")
    monkeypatch.setattr(config, "HUB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            writer: config.TokenIdentity("service-writer", "human", principal_id=7001),
            "watcher": config.TokenIdentity(
                "test-watcher", "watcher", principal_id=7002
            ),
        },
    )
    task = await client.post(
        "/api/tasks",
        headers={"Authorization": f"Bearer {writer}"},
        json={"title": "private task"},
    )
    assert task.status_code in (200, 201), task.text
    task_id = task.json()["id"]
    path = f"/api/tasks/{task_id}/updates"
    authorization = {"Authorization": f"Bearer {credential}"} if credential else {}
    direct = await client.post(
        path, headers=authorization, json={"content": "direct test"}
    )
    assert direct.status_code == expected_status, direct.text

    requests = []
    post_statuses = []
    rest = httpx.ASGITransport(app=app, client=("127.0.0.1", 10000))

    async def responder(request):
        requests.append(request)
        # Enable auth on the inner REST hop in both cases. The no-Bearer
        # case lets the outer MCP call through in open mode to test forwarding.
        with monkeypatch.context() as inner:
            inner.setattr(config, "HUB_AUTH_DISABLED", False)
            response = await rest.handle_async_request(request)
        if request.method == "POST":
            post_statuses.append(response.status_code)
        return response

    async_client = httpx.AsyncClient

    def intercept(**kwargs):
        return async_client(transport=httpx.MockTransport(responder), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", intercept)
    if credential is None:
        monkeypatch.setattr(config, "HUB_AUTH_DISABLED", True)
    async with _hub_process():
        response = await client.post(
            "/mcp",
            headers={
                **_MCP_HEADERS,
                **authorization,
                "X-Forwarded-For": "203.0.113.9",
                "X-Forwarded-Host": "untrusted.example",
                "X-Forwarded-Proto": "https",
            },
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "hub_task_update",
                    "arguments": {"task_id": task_id, "content": "MCP test"},
                },
            },
        )
    assert post_statuses == [expected_status]
    expected_reason = (
        "watcher_gate_forbidden" if credential else "authentication required"
    )
    assert expected_reason in _tool_text(_rpc_result(response))
    assert all(r.url.host == "127.0.0.1" for r in requests)
    for request in requests:
        assert request.headers.get("Authorization") == authorization.get(
            "Authorization"
        )
        assert not any(name.startswith("x-forwarded-") for name in request.headers)
