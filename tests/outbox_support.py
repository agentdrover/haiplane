"""Role-outbox tests use real host credentials and the ordinary role lease."""

import pytest

from hub import repository as repo
from tests import role_orders_support as roles

world = roles.world
keys = roles.keys


@pytest.fixture
async def outbox_world(world):
    task = await roles.task_in_project(world)
    await repo.update_task(world.db, task, human_owner="owner1712")
    await roles.admit(world)
    await roles.enter_needs_decision(world, task)
    assert await roles.sweep(world) == 1
    order = await roles.leased(world)
    response = await roles.started(world, "k8", order["id"], order["lease_id"])
    assert response.status_code == 200
    world.order = order
    world.task = task
    world.path = f"/api/executor-hosts/k8/role-orders/{order['id']}/outbox"
    scope = await world.client.get(world.path, headers=world.k8_auth)
    assert scope.status_code == 200, scope.text
    world.scope = scope.json()
    return world


def payload(world, **values):
    return {
        **world.scope,
        "lease_id": world.order["lease_id"],
        "delivery_key": "op-1",
        "kind": "result",
        "body": "Checked the release",
    } | values


async def publish(world, **values):
    return await world.client.post(
        world.path, json=payload(world, **values), headers=world.k8_auth
    )


async def status(world, delivery_id):
    response = await world.client.get(
        f"{world.path}/{delivery_id}", headers=world.k8_auth
    )
    assert response.status_code == 200, response.text
    return response.json()


async def answer(world, message_id, text="Continue diagnosis", headers=None):
    return await world.client.post(
        f"/api/tasks/{world.task}/outbox-questions/{message_id}/answer",
        json={"body": text},
        headers=world.human_auth if headers is None else headers,
    )


async def messages(world):
    return await repo.list_task_messages(world.db, world.task, exclude_roles=True)
