import json

import pytest

from hub import repository as repo
from hub.integrations.registry import plugins


@pytest.fixture
async def passport_projects(db):
    for slug, policy in (
        ("default", {"review": "dispatch", "executor_launch": "manual"}),
        ("home-infra", {"review": "off"}),
    ):
        if await repo.get_project_by_slug(db, slug) is None:
            await repo.create_project(
                db, slug=slug, name=slug, workspace_path="/srv/PASSPORT-CANARY"
            )
        project = await repo.get_project_by_slug(db, slug)
        await db.execute(
            "UPDATE projects SET gate_policy=? WHERE id=?",
            (json.dumps(policy), project["id"]),
        )
    await db.commit()


async def test_contract_two_projects(client, passport_projects):
    default = (await client.get("/api/projects/default/agent-passport")).json()
    infra = (await client.get("/api/projects/home-infra/agent-passport")).json()
    assert default["schema_version"] == infra["schema_version"] == "1"
    assert [role["role"] for role in default["roles"]] == ["executor", "roadmap"]
    assert [role["role"] for role in infra["roles"]] == ["infrastructure"]
    assert default["pipeline"]["review"]["value"] == "dispatch"
    assert infra["pipeline"]["review"]["value"] == "off"
    assert default["roles"][0]["runtime"]["verified"]["state"] == "unknown"


async def test_untrusted_declaration_fails_safely(
    client, passport_projects, monkeypatch
):
    async def read(*args, **kwargs):
        return {"state": "present", "content": '{"instructions":"PASSPORT-CANARY"}'}

    monkeypatch.setattr(plugins.git_ops, "read_file_at_ref", read)
    result = await client.get("/api/projects/home-infra/agent-passport")
    assert result.status_code == 200
    assert result.json()["declaration"]["state"] == "invalid"
    assert "PASSPORT-CANARY" not in result.text
    bootstrap = await client.get("/api/projects/home-infra/agent-bootstrap")
    assert bootstrap.status_code == 200
    assert bootstrap.json()["agent_passport"]["declaration"]["state"] == "invalid"


async def test_project_isolation_and_secret_redaction(
    client, db, passport_projects, monkeypatch
):
    from hub import config
    from hub.config import TokenIdentity
    from hub.services import admin
    from tests.test_private_project_schema import account, project

    readers = {}
    for name in ("passport-alice", "passport-bob"):
        human = await admin.create_principal(
            db, kind="human", username=name, role_slug="operator"
        )
        key = await admin.create_api_key(db, human["id"], name="passport-test")
        readers[name] = (
            human["id"],
            {"Authorization": f"Bearer {key['plaintext_key']}"},
        )
    alice_id, alice = readers["passport-alice"]
    bob_id, bob = readers["passport-bob"]
    alice_link = await account(db, alice_id, 501)
    bob_link = await account(db, bob_id, 502)
    await db.commit()
    private_a = await project(db, alice_id, alice_link, repo_id=301)
    private_b = await project(db, bob_id, bob_link, repo_id=302)
    slugs = await db.execute_fetchall(
        "SELECT slug FROM projects WHERE id IN (?, ?) ORDER BY id",
        (private_a, private_b),
    )
    a, b = (row["slug"] for row in slugs)
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "project-only": TokenIdentity("reader", role="agent", scopes=("default",)),
            "without-read": TokenIdentity(
                "blind", permissions=frozenset({"tasks.create"})
            ),
        },
    )
    assert (
        await client.get(f"/api/projects/{a}/agent-passport", headers=alice)
    ).status_code == 200
    for prefix, suffix in (
        ("/api/projects", "agent-passport"),
        ("/api/projects", "agent-bootstrap"),
        ("/projects", "agent-passport"),
    ):
        result = await client.get(f"{prefix}/{b}/{suffix}", headers=alice)
        absent = await client.get(f"{prefix}/does-not-exist/{suffix}", headers=alice)
        assert result.status_code == absent.status_code == 404
        assert result.text == absent.text
        restricted = {"Authorization": "Bearer without-read"}
        denied = await client.get(f"{prefix}/{b}/{suffix}", headers=restricted)
        missing = await client.get(
            f"{prefix}/does-not-exist/{suffix}", headers=restricted
        )
        assert denied.status_code == missing.status_code == 404
        assert denied.text == missing.text
        bob_result = await client.get(f"{prefix}/{b}/{suffix}", headers=bob)
        assert bob_result.status_code == 200
        assert "private-projects" not in bob_result.text
    headers = {"Authorization": "Bearer project-only"}
    for suffix in ("agent-passport", "agent-bootstrap"):
        own = await client.get(f"/api/projects/default/{suffix}", headers=headers)
        other = await client.get(f"/api/projects/home-infra/{suffix}", headers=headers)
        assert own.status_code == 200 and other.status_code == 404
        assert "PASSPORT-CANARY" not in own.text


async def test_policy_is_live_and_not_authority(
    client, db, passport_projects, monkeypatch
):
    path = "/api/projects/home-infra/agent-passport"
    before = (await client.get(path)).json()
    assert before["pipeline"]["review"]["value"] == "off"
    await db.execute(
        "UPDATE projects SET gate_policy=? WHERE slug=?",
        (json.dumps({"executor_launch": "manual"}), "home-infra"),
    )
    await db.commit()
    before = (await client.get(path)).json()
    assert before["permissions"]["executor_launch"]["value"] == "manual"
    await db.execute(
        "UPDATE projects SET gate_policy=? WHERE slug=?",
        (json.dumps({"review": "dispatch", "executor_launch": "off"}), "home-infra"),
    )
    await db.commit()
    after = (await client.get(path)).json()
    assert before["policy_version"] != after["policy_version"]
    assert after["pipeline"]["review"]["value"] == "dispatch"
    assert after["pipeline"]["review"]["source"].startswith("effective policy")
    assert after["permissions"]["executor_launch"]["value"] == "off"
    assert (await client.post(path, json=before)).status_code in {404, 405}

    from hub import config
    from hub.config import TokenIdentity
    from hub.services import admin, executor_launch

    human = await admin.create_principal(
        db, kind="human", username="passport-action", role_slug="operator"
    )
    key = await admin.create_api_key(db, human["id"], name="passport-test")
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "HUB_TOKENS", {"unused": TokenIdentity("unused")})
    headers = {"Authorization": f"Bearer {key['plaintext_key']}"}
    launch = await client.post(
        "/api/projects/home-infra/executor-launch", json=before, headers=headers
    )
    assert launch.status_code == 409
    assert launch.json()["detail"]["reason"] == executor_launch.REASON_OFF


async def test_bootstrap_backward_compatibility(client, db, passport_projects):
    from hub.models import AgentBootstrap

    project_id = await repo.create_project(db, slug="no-passport", name="No passport")
    await repo.update_project(
        db, project_id, repo="https://bot:PASSPORT-CANARY@forge.example/org/repo"
    )
    await db.commit()
    response = await client.get("/api/projects/no-passport/agent-bootstrap")
    assert response.status_code == 200
    payload = response.json()
    assert "PASSPORT-CANARY" not in response.text and not payload["repo"]
    assert not payload["agent_passport"]["roles"]
    assert payload["agent_passport"]["declaration"]["state"] == "unreadable"
    passport = payload.pop("agent_passport")
    legacy = AgentBootstrap.model_validate(payload)
    assert legacy.agent_passport is None
    assert legacy.mcp.authorization == "Bearer <ТОКЕН АГЕНТА>"
    for n in range(1, 7):
        assert f"## {n}. " in legacy.text
    assert passport["text"] in legacy.text


async def test_declared_observed_verified_are_distinct(client, db, passport_projects):
    dbproject = await repo.get_project_by_slug(db, "default")
    cursor = await db.execute("INSERT INTO tasks(title) VALUES ('passport witness')")
    await db.execute(
        "INSERT INTO role_orders(role, source_key, task_id, project_id, started_at) "
        "VALUES ('roadmap', 'passport-stale', ?, ?, datetime('now', '-2 days'))",
        (cursor.lastrowid, dbproject["id"]),
    )
    await db.commit()
    path = "/api/projects/default/agent-passport"
    executor, roadmap = (await client.get(path)).json()["roles"]
    assert executor["runtime"]["observed"]["state"] == "unknown"
    assert roadmap["runtime"]["declared"]["value"] == "codex"
    observed = roadmap["runtime"]["observed"]
    assert observed["state"] == "stale" and observed["checked_at"]
    assert observed["source"] == "role_orders.started_at"
    assert "unknown" in observed["value"]
    assert roadmap["runtime"]["verified"]["state"] == "unknown"
    await db.execute(
        "UPDATE role_orders SET started_at=datetime('now') WHERE source_key='passport-stale'"
    )
    await db.commit()
    roadmap = (await client.get(path)).json()["roles"][1]
    assert roadmap["runtime"]["observed"]["state"] == "observed"
    assert roadmap["runtime"]["verified"]["state"] == "unknown"
    infra = (await client.get("/api/projects/home-infra/agent-passport")).json()[
        "roles"
    ][0]
    assert infra["status"] == "planned"
    assert infra["runtime"]["observed"]["state"] == "unknown"


async def test_surfaces_share_projection(
    client, passport_projects, monkeypatch, capsys
):
    import html
    import sys

    from hub import cli, mcp_server
    from hub.agent_passport_contract import AgentPassport

    path = "/api/projects/default/agent-passport"
    payload = (await client.get(path)).json()

    async def get(api_path):
        assert api_path == path
        return payload

    def cli_get(method, api_path):
        assert method == "GET" and api_path == path
        return payload

    monkeypatch.setattr(mcp_server, "_api_get", get)
    mcp_result = await mcp_server.hub_agent_passport("default")
    assert (
        AgentPassport.model_validate(mcp_result.structuredContent).model_dump()
        == payload
    )
    monkeypatch.setattr(cli, "_api", cli_get)
    monkeypatch.setattr(
        sys, "argv", ["hp-hub", "projects", "passport", "default", "--json"]
    )
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out) == payload
    monkeypatch.setattr(sys, "argv", ["hp-hub", "projects", "passport", "default"])
    assert cli.main() == 0
    assert capsys.readouterr().out.rstrip() == payload["text"]
    bootstrap = (await client.get("/api/projects/default/agent-bootstrap")).json()[
        "agent_passport"
    ]
    ui = await client.get(
        "/projects/default/agent-passport", headers={"HX-Request": "true"}
    )
    for key in ("schema_version", "declaration", "policy_version"):
        assert bootstrap[key] == payload[key]
    assert 'data-schema-version="1"' in ui.text
    assert html.escape(payload["policy_version"]) in ui.text
