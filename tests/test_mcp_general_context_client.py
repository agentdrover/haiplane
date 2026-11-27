"""Golden responses and HTTP client lifecycle for the general context (#2425)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import contextmanager, suppress
from types import SimpleNamespace

import anyio
import httpx
import pytest

from hub import hub_instance
from hub import mcp_server as mcp
from hub.mcp_http_compat import MountedMcpRequest, mounted_mcp_request
from hub.mcp_internal_auth import bearer_context_reset, bearer_context_set


IDENTITY = "/api/diagnostics/identity"
CLAIMED = "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id="
HEADLESS = "/api/tasks?claimed_by=fixture&status=review&limit=50"
HEALTH = "/health"
RELEASE = "/api/release-blocks"
POLICY = "/api/projects/{slug}/effective-policy"

CASES = (
    ("empty", "summary", ""),
    ("paged", "full", "lab"),
    ("capped", "summary", "lab"),
)

FAILURES = {
    "diagnostics": IDENTITY,
    "page": CLAIMED + "2",
    "headless": HEADLESS,
    "worktree": "/api/tasks/41",
    "release-http": RELEASE,
    "release-other": RELEASE,
    "health-timeout": HEALTH,
    "policy": POLICY.format(slug="lab"),
}

# Full CallToolResult, canonical JSON, and exact requested routes collected on
# 13925c7b before changing the production code.
BASE_RESPONSES = {
    "empty": {
        "sha256": "d756d7c55a0d508fa0bf9f5d1fd9f6d5324d3af9d16bad8f34e763bb2b413bac",  # pragma: allowlist secret
        "routes": [
            "/api/diagnostics/identity",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=0",
            "/health",
            "/api/release-blocks",
            "/api/projects/default/effective-policy",
        ],
    },
    "paged": {
        "sha256": "f2726d8bf0e271bec076bd7b184b70c10472e218427a3b9ca820832fb2d6416b",  # pragma: allowlist secret
        "routes": [
            "/api/diagnostics/identity",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=0",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=2",
            "/api/tasks?claimed_by=fixture&status=review&limit=50",
            "/api/tasks/41",
            "/api/tasks/42",
            "/api/tasks/43",
            "/health",
            "/api/release-blocks",
            "/api/projects/lab/effective-policy",
        ],
    },
    "capped": {
        "sha256": "c924ff1d10b4a263d376c0d59b4d9878d7258d1b27479c717f6ca299110d2471",  # pragma: allowlist secret
        "routes": [
            "/api/diagnostics/identity",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=0",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=2",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=4",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=6",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=8",
            "/api/tasks?claimed_by=fixture&status=review&limit=50",
            "/api/tasks/41",
            "/api/tasks/42",
            "/api/tasks/43",
            "/health",
            "/api/release-blocks",
            "/api/projects/lab/effective-policy",
        ],
    },
    "diagnostics": {
        "sha256": "f2726d8bf0e271bec076bd7b184b70c10472e218427a3b9ca820832fb2d6416b",  # pragma: allowlist secret
        "routes": [
            "/api/diagnostics/identity",
            "/api/whoami",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=0",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=2",
            "/api/tasks?claimed_by=fixture&status=review&limit=50",
            "/api/tasks/41",
            "/api/tasks/42",
            "/api/tasks/43",
            "/health",
            "/api/release-blocks",
            "/api/projects/lab/effective-policy",
        ],
    },
    "page": {
        "sha256": "099fe8b3d00887630499abff4e83da7385a3370884b19d0d2870aa1f981d9cb1",  # pragma: allowlist secret
        "routes": [
            "/api/diagnostics/identity",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=0",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=2",
            "/api/tasks?claimed_by=fixture&status=review&limit=50",
            "/api/tasks/41",
            "/api/tasks/42",
            "/health",
            "/api/release-blocks",
            "/api/projects/lab/effective-policy",
        ],
    },
    "headless": {
        "sha256": "29330fed197de319c35b735108cbc0081b08bcecda7993651ff71b9b7a495b2b",  # pragma: allowlist secret
        "routes": [
            "/api/diagnostics/identity",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=0",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=2",
            "/api/tasks?claimed_by=fixture&status=review&limit=50",
            "/api/tasks/41",
            "/api/tasks/42",
            "/api/tasks/43",
            "/health",
            "/api/release-blocks",
            "/api/projects/lab/effective-policy",
        ],
    },
    "worktree": {
        "sha256": "02248b2e0326b29d6370be0f6eac1928fe4c11cac3d85fa434e6bc34b4cbbc6e",  # pragma: allowlist secret
        "routes": [
            "/api/diagnostics/identity",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=0",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=2",
            "/api/tasks?claimed_by=fixture&status=review&limit=50",
            "/api/tasks/41",
            "/api/tasks/42",
            "/api/tasks/43",
            "/health",
            "/api/release-blocks",
            "/api/projects/lab/effective-policy",
        ],
    },
    "release-http": {
        "sha256": "fa5c19dcfd2403430b8996da7c21d226d89a4959d508bedebc567c318e8abcad",  # pragma: allowlist secret
        "routes": [
            "/api/diagnostics/identity",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=0",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=2",
            "/api/tasks?claimed_by=fixture&status=review&limit=50",
            "/api/tasks/41",
            "/api/tasks/42",
            "/api/tasks/43",
            "/health",
            "/api/release-blocks",
            "/api/projects/lab/effective-policy",
        ],
    },
    "release-other": {
        "sha256": "9540c0dbdf4c7b1f9f3ca6976cc437b48cd7408e25ee7233831eaa9630edf47a",  # pragma: allowlist secret
        "routes": [
            "/api/diagnostics/identity",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=0",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=2",
            "/api/tasks?claimed_by=fixture&status=review&limit=50",
            "/api/tasks/41",
            "/api/tasks/42",
            "/api/tasks/43",
            "/health",
            "/api/release-blocks",
            "/api/projects/lab/effective-policy",
        ],
    },
    "health-timeout": {
        "sha256": "e0eed7eefc33006851a4fa246aadb038c32dc252a8a45549b6fc82d5ae10c757",  # pragma: allowlist secret
        "routes": [
            "/api/diagnostics/identity",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=0",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=2",
            "/api/tasks?claimed_by=fixture&status=review&limit=50",
            "/api/tasks/41",
            "/api/tasks/42",
            "/api/tasks/43",
            "/health",
            "/api/release-blocks",
            "/api/projects/lab/effective-policy",
        ],
    },
    "policy": {
        "sha256": "12537ed6fc6bc6d1b7cf935233ae98c5e4c8d8f032c14ffb75d0c7fae10edb30",  # pragma: allowlist secret
        "routes": [
            "/api/diagnostics/identity",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=0",
            "/api/tasks?claimed_by=fixture&limit=50&mode=summary&after_id=2",
            "/api/tasks?claimed_by=fixture&status=review&limit=50",
            "/api/tasks/41",
            "/api/tasks/42",
            "/api/tasks/43",
            "/health",
            "/api/release-blocks",
            "/api/projects/lab/effective-policy",
        ],
    },
}


def _check_contract(key, result, requests):
    digest = hashlib.sha256(
        json.dumps(result.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    actual = {"sha256": digest, "routes": [item["route"] for item in requests]}
    assert actual == BASE_RESPONSES.get(key), (key, actual)


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, wire, client_id):
        self.wire = wire
        self.client_id = client_id

    async def handle_async_request(self, request):
        return await self.wire.request(self.client_id, request)

    async def aclose(self):
        # Detect cleanup which is interrupted at the first cancellation checkpoint.
        await anyio.lowlevel.checkpoint()
        self.wire.closed.append(self.client_id)


class _Wire:
    def __init__(self):
        self.scenario = "empty"
        self.failure = ""
        self.before_request = None
        self.created = []
        self.closed = []
        self.requests = []

    def install(self, monkeypatch):
        client_type = httpx.AsyncClient

        def new_client(**kwargs):
            client_id = len(self.created)
            self.created.append(kwargs.copy())
            return client_type(transport=_Transport(self, client_id), **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", new_client)
        monkeypatch.setenv("HAIPLANE_HUB_URL", "https://stdio.example.test")
        monkeypatch.setenv("HAIPLANE_HUB_TOKEN", "fixture-service-token")
        monkeypatch.setattr(hub_instance, "hub_hostname", lambda: "fixture-host")

    async def request(self, client_id, request):
        route = request.url.raw_path.decode()
        self.requests.append(
            {
                "id": client_id,
                "route": route,
                "url": str(request.url),
                "headers": request.headers,
                "timeout": request.extensions["timeout"],
            }
        )
        if self.before_request is not None:
            await self.before_request(request)
        if route == FAILURES.get(self.failure):
            if self.failure == "release-other":
                raise ValueError("fixture release decode failure")
            if self.failure == "page":
                raise httpx.ReadError("fixture interrupted read", request=request)
            return httpx.Response(503, json={"detail": "fixture unavailable"})
        response = self.response(request)
        headers = (
            {"Set-Cookie": "fixture-session=temporary; Path=/"}
            if route == IDENTITY
            else {}
        )
        return httpx.Response(200, json=response, headers=headers)

    def response(self, request):
        route = request.url.raw_path.decode()
        path = request.url.path
        if path in (IDENTITY, "/api/whoami"):
            return {
                "username": "fixture",
                "role": "agent",
                "principal_id": 7,
                "workspace_mode": "worktree",
            }
        if path == "/api/tasks" and request.url.params.get("mode") == "summary":
            cursor = int(request.url.params["after_id"])
            if self.scenario == "empty":
                return {"tasks": [], "next_cursor": None}
            tasks = (
                [
                    {"id": 41, "title": "First", "status": "running"},
                    {"id": 42, "title": "Machine review", "status": "review"},
                ]
                if cursor == 0
                else [{"id": 43, "title": "Owner review", "status": "review"}]
                if cursor == 2
                else []
            )
            next_cursor = (
                cursor + 2 if self.scenario == "capped" or cursor == 0 else None
            )
            return {"tasks": tasks, "next_cursor": next_cursor}
        if route == HEADLESS:
            return [{"id": 42, "review_job_id": "fixture-review"}, {"id": 43}]
        if path in ("/api/tasks/41", "/api/tasks/42", "/api/tasks/43"):
            return {
                "id": int(path.rsplit("/", 1)[1]),
                "worktree_path": "/fixture/worktree",
            }
        if path == HEALTH:
            return {
                "status": "ok",
                "app_version": "fixture",
                "bind_host": "127.0.0.1",
                "bind_port": 8080,
                "auth_required": True,
                "auth_disabled": False,
                "env_tokens_configured": True,
                "vast_enabled": False,
            }
        if path == RELEASE:
            return {"release_blocks": []}
        if path.endswith("/effective-policy"):
            return {
                "slug": path.split("/")[3],
                "keys": [],
                "steward": {"requested": "off", "effective": "off"},
            }
        if path == "/api/tasks/77/context":
            return {
                "task_id": 77,
                "context_text": "Fixture task",
                "path_brief": {"status": "no_project"},
            }
        raise AssertionError(f"Unexpected fixture route: {route}")


@pytest.fixture
def wire(monkeypatch):
    recorder = _Wire()
    recorder.install(monkeypatch)
    return recorder


@contextmanager
def _caller(bearer, *, mounted=False, host="fixture.example.test"):
    auth_token = bearer_context_set(bearer)
    mount_token = mounted_mcp_request.set(
        MountedMcpRequest("http://127.0.0.1:8080", f"https://{host}", host)
        if mounted
        else None
    )
    try:
        yield
    finally:
        mounted_mcp_request.reset(mount_token)
        bearer_context_reset(auth_token)


async def _read_case(wire, case):
    wire.scenario, mode, project = case
    return await mcp.hub_my_context(mode=mode, project=project)


@pytest.mark.parametrize("case", CASES)
async def test_general_context_matches_base_contract(wire, case):
    result = await _read_case(wire, case)
    _check_contract(case[0], result, wire.requests)


@pytest.mark.parametrize("case", CASES)
async def test_general_context_reuses_one_http_client(wire, case):
    result = await _read_case(wire, case)
    _check_contract(case[0], result, wire.requests)
    assert len(wire.created) == 1
    assert wire.closed == [0]
    assert {request["id"] for request in wire.requests} == {0}


async def test_general_context_isolates_mounted_and_stdio_identity(wire, caplog):
    both_started = asyncio.Event()
    arrived = set()

    async def rendezvous(request):
        if request.url.path == IDENTITY:
            arrived.add(request.headers["Host"])
            if len(arrived) == 2:
                both_started.set()
            await both_started.wait()

    wire.before_request = rendezvous
    bearers = {
        "one.example.test": "fixture-first-token",
        "two.example.test": "fixture-second-token",
    }

    async def mounted_context(host, bearer):
        with _caller(bearer, mounted=True, host=host):
            await mcp.hub_my_context()

    await asyncio.gather(
        *(mounted_context(host, bearer) for host, bearer in bearers.items())
    )
    wire.before_request = None
    await mounted_context("anonymous.example.test", "")
    with _caller(""):
        await mcp.hub_my_context()
    with _caller("fixture-standalone-override"):
        await mcp.hub_my_context()

    expected = {
        **{
            host: ("http://127.0.0.1:8080", f"Bearer {token}", False)
            for host, token in bearers.items()
        },
        "anonymous.example.test": ("http://127.0.0.1:8080", None, False),
        "stdio.example.test": (
            "https://stdio.example.test",
            "Bearer fixture-service-token",
            True,
        ),
        "override-standalone": (
            "https://stdio.example.test",
            "Bearer fixture-standalone-override",
            True,
        ),
    }
    observed = {}
    for request in wire.requests:
        headers = request["headers"]
        assert "cookie" not in headers
        host = headers["Host"]
        if headers.get("Authorization") == "Bearer fixture-standalone-override":
            host = "override-standalone"
        url, token, trust = expected[host]
        assert request["url"].startswith(url + "/")
        assert headers.get("Authorization") == token
        assert wire.created[request["id"]].get("trust_env", True) is trust
        observed.setdefault(host, set()).add(request["id"])

    assert set(observed) == set(expected)
    assert all(len(ids) == 1 for ids in observed.values())
    assert len(set.union(*observed.values())) == len(expected)
    assert sorted(wire.closed) == list(range(len(expected)))
    assert all(
        not token or token.removeprefix("Bearer ") not in caplog.text
        for _, token, _ in expected.values()
    )


def _interrupt_health(monkeypatch, wire):
    entered = asyncio.Event()

    async def hang_health(request):
        if request.url.path == HEALTH:
            entered.set()
            await asyncio.Event().wait()

    async def wait_for_health(coro, timeout):
        assert timeout == 2.0
        task = asyncio.create_task(coro)
        await entered.wait()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        raise TimeoutError

    wire.before_request = hang_health
    monkeypatch.setattr(mcp, "asyncio", SimpleNamespace(wait_for=wait_for_health))


async def _check_external_cancel(wire):
    cancelled = False
    with anyio.CancelScope() as scope:

        async def cancel_during_get(request):
            assert request.url.path == IDENTITY
            scope.cancel()
            await anyio.lowlevel.checkpoint()

        wire.before_request = cancel_during_get
        try:
            await mcp.hub_my_context()
        except anyio.get_cancelled_exc_class():
            cancelled = True
            raise

    assert cancelled and scope.cancelled_caught
    assert wire.closed == [0]
    wire.before_request = None
    await mcp.hub_my_context()
    assert wire.closed == [0, 1]


@pytest.mark.parametrize("failure", (*FAILURES, "cancel"))
async def test_general_context_preserves_fallbacks_and_closes_on_cancel(
    wire, monkeypatch, failure
):
    if failure == "cancel":
        await _check_external_cancel(wire)
        return
    wire.scenario = "paged"
    if failure == "health-timeout":
        _interrupt_health(monkeypatch, wire)
    else:
        wire.failure = failure
    result = await mcp.hub_my_context(project="lab", mode="full")
    _check_contract(failure, result, wire.requests)
    assert len(wire.created) == 1 and wire.closed == [0]
    assert {row["id"] for row in wire.requests} == {0}
    assert all(set(row["timeout"].values()) == {15.0} for row in wire.requests)


async def test_general_context_leaves_task_and_other_calls_isolated(wire):
    waiting = asyncio.Event()
    released = asyncio.Event()

    async def pause_identity(request):
        if request.url.path == IDENTITY:
            waiting.set()
            await released.wait()

    wire.before_request = pause_identity
    running = asyncio.create_task(mcp.hub_my_context())
    try:
        await waiting.wait()
        task_output = await mcp.hub_my_context(task_id=77)
        await mcp.hub_health()
        assert "Fixture task" in task_output.content[0].text
        assert wire.closed == [1, 2]
    finally:
        released.set()
        await running

    # A general call in this task must also release its binding before later calls.
    await mcp.hub_my_context()
    await mcp.hub_my_context(task_id=77)
    await mcp.hub_health()
    assert wire.closed == [1, 2, 0, 3, 4, 5]
    assert [
        row["id"] for row in wire.requests if row["route"] == "/api/tasks/77/context"
    ] == [1, 4]
