"""CLI: admin-команды, проекты и ключи."""

from __future__ import annotations

import argparse
import json
from io import StringIO
from unittest.mock import MagicMock, patch

from hub import cli
from tests.cli_support import _run_main


def test_cmd_projects_create() -> None:
    mock_api = MagicMock(return_value={"id": 2, "slug": "calc-kids"})
    args = argparse.Namespace(
        slug="calc-kids",
        name="Calc Kids",
        repo="mrPDA/calc-kids",
        workspace_path="/srv/calc",
        default_branch="develop",
        forge="github",
    )
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_projects_create(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/projects",
        {
            "slug": "calc-kids",
            "name": "Calc Kids",
            "repo": "mrPDA/calc-kids",
            "workspace_path": "/srv/calc",
            "default_branch": "develop",
            # #1114: контракт публикуется трижды — REST, CLI, MCP. Поверхность,
            # где форж задать нельзя, тихо заводит проект на github.
            "forge": "github",
        },
    )


def test_main_admin_commands(capsys) -> None:
    rc, api = _run_main(["admin", "bootstrap", "--username", "root"])
    assert rc == 1
    api.assert_not_called()
    assert "Password is required" in capsys.readouterr().err

    with patch("getpass.getpass", return_value="secret"):
        rc, api = _run_main(
            [
                "admin",
                "bootstrap",
                "--username",
                "root",
                "--password-prompt",
                "--display-name",
                "Root",
            ],
            api_result={"id": 1},
        )
    assert rc == 0
    assert api.call_args.args[2]["password"] == "secret"  # pragma: allowlist secret
    assert "Admin user 'root' created" in capsys.readouterr().out

    rc, api = _run_main(
        ["admin", "users", "list"],
        api_result=[
            {
                "id": 2,
                "status": "active",
                "username": "ada",
                "display_name": "Ada",
                "roles": ["operator"],
            }
        ],
    )
    assert rc == 0
    assert "ada" in capsys.readouterr().out

    with patch("getpass.getpass", return_value="pw"):
        rc, api = _run_main(
            [
                "admin",
                "users",
                "create",
                "--username",
                "ada",
                "--password-prompt",
                "--role",
                "operator",
            ],
            api_result={"id": 2},
        )
    assert rc == 0
    assert api.call_args.args[2]["password"] == "pw"  # pragma: allowlist secret

    rc, api = _run_main(
        ["admin", "users", "disable", "missing"],
        api_result=[{"id": 2, "username": "ada"}],
    )
    assert rc == 1
    assert "not found" in capsys.readouterr().err

    rc, api = _run_main(
        ["admin", "users", "disable", "ada"],
        api_side_effect=[[{"id": 2, "username": "ada"}], {}],
    )
    assert rc == 0
    assert api.call_args_list[1].args[:2] == ("POST", "/api/admin/principals/2/disable")

    rc, api = _run_main(
        ["admin", "agents", "create", "--name", "bot"],
        api_result={"id": 9},
    )
    assert rc == 0
    assert api.call_args.args[2]["kind"] == "agent"

    rc, api = _run_main(
        ["admin", "keys", "create", "--principal", "missing", "--name", "k"],
        api_result=[],
    )
    assert rc == 1
    assert "not found" in capsys.readouterr().err

    rc, api = _run_main(
        [
            "admin",
            "keys",
            "create",
            "--principal",
            "ada",
            "--name",
            "ci",
            "--expires-days",
            "7",
        ],
        api_side_effect=[
            [{"id": 2, "username": "ada"}],
            {"id": 4, "key_prefix": "och_", "plaintext_key": "secret-key"},
        ],
    )
    assert rc == 0
    assert "secret-key" in capsys.readouterr().out

    rc, api = _run_main(["admin", "keys", "revoke", "4"], api_result={})
    assert rc == 0
    assert api.call_args.args[:2] == ("POST", "/api/admin/api-keys/4/revoke")

    rc, api = _run_main(
        ["admin", "roles", "list"],
        api_result=[
            {
                "slug": "agent",
                "name": "Agent",
                "system": True,
                "permissions": ["tasks.read"],
            }
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "[system]" in out
    assert "tasks.read" in out

    rc, api = _run_main(
        ["admin", "audit", "--limit", "3"],
        api_result=[
            {
                "created_at": "t",
                "actor_username": "ada",
                "action": "disable",
                "target_type": "principal",
                "target_id": "2",
                "summary": "disabled",
            }
        ],
    )
    assert rc == 0
    assert api.call_args.args[1] == "/api/admin/audit?limit=3"
    assert "disabled" in capsys.readouterr().out


async def test_admin_agents_create_with_watcher_role(client, db, monkeypatch):
    """AC-4 (#1556): --role watcher reaches the API; only a human admin can do it."""
    from hub import config
    from hub.services import admin as admin_svc

    # CLI side: the flag names the role in the request body, default stays agent.
    rc, api = _run_main(
        ["admin", "agents", "create", "--name", "w1", "--role", "watcher"],
        api_result={"id": 9},
    )
    assert rc == 0
    assert api.call_args.args[2]["role"] == "watcher"
    assert api.call_args.args[2]["kind"] == "agent"
    rc, api = _run_main(["admin", "agents", "create", "--name", "w2"], api_result={})
    assert rc == 0 and api.call_args.args[2]["role"] == "agent"
    rc, _ = _run_main(["admin", "agents", "create", "--name", "w3", "--role", "admin"])
    assert rc != 0  # a role that is not agent/watcher is not offered here

    # Server side: the same body, sent by a human admin and by an agent.
    monkeypatch.setattr(
        config, "HUB_TOKENS", {"unused-env-token": config.TokenIdentity("x", "human")}
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    admin = await admin_svc.create_principal(
        db, kind="human", username="boss", role_slug="super_admin"
    )
    admin_key = await admin_svc.create_api_key(db, admin["id"], name="k")
    agent = await admin_svc.create_principal(
        db, kind="agent", username="ordinary", role_slug="agent"
    )
    agent_key = await admin_svc.create_api_key(db, agent["id"], name="k")
    body = {"kind": "agent", "username": "w1", "display_name": "w1", "role": "watcher"}

    denied = await client.post(
        "/api/admin/principals",
        json=body,
        headers={"Authorization": f"Bearer {agent_key['plaintext_key']}"},
    )
    assert denied.status_code == 403, denied.text
    assert not await db.execute_fetchall(
        "SELECT 1 FROM principals WHERE username = 'w1'"
    )

    made = await client.post(
        "/api/admin/principals",
        json=body,
        headers={"Authorization": f"Bearer {admin_key['plaintext_key']}"},
    )
    assert made.status_code in (200, 201), made.text
    assert made.json()["roles"] == ["watcher"]
    role = await db.execute_fetchall(
        "SELECT r.system, GROUP_CONCAT(rp.permission) AS perms FROM roles r "
        "JOIN role_permissions rp ON rp.role_id = r.id WHERE r.slug = 'watcher'"
    )
    assert role[0]["system"] == 1 and role[0]["perms"] == "tasks.read"


def test_projects_bootstrap_prints_server_text(capsys) -> None:
    """AC-5: без флага — text сервера как есть; --json — JSON; нет проекта — rc≠0."""
    payload = {
        "project_slug": "demo",
        "text": "# Стартовый пакет demo\n\nстрока  с  пробелами",
    }

    rc, api = _run_main(["projects", "bootstrap", "demo"], api_result=payload)
    out = capsys.readouterr().out
    assert rc == 0
    assert api.call_args.args == ("GET", "/api/projects/demo/agent-bootstrap")
    assert out == payload["text"] + "\n"

    rc, _ = _run_main(["projects", "bootstrap", "demo", "--json"], api_result=payload)
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == payload

    rc, _ = _run_main(["projects", "bootstrap", "nope"], api_side_effect=SystemExit(1))
    assert rc == 1


def test_admin_keys_create_sends_project_scope():
    """#1644: ``--project`` (repeatable) becomes the key's ``projects`` list."""
    rc, api = _run_main(
        [
            "admin",
            "keys",
            "create",
            "--principal",
            "ada",
            "--name",
            "ci",
            "--project",
            "audit-in",
        ],
        api_side_effect=[
            [{"id": 2, "username": "ada"}],
            {"id": 4, "key_prefix": "och_", "plaintext_key": "k"},
        ],
    )
    assert rc == 0
    assert api.call_args_list[1].args[2]["projects"] == ["audit-in"]
