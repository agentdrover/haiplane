"""HTTP regression coverage for the one-shot k8 receipt mandate."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from hub import repository as repo
from hub.integrations.registry import plugins
from hub.services import admin, network_receipt
from tests import role_orders_support as support
from tests.test_roadmap_activation import approved as approval_fixture

approved = approval_fixture
world = support.world
keys = support.keys
_clean_limiter = support._clean_limiter
_no_cloud = support._no_cloud

_HOST = "/api/executor-hosts/k8/network-receipt/2413"
_TASK = "/api/tasks/2413/network-receipt"
_HEAD = "a" * 40
_COMMIT = "b" * 40
_PROFILE = "c" * 64


async def _key(world, principal_id):
    key = await admin.create_api_key(
        world.db, principal_id, name="receipt test", projects=[world.slug]
    )
    return {"Authorization": f"Bearer {key['plaintext_key']}"}


@pytest.fixture(autouse=True)
def _receipt_task_type(monkeypatch):
    make_task = support.task_in_project

    async def make_chore(world, *args, **kwargs):
        task_id = await make_task(world, *args, **kwargs)
        await repo.update_task(world.db, task_id, work_type="chore")
        return task_id

    monkeypatch.setattr(support, "task_in_project", make_chore)


@pytest.fixture
async def pilot(approved, monkeypatch, _no_cloud):
    world, mandate, owner = approved
    await repo.update_project(world.db, world.pid, repo="agentdrover/home-infra")
    await world.db.commit()
    monkeypatch.setattr(network_receipt, "MANDATE", mandate)
    monkeypatch.setattr(network_receipt, "TASK_ID", mandate.task_id)
    monkeypatch.setattr(
        network_receipt, "BRANCH", f"task-{mandate.task_id}/k8-network-receipt"
    )
    monkeypatch.setattr(network_receipt, "PROFILE_SHA256", _PROFILE)
    dispatch = AsyncMock(side_effect=AssertionError("pilot dispatched a model"))
    monkeypatch.setattr(plugins.dispatch, "submit_task", dispatch)
    world.steward_auth = await _key(world, mandate.steward_id)
    other_steward = await admin.create_principal(
        world.db,
        kind="agent",
        username="other-pilot-steward",
        role_slug="roadmap_steward",
    )
    world.other_steward_auth = await _key(world, other_steward["id"])
    yield world, mandate, owner
    dispatch.assert_not_awaited()
    assert not _no_cloud


async def _ready(world, profile=_PROFILE, *, auth=None):
    return await world.client.post(
        f"{_HOST}/ready",
        headers=world.k8_auth if auth is None else auth,
        json={"profile_sha256": profile},
    )


async def _issue(world, *, auth=None, payload=None):
    return await world.client.post(
        f"{_TASK}/issue",
        headers=world.steward_auth if auth is None else auth,
        json={} if payload is None else payload,
    )


async def _order(world, *, auth=None):
    return await world.client.get(
        f"{_HOST}/order", headers=world.k8_auth if auth is None else auth
    )


def _receipt(order):
    return order | {
        "head_sha": _HEAD,
        "repository": "agentdrover/home-infra",
        "ref": "refs/heads/main",
    }


async def _post_receipt(world, payload, *, auth=None):
    return await world.client.post(
        f"{_HOST}/orders/{payload['order_id']}/receipt",
        headers=world.k8_auth if auth is None else auth,
        json=payload,
    )


async def test_repeat_returns_same_order(pilot):
    world, mandate, _ = pilot
    assert (await _order(world)).status_code == 204
    assert (await _issue(world)).status_code == 204
    readiness = await _ready(world)
    assert readiness.status_code == 200, readiness.text
    assert readiness.json() == {"ready": True}

    first, replay = await asyncio.gather(_issue(world), _issue(world))
    assert first.status_code == replay.status_code == 200, (first.text, replay.text)
    order = first.json()
    assert replay.json() == order
    assert set(order) == {"task_id", "order_id", "receipt_id"}
    assert order["task_id"] == mandate.task_id
    assert isinstance(order["order_id"], int) and order["order_id"] > 0

    rows_before = await support.rows(world.db, "SELECT * FROM network_receipt_orders")
    get_first, get_again = await asyncio.gather(_order(world), _order(world))
    assert get_first.status_code == get_again.status_code == 200
    assert get_first.json() == get_again.json() == order
    assert (
        await support.rows(world.db, "SELECT * FROM network_receipt_orders")
        == rows_before
    )

    payload = _receipt(order)
    first, replay = await asyncio.gather(
        _post_receipt(world, payload), _post_receipt(world, payload)
    )
    assert first.status_code == replay.status_code == 200, (first.text, replay.text)
    assert {first.json()["repeat"], replay.json()["repeat"]} == {True, False}
    assert (
        first.json().keys() == replay.json().keys() == (order | {"repeat": None}).keys()
    )
    assert (
        await _post_receipt(world, payload | {"head_sha": _COMMIT})
    ).status_code == 409
    duplicate = await _post_receipt(world, payload)
    assert duplicate.json() == order | {"repeat": True}

    # The approved physical receipt never enters the ordinary slot/model queues.
    ordinary = await world.client.get(
        "/api/executor-hosts/k8/orders?wait=0", headers=world.k8_auth
    )
    assert ordinary.status_code == 200 and ordinary.json()["orders"] == []
    for table in ("executor_orders", "executor_runs", "role_orders"):
        rows = await support.rows(world.db, f"SELECT count(*) AS n FROM {table}")  # nosec B608
        assert rows == [{"n": 0}]


async def test_mismatch_refused(pilot):
    world, _, owner = pilot
    assert (await _ready(world, auth=world.k9_auth)).status_code == 403
    assert (await _order(world, auth=world.k9_auth)).status_code == 403
    assert (await _issue(world, auth=owner)).status_code == 403
    assert (await _issue(world, auth=world.other_steward_auth)).status_code == 403
    assert (await _ready(world, "d" * 64)).status_code == 409
    assert (await _issue(world)).status_code == 204
    assert (await _ready(world)).status_code == 200
    order = (await _issue(world)).json()
    assert (
        await _post_receipt(world, _receipt(order), auth=world.k9_auth)
    ).status_code == 403
    tampered = _receipt(order) | {"receipt_id": "0" * 32}
    assert (await _post_receipt(world, tampered)).status_code == 409
    assert (await _post_receipt(world, _receipt(order))).status_code == 200


@pytest.mark.parametrize("changed", ["generation", "approval_event", "fingerprint"])
async def test_changed_approval_refuses_issuance(pilot, monkeypatch, changed):
    world, mandate, _ = pilot
    assert (await _ready(world)).status_code == 200
    if changed == "generation":
        await repo.update_task(
            world.db, mandate.task_id, statement_generation=mandate.generation + 1
        )
    elif changed == "approval_event":
        monkeypatch.setattr(
            network_receipt,
            "MANDATE",
            replace(mandate, approval_id=mandate.approval_id + 100),
        )
    else:
        monkeypatch.setattr(
            network_receipt, "MANDATE", replace(mandate, fingerprint="e" * 64)
        )
    refusal = await _issue(world)
    assert refusal.status_code == 409, refusal.text


async def test_admin_revoke_fences_existing_order_and_receipt(pilot):
    world, _, owner = pilot
    assert (await _ready(world)).status_code == 200
    order = (await _issue(world)).json()
    route = f"{_TASK}/mandate"
    non_admin = await world.client.delete(route, headers=world.steward_auth)
    assert non_admin.status_code == 403
    revoked = await world.client.delete(route, headers=owner)
    assert revoked.status_code == 204, revoked.text
    assert (await _order(world)).status_code == 409
    assert (await _ready(world)).status_code == 409
    assert (await _issue(world)).status_code == 409
    assert (await _post_receipt(world, _receipt(order))).status_code == 409
    assert (
        await world.client.get(f"{_HOST}/publication", headers=world.k8_auth)
    ).status_code == 409
    publication = f"{_HOST}/orders/{order['order_id']}/publication"
    assert (
        await world.client.post(
            publication, headers=world.k8_auth, json={"commit_sha": _COMMIT}
        )
    ).status_code == 409


async def test_publication_uses_only_the_immutable_checkpoint(pilot):
    world, mandate, _ = pilot
    assert (await _ready(world)).status_code == 200
    response = await _issue(world)
    assert response.status_code == 200, response.text
    order = response.json()
    feed = f"{_HOST}/publication"
    publication = f"{_HOST}/orders/{order['order_id']}/publication"
    assert (await world.client.get(feed, headers=world.k8_auth)).status_code == 204
    assert (await world.client.get(feed, headers=world.k9_auth)).status_code == 403
    premature = await world.client.post(
        publication, headers=world.k8_auth, json={"commit_sha": _COMMIT}
    )
    assert premature.status_code == 409, premature.text

    payload = _receipt(order)
    assert (await _post_receipt(world, payload)).status_code == 200
    ready = await world.client.get(feed, headers=world.k8_auth)
    assert ready.status_code == 200, ready.text
    assert ready.json() == payload

    async def post(sha, auth=world.k8_auth):
        return await world.client.post(
            publication, headers=auth, json={"commit_sha": sha}
        )

    assert (await post(_COMMIT, world.k9_auth)).status_code == 403
    first, replay = await asyncio.gather(post(_COMMIT), post(_COMMIT))
    assert first.status_code == replay.status_code == 200, (first.text, replay.text)
    for response in (first, replay):
        assert response.json()["order_id"] == order["order_id"]
        assert response.json()["commit_sha"] == _COMMIT
    assert (await post("e" * 40)).status_code == 409
    task = await repo.get_task(world.db, mandate.task_id)
    assert task is not None
    assert task["submission_generation"] == 0 and not task["submission_sha"]
    assert task["status"] != "completed"


async def test_stale_readiness_does_not_issue_but_old_order_replays(pilot):
    world, mandate, _ = pilot
    assert (await _issue(world, payload={"target": "cloud"})).status_code == 422
    assert (await _ready(world)).status_code == 200

    async def expire_readiness():
        await world.db.execute(
            "UPDATE network_receipt_mandates SET ready_at=datetime('now','-4 minutes') "
            "WHERE approval_id=?",
            (mandate.approval_id,),
        )

    await expire_readiness()
    assert (await _issue(world)).status_code == 204
    assert not await support.rows(world.db, "SELECT * FROM network_receipt_orders")
    assert (await _ready(world)).status_code == 200
    issued = await _issue(world)
    assert issued.status_code == 200, issued.text
    await expire_readiness()
    replay = await _issue(world)
    assert replay.status_code == 200 and replay.json() == issued.json()


async def test_concurrent_different_commit_candidates_have_one_winner(pilot):
    world, _, _ = pilot
    assert (await _ready(world)).status_code == 200
    issued = await _issue(world)
    assert issued.status_code == 200, issued.text
    order = issued.json()
    assert (await _post_receipt(world, _receipt(order))).status_code == 200
    route = f"{_HOST}/orders/{order['order_id']}/publication"
    responses = await asyncio.gather(
        world.client.post(route, headers=world.k8_auth, json={"commit_sha": _COMMIT}),
        world.client.post(route, headers=world.k8_auth, json={"commit_sha": "e" * 40}),
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response for response in responses if response.status_code == 200)
    rows = await support.rows(
        world.db,
        "SELECT commit_sha FROM network_receipt_orders WHERE id=?",
        (order["order_id"],),
    )
    assert rows == [{"commit_sha": winner.json()["commit_sha"]}]


async def test_paid_executor_cannot_take_approved_pilot_before_issuance(pilot):
    from hub.services import executor_launch, executor_orders

    world, mandate, _ = pilot
    task = dict(await repo.get_task(world.db, mandate.task_id))
    assert "служебным исполнителем" in await executor_launch._live_run(
        world.db, mandate.task_id
    )
    assert "служебным исполнителем" in await executor_orders._task_refusal(
        world.db, task
    )
    assert not await support.rows(world.db, "SELECT * FROM network_receipt_orders")
