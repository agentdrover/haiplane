"""A typo in a project policy is refused on write, not swallowed (#886).

``default_branch_policy`` has exactly one reader —
``project_policy.release_base_of`` — and it falls back to
``config.RELEASE_BRANCH`` when the key is absent. That fallback is correct for
a project which declared no release branch, and it is *indistinguishable* from
the result of a typo: ``releaseBase`` is not found either, so the project
quietly runs on the hub's default while its owner reads their own JSON in the
project card and believes the policy is set. Nothing in the API, the UI or the
log tells those two apart after the fact.

So the refusal has to happen at the only moment the difference is still cheap:
the write, with the person who made the typo still in front of the form. These
tests hold four things:

* the refusal names both the unknown key and the ones that exist (AC-1);
* "declared nothing" stays a legal state and still falls back (AC-2);
* the allowed set cannot fall behind the reader — a key read in
  ``project_policy`` but declared nowhere turns this suite red (AC-3);
* every surface that can put a policy into the database refuses it, not just
  the one where the check was written first (AC-4).
"""

from __future__ import annotations

import argparse
import ast
import json
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from hub import cli, config
from hub import repository as repo
from hub.models import GATE_POLICY_KEYS
from hub.services.project_policy import (
    DEFAULT_BRANCH_POLICY_KEYS,
    release_base_of,
    validate_default_branch_policy,
)

TYPO = "releaseBase"
BAD_POLICY = {TYPO: "main"}
POLICY_READER = Path(__file__).resolve().parents[1] / "hub/services/project_policy.py"


async def _project(db, slug: str) -> int:
    pid = await repo.create_project(db, slug=slug, name=slug.title())
    await db.commit()
    return pid


# ---------------------------------------------------------------------------
# AC-1 — the refusal is usable without reading the source
# ---------------------------------------------------------------------------


def test_unknown_policy_key_is_rejected_with_both_lists() -> None:
    with pytest.raises(ValueError) as exc:
        validate_default_branch_policy(BAD_POLICY)
    message = str(exc.value)
    assert TYPO in message, "the message must name what was actually written"
    assert "release_base" in message, (
        "an 'unknown key' message without the allowed keys sends the reader "
        "to the source — the message is shown to a human in the project card"
    )
    # A correct policy passes through untouched: this refuses typos, not use.
    assert validate_default_branch_policy({"release_base": "main"}) == {
        "release_base": "main"
    }


# ---------------------------------------------------------------------------
# AC-2 — "we declared nothing" remains a legal, working state
# ---------------------------------------------------------------------------


async def test_absent_key_still_falls_back(client: AsyncClient, db) -> None:
    for slug, policy in (("silent", {}), ("unset", None)):
        body = {"slug": slug, "name": slug.title()}
        if policy is not None:
            body["default_branch_policy"] = policy
        resp = await client.post("/api/projects", json=body)
        assert resp.status_code == 200, resp.text
        row = await repo.get_project(db, resp.json()["id"])
        assert release_base_of(row) == config.RELEASE_BRANCH, (
            "a project that declared no release base keeps the hub default — "
            "the validation refuses typos, it does not make the key required"
        )


# ---------------------------------------------------------------------------
# AC-3 — the allowed set cannot fall behind the reader
# ---------------------------------------------------------------------------


def _keys_read_by_reader() -> set[str]:
    """Every policy key ``project_policy`` looks up, found in its source.

    Read from the file rather than from a hand-kept list on purpose: a list
    would have to be updated by the same person who forgot to update the
    allowed set, so it would agree with the mistake instead of catching it.
    """
    tree = ast.parse(POLICY_READER.read_text())
    constants: dict[str, str] = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for target in node.targets
        if isinstance(target, ast.Name) and isinstance(node.value.value, str)
    }
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        receiver = node.func.value
        if node.func.attr != "get" or not isinstance(receiver, ast.Name):
            continue
        if "policy" not in receiver.id or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            keys.add(arg.value)
        elif isinstance(arg, ast.Name) and arg.id in constants:
            keys.add(constants[arg.id])
        else:  # pragma: no cover - a key this scan cannot resolve
            pytest.fail(
                f"policy key read at line {node.lineno} is not a literal or a "
                "module constant, so this check can no longer see it; keep "
                "reads simple or teach the scan to resolve them"
            )
    return keys


def test_allowed_keys_stay_in_sync_with_reader() -> None:
    declared = set(DEFAULT_BRANCH_POLICY_KEYS) | set(GATE_POLICY_KEYS)
    undeclared = _keys_read_by_reader() - declared
    assert not undeclared, (
        f"hub/services/project_policy.py reads {sorted(undeclared)}, and no "
        "allowed-key set declares them: every write carrying such a key is "
        "refused as unknown, so the reader would read a key nobody can save. "
        "Add it to DEFAULT_BRANCH_POLICY_KEYS (project_policy) or to "
        "GATE_POLICY_KEYS (hub/models.py), next to the reader it belongs to."
    )
    # release_base is the case the whole task is about; if it ever leaves the
    # reader, the set above must lose it too rather than keep a dead key.
    assert "release_base" in _keys_read_by_reader()


# ---------------------------------------------------------------------------
# AC-4 — every surface that can write a policy refuses the typo
# ---------------------------------------------------------------------------


async def test_every_write_surface_rejects_unknown_key(client: AsyncClient, db) -> None:
    """REST (create and edit), the web form, MCP and the CLI.

    Two of the four cannot express a policy at all today, and that is checked
    here rather than assumed: ``hub_create_project`` and ``oc-hub projects
    create`` build their request bodies without the field. So the test holds
    both halves for them — the body they send carries no policy, and a policy
    put into that same body is refused by the endpoint they send it to. A
    future ``--policy`` flag inherits the refusal; a future flag that routes
    around the endpoint turns this red.
    """
    # --- surface 1: REST create -------------------------------------------
    resp = await client.post(
        "/api/projects",
        json={"slug": "rest-new", "name": "Rest", "default_branch_policy": BAD_POLICY},
    )
    assert resp.status_code == 422, f"POST /api/projects accepted a typo: {resp.text}"
    assert TYPO in resp.text and "release_base" in resp.text
    assert await repo.get_project_by_slug(db, "rest-new") is None, (
        "a refused create must leave no project behind"
    )

    # --- surface 2: REST edit ---------------------------------------------
    pid = await _project(db, "rest-edit")
    resp = await client.patch(
        f"/api/projects/{pid}", json={"default_branch_policy": BAD_POLICY}
    )
    assert resp.status_code == 422, f"PATCH accepted a typo: {resp.text}"
    assert TYPO in resp.text and "release_base" in resp.text
    row = await repo.get_project(db, pid)
    assert json.loads(row["default_branch_policy"]) == {}

    # --- surface 3: the project card form ---------------------------------
    for url, data in (
        (
            "/projects/web-create",
            {"slug": "web-new", "name": "Web", "default_branch_policy": ""},
        ),
        (
            f"/projects/{pid}/web-edit",
            {"name": "Web Edit", "default_branch_policy": ""},
        ),
    ):
        data["default_branch_policy"] = json.dumps(BAD_POLICY)
        resp = await client.post(url, data=data, follow_redirects=False)
        assert resp.status_code == 303, resp.text
        location = resp.headers["location"]
        assert "project_error=" in location, f"{url} swallowed a typo: {location}"
        assert TYPO in location and "release_base" in location, (
            f"{url} must show the human which key it refused and what exists"
        )
    assert await repo.get_project_by_slug(db, "web-new") is None
    assert (await repo.get_project(db, pid))["name"] == "rest-edit".title()

    # --- surface 4: MCP tools ---------------------------------------------
    from hub.mcp_server import hub_create_project, hub_propose_project

    for tool in (hub_create_project, hub_propose_project):
        sent = AsyncMock(return_value={"id": 1, "slug": "mcp-new", "status": "active"})
        with patch("hub.mcp_server._api_post", sent):
            await tool(slug="mcp-new", name="Mcp")
        body = sent.await_args.args[1]
        assert "default_branch_policy" not in body, (
            f"{tool.__name__} now sends a policy; route it through the same "
            "validated endpoint and assert the refusal here"
        )
        resp = await client.post(
            "/api/projects", json={**body, "default_branch_policy": BAD_POLICY}
        )
        assert resp.status_code == 422, (
            f"the endpoint {tool.__name__} posts to accepted a typo: {resp.text}"
        )

    # --- surface 5: the CLI ------------------------------------------------
    called = MagicMock(return_value={"id": 1, "slug": "cli-new"})
    args = argparse.Namespace(
        slug="cli-new",
        name="Cli",
        repo="",
        workspace_path="",
        default_branch="develop",
        forge="github",
    )
    with patch.object(cli, "_api", called), patch("sys.stdout", new=StringIO()):
        assert cli.cmd_projects_create(args) == 0
    method, path, body = called.call_args.args
    assert (method, path) == ("POST", "/api/projects")
    assert "default_branch_policy" not in body, (
        "the CLI now sends a policy; it holds no rules of its own (#486), so "
        "the refusal must still come from the endpoint — assert it here"
    )
    resp = await client.post(path, json={**body, "default_branch_policy": BAD_POLICY})
    assert resp.status_code == 422, f"the CLI's endpoint accepted a typo: {resp.text}"


# ---------------------------------------------------------------------------
# The other half of "no silent rollback": a card edit must not erase a knob
# the card never showed (#886, found while closing the sets above).
# ---------------------------------------------------------------------------


async def test_web_edit_keeps_gate_policy_keys_the_form_never_showed(
    client: AsyncClient, db
) -> None:
    pid = await _project(db, "keeps")
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"gate_policy": {"ci_runner": "make test", "review": "dispatch"}},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={
            "name": "Keeps",
            "gate_policy_dor": "human",
            "gate_policy_verdict": "human",
            "gate_policy_review": "off",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text

    stored = json.loads((await repo.get_project(db, pid))["gate_policy"])
    assert stored.get("ci_runner") == "make test", (
        "the form has no ci_runner field, so submitting it cannot mean "
        "'remove ci_runner' — that would undo an API-set value with no trace"
    )
    assert "review" not in stored, (
        "the review select IS on the form, and 'off' there does mean remove"
    )
