from __future__ import annotations

from httpx import AsyncClient

from tests.web_support import (
    _form_carries_policy_version,  # noqa: F401
    _web_project_tokens,
)


async def test_web_patch_routes_reject_agent_token(
    client: AsyncClient, monkeypatch, db
):
    # #351 AC-1: the human gate on web-activate/edit/archive must hold for an
    # agent Bearer — the web path is guarded only by require_human_or_admin.
    from hub import config
    from hub import repository as repo_module

    monkeypatch.setattr(config, "HUB_TOKENS", _web_project_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent = {"Authorization": "Bearer agent-token"}

    pid = await repo_module.create_project(
        db, slug="gate", name="Gate", status="pending"
    )
    await db.commit()

    for url, data in (
        (f"/projects/{pid}/web-activate", {}),
        (f"/projects/{pid}/web-edit", {"name": "Hacked"}),
        (f"/projects/{pid}/web-archive", {"archived": "true"}),
    ):
        resp = await client.post(url, data=data, headers=agent, follow_redirects=False)
        assert resp.status_code == 403, url

    row = await repo_module.get_project(db, pid)
    assert row["status"] == "pending"
    assert row["name"] == "Gate"
    assert row["archived"] == 0


async def test_web_human_only_routes_reject_agent_token(
    client: AsyncClient, monkeypatch, db
):
    """#427: agent bearer must not bypass human-only web lifecycle gates."""
    from hub import config
    from hub import repository as repo_module

    monkeypatch.setattr(config, "HUB_TOKENS", _web_project_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent = {"Authorization": "Bearer agent-token"}

    draft_id = await repo_module.create_task(
        db,
        title="Draft gate",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="draft",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    open_id = await repo_module.create_task(
        db,
        title="Open gate",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    needs_info_id = await repo_module.create_task(
        db,
        title="Needs info gate",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="needs_info",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    needs_decision_id = await repo_module.create_task(
        db,
        title="Needs decision gate",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="needs_decision",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    pending_id = await repo_module.create_task(
        db,
        title="Pending report gate",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="pending_report",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    proposal_id = await repo_module.create_task(
        db,
        title="Agent proposal",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="bot",
        rationale="",
        status="draft",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    routes = [
        (f"/tasks/{draft_id}/web-approve", {}),
        (f"/tasks/{draft_id}/web-reject", {}),
        (f"/tasks/{open_id}/web-start", {}),
        (f"/tasks/{needs_info_id}/web-answer", {"answer": "ok"}),
        (
            f"/tasks/{needs_decision_id}/web-decide",
            {"action": "accept", "decision_summary": "yes"},
        ),
        (f"/tasks/{pending_id}/web-force-complete", {"comment": "override"}),
        (f"/proposals/{proposal_id}/approve", {}),
        (f"/proposals/{proposal_id}/reject", {}),
    ]
    for url, data in routes:
        resp = await client.post(url, data=data, headers=agent, follow_redirects=False)
        assert resp.status_code == 403, url

    assert (await repo_module.get_task(db, draft_id))["status"] == "draft"
    assert (await repo_module.get_task(db, open_id))["status"] == "open"
    assert (await repo_module.get_task(db, pending_id))["status"] == "pending_report"


async def test_web_create_form_rejects_agent_token(
    client: AsyncClient, monkeypatch, db
):
    """#360: the web create form is the third door onto ready work.

    The REST endpoints were gated first; this form was not, and it builds
    TaskCreate with the default source=human while honouring run_immediately —
    so an agent token landed a task in `running` with a dispatched job. Found
    while assembling context for a machine review of the REST-only fix.
    """
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _web_project_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent = {"Authorization": "Bearer agent-token"}

    resp = await client.post(
        "/tasks/create",
        data={"title": "Agent via web form", "run_immediately": "true"},
        headers=agent,
        follow_redirects=False,
    )

    assert resp.status_code == 403
    # Round-2 machine-review finding: the refusal must point at the alternative
    # the agent can actually take. human_only_gate says "retry with a human
    # token" and offers no tool — a dead end for the only actor that can hit
    # this. The REST endpoints answer agent_create_forbidden with
    # hub_propose_task for the identical violation, so this route must too.
    detail = resp.json()["detail"]
    assert detail["reason"] == "agent_create_forbidden"
    assert detail["suggested_tool"] == "hub_propose_task"
    assert "hub_propose_task" in detail["next_action"]
    assert detail["actor_hint"] == "human"

    listing = await client.get("/api/tasks", headers=agent)
    assert all(t["title"] != "Agent via web form" for t in listing.json())


async def test_web_create_form_human_token_still_works(
    client: AsyncClient, monkeypatch
):
    # The gate must not close the form for the people it exists for.
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _web_project_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    human = {"Authorization": "Bearer human-token"}

    resp = await client.post(
        "/tasks/create",
        data={"title": "Human via web form"},
        headers=human,
        follow_redirects=False,
    )

    assert resp.status_code == 303
    listing = await client.get("/api/tasks", headers=human)
    made = [t for t in listing.json() if t["title"] == "Human via web form"]
    assert len(made) == 1
    assert made[0]["status"] == "open"


async def test_web_force_complete_human_token_still_works(
    client: AsyncClient, monkeypatch, db
):
    from hub import config
    from hub import repository as repo_module

    monkeypatch.setattr(config, "HUB_TOKENS", _web_project_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    human = {"Authorization": "Bearer human-token"}

    task_id = await repo_module.create_task(
        db,
        title="Human override",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="pending_report",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    resp = await client.post(
        f"/tasks/{task_id}/web-force-complete",
        data={"comment": "human approved override"},
        headers=human,
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303)
    row = await repo_module.get_task(db, task_id)
    assert row["status"] == "completed"


async def test_web_create_project_agent_token_creates_pending(
    client: AsyncClient, monkeypatch, db
):
    # #351 AC-2: agent→pending must survive through the web-create wrapper,
    # not only through POST /api/projects.
    from hub import config
    from hub import repository as repo_module

    monkeypatch.setattr(config, "HUB_TOKENS", _web_project_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "ALLOW_AGENT_PROJECTS", "propose")
    agent = {"Authorization": "Bearer agent-token"}

    resp = await client.post(
        "/projects/web-create",
        data={"slug": "agent-made", "name": "Agent Made"},
        headers=agent,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = await repo_module.get_project_by_slug(db, "agent-made")
    assert row is not None
    assert row["status"] == "pending"


async def test_web_skills_pages(client: AsyncClient, db):
    # #380: list page, detail page, Activate button for drafts.
    from hub import repository as repo_module
    from hub.db import seed_default_skills

    await seed_default_skills(db)
    await repo_module.create_skill_version(
        db,
        name="multi-agent-review",
        content="v2 draft",
        status="draft",
        created_by="bot",
    )
    await db.commit()

    page = await client.get("/skills")
    assert page.status_code == 200
    assert "multi-agent-review" in page.text

    detail = await client.get("/skills/multi-agent-review")
    assert detail.status_code == 200
    assert "web-activate" in detail.text  # кнопка для драфта v2

    resp = await client.post(
        "/skills/multi-agent-review/versions/2/web-activate",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = await repo_module.get_active_skill(db, "multi-agent-review")
    assert row["version"] == 2


async def test_web_create_skill_from_form(client: AsyncClient, db):
    # AC-1 (#385): create form → active skill (human path) in the list.
    from hub import repository as repo_module

    resp = await client.post(
        "/skills/web-create",
        data={
            "name": "dor-checklist",
            "kind": "checklist",
            "tags": "dor, quality",
            "content": "- AC present?\n- risks listed?",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/skills/dor-checklist"

    row = await repo_module.get_active_skill(db, "dor-checklist")
    assert row is not None and row["version"] == 1 and row["status"] == "active"

    page = await client.get("/skills")
    assert "dor-checklist" in page.text


async def test_web_edit_as_new_version(client: AsyncClient, db):
    # AC-2 (#385): active content prefilled; save creates N+1, history kept.
    from hub import repository as repo_module
    from hub.db import seed_default_skills

    await seed_default_skills(db)

    detail = await client.get("/skills/multi-agent-review")
    assert "refuted" in detail.text  # textarea prefilled with active v1
    assert 'name="content"' in detail.text  # Edit-as-new-version form present

    resp = await client.post(
        "/skills/multi-agent-review/web-new-version",
        data={"content": "v2 harness body", "tags": "review"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    versions = await repo_module.list_skill_versions(db, "multi-agent-review")
    assert len(versions) == 2
    assert versions[0]["version"] == 2  # newest first
    # v1 стала неактивной? нет — новая версия у человека сразу active,
    # активной считается старшая active-версия
    active = await repo_module.get_active_skill(db, "multi-agent-review")
    assert active["version"] == 2
    assert active["content"] == "v2 harness body"
    # история цела: v1 всё ещё существует
    assert any(v["version"] == 1 for v in versions)


async def test_web_create_skill_bad_slug(client: AsyncClient, db):
    # AC-3 (#385): invalid slug → form error, not a 500.
    resp = await client.post(
        "/skills/web-create",
        data={"name": "Bad Slug!", "kind": "prompt", "content": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "skill_error=" in resp.headers["location"]


async def test_web_new_version_unknown_skill_404(client: AsyncClient, db):
    resp = await client.post(
        "/skills/nope/web-new-version",
        data={"content": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 404


# ---- Roles for machine identities are assignable from the admin UI (#613) ----
#
# The role model was already complete — set_principal_roles takes a LIST of
# slugs, guards the last admin, and writes audit — but the UI reached none of
# it: agent creation hardcoded the `agent` role and role editing lived only on
# the users page. #546 made that concrete: its ci_runner role exists on
# production carrying exactly tasks.read + tasks.ci_report, and an admin
# looking at the admin UI had no way to grant it. A narrow role nobody can
# grant is a narrow role nobody uses.


def _admin_headers(monkeypatch) -> dict:
    """An admin identity for the web admin pages, which require one."""
    from hub import config

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        config.parse_tokens("root:admin-token:admin,denis:human-token:human"),
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return {"Authorization": "Bearer admin-token"}


async def _roles_of(db, username: str) -> list[str]:
    from hub.services import admin as admin_svc

    for p in await admin_svc.list_principals(db):
        if p["username"] == username:
            return sorted(p["roles"])
    raise AssertionError(f"principal {username!r} not found")


async def test_agent_is_created_with_the_chosen_role(
    client: AsyncClient, db, monkeypatch
):
    # AC-1 (#613): the role chosen in the form is the role the identity gets.
    # Before this, every identity created through the UI was an `agent` — the
    # one role that deliberately does NOT carry tasks.ci_report (#546).
    admin = _admin_headers(monkeypatch)

    resp = await client.post(
        "/admin/agents/create",
        data={
            "username": "ci-runner",
            "display_name": "GitHub Actions",
            "role": "ci_runner",
        },
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert await _roles_of(db, "ci-runner") == ["ci_runner"], (
        "the identity must carry exactly the chosen role, not the agent default"
    )

    # The CREATE picker must be built from the roles in the database. Assert
    # inside the <select> itself: the row-level checkbox list mentions every role
    # too, so a page-wide search passes even when the picker is hardcoded — the
    # first version of this assertion did exactly that and let the mutation live.
    page = await client.get("/admin/agents", headers=admin)
    picker = page.text.split('id="ca-role"', 1)[1].split("</select>", 1)[0]
    assert 'value="ci_runner"' in picker, "the create form must offer DB roles"
    assert 'value="viewer"' in picker


async def test_creating_an_agent_without_a_role_still_gets_agent(
    client: AsyncClient, db, monkeypatch
):
    # AC-5 (#613): the default is unchanged. Adding a choice must not silently
    # change what happens when nobody chooses.
    admin = _admin_headers(monkeypatch)

    resp = await client.post(
        "/admin/agents/create",
        data={"username": "plain-agent"},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert await _roles_of(db, "plain-agent") == ["agent"]


async def test_agent_roles_can_be_changed_from_the_agents_page(
    client: AsyncClient, db, monkeypatch
):
    # AC-2 (#613): roles are editable where the identity is visible, the change
    # is recorded in audit, and the response re-renders the AGENTS table. That
    # last part is not cosmetic: htmx swaps by hx-select, so a handler returning
    # the users page would silently replace the agents table with human users.
    from hub.services import admin as admin_svc

    admin = _admin_headers(monkeypatch)
    created = await admin_svc.create_principal(
        db, kind="agent", username="promoted-bot", role_slug="agent"
    )

    resp = await client.post(
        f"/admin/agents/{created['id']}/edit-roles",
        data={"roles": ["ci_runner", "viewer"]},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert await _roles_of(db, "promoted-bot") == ["ci_runner", "viewer"]

    assert 'id="agents-table"' in resp.text, "the agents table must come back"
    assert 'id="users-table"' not in resp.text, (
        "returning the users page would let htmx swap the wrong table in"
    )

    rows = await db.execute_fetchall(
        "SELECT action, target_id FROM admin_audit_log WHERE action='set_roles'"
    )
    assert [dict(r)["target_id"] for r in rows] == [str(created["id"])], (
        "a role change is an audited event on every path, not just the users one"
    )


async def test_service_principals_are_visible_in_the_admin_ui(
    client: AsyncClient, db, monkeypatch
):
    # AC-3 (#613): a `service` identity — which is what #546's CI reporter is —
    # used to be invisible everywhere except the keys page, because the agents
    # page filtered kind='agent'. An identity you can create but never see
    # again cannot be administered at all.
    from hub.services import admin as admin_svc

    admin = _admin_headers(monkeypatch)
    svc = await admin_svc.create_principal(
        db, kind="service", username="ci-service", role_slug="ci_runner"
    )

    page = await client.get("/admin/agents", headers=admin)
    assert page.status_code == 200, page.text
    assert "ci-service" in page.text, "a service identity must be listed"

    # And it must be administrable, not merely listed.
    resp = await client.post(
        f"/admin/agents/{svc['id']}/edit-roles",
        data={"roles": ["viewer"]},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert await _roles_of(db, "ci-service") == ["viewer"]


async def test_the_last_admin_cannot_lose_the_admin_role(
    client: AsyncClient, db, monkeypatch
):
    # AC-4 (#613): the guard that already exists must hold on the NEW path too.
    # A second way into set_principal_roles is a second way to lock everyone out.
    from hub.services import admin as admin_svc

    admin = _admin_headers(monkeypatch)
    only_admin = await admin_svc.create_principal(
        db, kind="human", username="sole-admin", role_slug="admin"
    )

    resp = await client.post(
        f"/admin/agents/{only_admin['id']}/edit-roles",
        data={"roles": ["viewer"]},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert await _roles_of(db, "sole-admin") == ["admin"], (
        "the last admin keeps the role no matter which page asked"
    )
    assert "last active admin" in resp.text.lower() or "admin" in resp.text.lower()


async def test_role_editing_requires_an_admin_identity(
    client: AsyncClient, db, monkeypatch
):
    # The new routes are a path to privilege escalation if they sit outside the
    # admin gate. Every other /admin handler is behind _require_admin_web;
    # these must be too.
    from hub.services import admin as admin_svc

    _admin_headers(monkeypatch)
    target = await admin_svc.create_principal(
        db, kind="agent", username="untouchable", role_slug="agent"
    )
    human = {"Authorization": "Bearer human-token"}

    create = await client.post(
        "/admin/agents/create",
        data={"username": "sneaky", "role": "super_admin"},
        headers=human,
    )
    assert create.status_code == 403, create.text

    edit = await client.post(
        f"/admin/agents/{target['id']}/edit-roles",
        data={"roles": ["super_admin"]},
        headers=human,
    )
    assert edit.status_code == 403, edit.text
    assert await _roles_of(db, "untouchable") == ["agent"]


async def test_the_roles_page_marks_permissions_that_gate_nothing(
    client: AsyncClient, monkeypatch
):
    # AC-4 (#614): an admin reading the roles page must be able to tell a real
    # limit from a decorative one. Reading narrowness off this list is exactly
    # how a false claim about the CI token reached the owner in #613.
    from hub.db import DECLARED_ONLY_PERMISSIONS, ENFORCED_PERMISSIONS

    admin = _admin_headers(monkeypatch)
    page = await client.get("/admin/roles", headers=admin)
    assert page.status_code == 200, page.text

    # super_admin carries every permission, so both kinds are on the page.
    for decorative in ("tasks.create", "tasks.update"):
        assert decorative in DECLARED_ONLY_PERMISSIONS  # guards the premise
        assert f"{decorative} ⚠" in page.text, (
            f"{decorative} gates nothing and must be marked as such"
        )
    for real in ("tasks.delete", "tasks.ci_report"):
        assert real in ENFORCED_PERMISSIONS
        assert f"{real} ⚠" not in page.text, (
            f"{real} does gate something — marking it would be a new lie"
        )
    assert "не проверяются кодом" in page.text, "explain the mark, not just show it"


async def test_web_key_form_can_scope_the_key_to_a_project(
    client: AsyncClient, db, monkeypatch
):
    """#1644: the key form carries a project; an unknown one is refused."""
    from hub import repository as repo
    from hub.services import admin as admin_svc

    admin = _admin_headers(monkeypatch)
    await repo.create_project(
        db, slug="audit-in", name="audit-in", workspace_path="/tmp/ws"
    )
    principal = await admin_svc.create_principal(
        db, kind="service", username="ci-web-1644", role_slug="ci_runner"
    )
    await db.commit()

    ok = await client.post(
        "/admin/keys/create",
        data={"principal_id": principal["id"], "name": "scoped", "project": "audit-in"},
        headers=admin,
    )
    assert ok.status_code == 200, ok.text
    keys = await admin_svc.list_api_keys(db, principal_id=principal["id"])
    assert [k["name"] for k in keys] == ["scoped"]
    assert keys[0]["scopes"] == ["project:audit-in"]

    bad = await client.post(
        "/admin/keys/create",
        data={"principal_id": principal["id"], "name": "bad", "project": "nope"},
        headers=admin,
    )
    assert "bad" not in [
        k["name"]
        for k in await admin_svc.list_api_keys(db, principal_id=principal["id"])
    ]
    assert bad.status_code == 422, "same status as the API"
    assert "nope" in str(dict(bad.headers)), "the refusal names the project"
