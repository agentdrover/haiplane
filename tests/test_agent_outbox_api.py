from hub import config, mcp_server
from hub.services import admin as admin_service
from hub.services import agent_outbox_delivery as worker
from tests import outbox_support as fixtures
from tests.outbox_support import messages, payload, publish
from tests.role_orders_support import rows

world = fixtures.world
keys = fixtures.keys
outbox_world = fixtures.outbox_world


async def test_key_conflict_and_principal_project_isolation(outbox_world):
    world = outbox_world
    first = await publish(world)
    assert first.status_code == 200, first.text
    assert (await publish(world, body="Different result")).status_code == 409
    assert (await publish(world, project="default")).status_code == 404
    path = world.path.replace("/k8/", "/k9/")
    assert (
        await world.client.post(path, json=payload(world), headers=world.k9_auth)
    ).status_code == 404
    response = await world.client.get(
        f"{path}/{first.json()['id']}", headers=world.k9_auth
    )
    assert response.status_code == 404
    assert len(await rows(world.db, "SELECT * FROM agent_outbox")) == 1


async def test_secret_canaries_and_safe_errors(outbox_world, caplog):
    world = outbox_world
    canary = "Bearer " + "t" * 24
    malformed = await publish(world, generation=canary)
    assert malformed.status_code == 422 and canary not in malformed.text
    response = await publish(world, body=canary)
    assert response.status_code == 422 and canary not in response.text
    echoed_lease = await publish(world, body=world.order["lease_id"])
    assert (
        echoed_lease.status_code == 422
        and world.order["lease_id"] not in echoed_lease.text
    )
    assert not await rows(world.db, "SELECT * FROM agent_outbox")
    assert not await messages(world)
    assert canary not in caplog.text


async def test_role_question_form_uses_existing_task_messaging(outbox_world):
    world = outbox_world
    response = await publish(
        world,
        kind="question",
        continuation="reply-test",
        body="<script>confirm(123)</script>",
    )
    assert response.status_code == 200
    await worker.sweep(world.db)
    session = await admin_service.create_browser_session(world.db, world.human_id)
    await world.db.commit()
    cookie = {"Cookie": f"{config.HUB_COOKIE_NAME}={session}"}
    page = await world.client.get(f"/tasks/{world.task}", headers=cookie)
    assert page.status_code == 200
    assert "outbox_message_id" in page.text
    assert "<script>confirm(123)</script>" not in page.text


async def test_cli_and_mcp_project_the_same_routes(monkeypatch):
    from hub import cli

    paths = []

    def cli_api(method, path, data=None):
        paths.append((method, path, data))
        return {"state": "pending"}

    async def mcp_get(path):
        return cli_api("GET", path)

    async def mcp_post(path, data):
        return cli_api("POST", path, data)

    monkeypatch.setattr(cli, "_api", cli_api)
    monkeypatch.setattr(mcp_server, "_api_get", mcp_get)
    monkeypatch.setattr(mcp_server, "_api_post", mcp_post)
    args = cli.build_parser().parse_args(["agent-outbox", "status", "k8", "7", "8"])
    assert args.func(args) == 0
    mcp_result = await mcp_server.hub_agent_outbox("k8", 7, delivery_id=8)
    assert mcp_result.structuredContent["delivery"]["state"] == "pending"
    assert paths == [("GET", "/api/executor-hosts/k8/role-orders/7/outbox/8", None)] * 2
    data = {"kind": "silent"}
    await mcp_server.hub_agent_outbox("k8", 7, payload=data)
    assert paths[-1] == ("POST", "/api/executor-hosts/k8/role-orders/7/outbox", data)
