"""Filing a production defect in one call (#915, feature #906).

The intake fills what a person under fire should not have to type — the
passport (found_in=prod, detected_at), expedite, bugfix, the release the
defect showed up in — and asks for the minimum: what broke, how to check the
fix, where. What it does NOT do is skip a gate: the defect is a draft that
already passes DoR and waits for a human, like every other proposal.
"""

from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, patch

import aiosqlite

from hub.services import prod_defect

SHA_OLD = "1" * 40
SHA_FAILED = "2" * 40
SHA_NEW = "3" * 40
SHA_OTHER = "4" * 40

DEFECT = {
    "title": "Карточка задачи отдаёт 500",
    "broken": "GET /tasks/{id} падает 500 на задачах с паспортом дефекта",
    "verify": "curl /tasks/917 отвечает 200 и показывает паспорт",
    "affected_areas": ["hub/templates/task_detail.html"],
}


async def _project(db: aiosqlite.Connection, slug: str) -> int:
    await db.execute(
        "INSERT OR IGNORE INTO projects (slug, name) VALUES (?, ?)", (slug, slug)
    )
    rows = await db.execute_fetchall("SELECT id FROM projects WHERE slug=?", (slug,))
    return int(rows[0]["id"])


async def _release(
    db: aiosqlite.Connection, sha: str, project_id: int | None, status: str = "success"
) -> int:
    cur = await db.execute(
        "INSERT INTO releases (project_id, deployed_sha, ref, status, source) "
        "VALUES (?, ?, 'refs/heads/main', ?, 'ci')",
        (project_id, sha, status),
    )
    return int(cur.lastrowid or 0)


async def _file(client, **overrides) -> dict:
    resp = await client.post("/api/prod-defects", json={**DEFECT, **overrides})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


# --- AC-1 -------------------------------------------------------------------


async def test_defect_binds_to_last_successful_release(client, db):
    default = await _project(db, "default")
    other = await _project(db, "other")
    # The deploy callback in ci.yml sends no project: on a real installation
    # every release row is project-less, and it is the default project's.
    await _release(db, SHA_OLD, None)
    newest = await _release(db, SHA_NEW, default)
    await _release(db, SHA_FAILED, None, status="failure")
    await _release(db, SHA_OTHER, other)
    await db.commit()

    filed = await _file(client)
    task = filed["task"]

    assert filed["release_id"] == newest
    assert filed["release_reason"] == ""
    assert task["release_id"] == newest
    assert task["found_in"] == "prod"
    assert task["detected_at"]
    assert task["class_of_service"] == "expedite"
    assert task["wip_tag"] == "bugfix"
    assert task["problem_statement"] == DEFECT["broken"]
    assert task["affected_areas"] == DEFECT["affected_areas"]
    # The release link is the one #917 reads: the hypothesis is computed.
    assert task["cause_suggestion"]["release_id"] == newest


async def test_projectless_release_counts_for_the_default_project(client, db):
    await _project(db, "default")
    legacy = await _release(db, SHA_OLD, None)
    await db.commit()

    filed = await _file(client)

    assert filed["release_id"] == legacy


# --- AC-2 -------------------------------------------------------------------


async def test_missing_release_is_explicit(client, db):
    await _project(db, "default")
    await _release(db, SHA_FAILED, None, status="failure")
    await db.commit()

    filed = await _file(client)

    assert filed["release_id"] is None
    assert filed["task"]["release_id"] is None
    assert filed["release_reason"] == prod_defect.REASON_NO_RELEASE
    notes = [u["content"] for u in filed["task"]["updates"] or []]
    assert any(prod_defect.REASON_NO_RELEASE in n for n in notes), notes


async def test_other_projects_do_not_borrow_projectless_releases(client, db):
    other = await _project(db, "other")
    await _release(db, SHA_OLD, None)
    cur = await db.execute(
        "INSERT INTO tasks (title, description, status, task_type, project_id) "
        "VALUES ('эпик', '', 'open', 'epic', ?)",
        (other,),
    )
    epic = int(cur.lastrowid or 0)
    cur = await db.execute(
        "INSERT INTO tasks (title, description, status, task_type, parent_id) "
        "VALUES ('фича', '', 'open', 'feature', ?)",
        (epic,),
    )
    feature = int(cur.lastrowid or 0)
    await db.commit()

    filed = await _file(client, parent_id=feature)

    assert filed["release_id"] is None
    assert filed["release_reason"] == prod_defect.REASON_NO_RELEASE


# --- AC-3 -------------------------------------------------------------------


async def test_defect_draft_is_ready_and_waits_for_a_human(client, db):
    await _project(db, "default")
    await db.commit()

    # The client fixture is a HUMAN: the intake still files a draft — a human
    # reporting a defect is not a human approving the fix.
    filed = await _file(client)
    task = filed["task"]

    assert task["status"] == "draft"
    assert task["dor_passed"] is True
    assert task["class_of_service"] == "expedite"
    assert task["work_type"] == "incident"
    acs = task["acceptance_criteria"]
    assert len(acs) == 1 and acs[0]["then"] == DEFECT["verify"]
    assert task["validation_commands"] == [DEFECT["verify"]]

    rows = await db.execute_fetchall(
        "SELECT status FROM tasks WHERE id=?", (task["id"],)
    )
    assert rows[0]["status"] == "draft"


async def test_intake_refuses_without_the_minimum(client, db):
    await _project(db, "default")
    await db.commit()
    for missing in ("broken", "verify", "affected_areas"):
        body = {k: v for k, v in DEFECT.items() if k != missing}
        resp = await client.post("/api/prod-defects", json=body)
        assert resp.status_code == 422, (missing, resp.text)
    resp = await client.post(
        "/api/prod-defects", json={**DEFECT, "affected_areas": ["  "]}
    )
    assert resp.status_code == 422


# --- surfaces ---------------------------------------------------------------


async def test_mcp_propose_files_a_prod_defect():
    from hub.mcp_server import hub_propose_task

    with patch("hub.mcp_server._api_post", new_callable=AsyncMock) as post:
        post.return_value = {
            "task": {"id": 77},
            "release_id": None,
            "release_reason": prod_defect.REASON_NO_RELEASE,
        }
        msg = await hub_propose_task(
            DEFECT["title"],
            DEFECT["broken"],
            agent="pda_claude",
            parent_id=5,
            defect_verify=DEFECT["verify"],
            affected_areas=DEFECT["affected_areas"],
        )

    post.assert_awaited_once_with(
        "/api/prod-defects",
        {
            "title": DEFECT["title"],
            "broken": DEFECT["broken"],
            "verify": DEFECT["verify"],
            "affected_areas": DEFECT["affected_areas"],
            "agent": "pda_claude",
            "parent_id": 5,
        },
    )
    assert "#77" in msg and prod_defect.REASON_NO_RELEASE in msg


async def test_mcp_propose_without_defect_is_unchanged():
    from hub.mcp_server import hub_propose_task

    with patch("hub.mcp_server._api_post", new_callable=AsyncMock) as post:
        post.return_value = {"id": 5}
        await hub_propose_task("X", "Y")
    assert post.await_args.args[0] == "/api/tasks"


def test_cli_propose_files_a_prod_defect():
    from hub.cli import build_parser

    args = build_parser().parse_args(
        [
            "propose",
            "--title",
            DEFECT["title"],
            "--description",
            DEFECT["broken"],
            "--defect-verify",
            DEFECT["verify"],
            "--area",
            DEFECT["affected_areas"][0],
        ]
    )
    with patch("hub.cli._api", return_value={"task": {"id": 1}}) as api:
        assert args.func(args) == 0
    api.assert_called_once_with(
        "POST",
        "/api/prod-defects",
        {
            "title": DEFECT["title"],
            "broken": DEFECT["broken"],
            "verify": DEFECT["verify"],
            "affected_areas": DEFECT["affected_areas"],
            "agent": "",
        },
    )


def test_cli_propose_without_defect_is_unchanged():
    from hub.cli import cmd_propose

    args = argparse.Namespace(
        title="X", description="Y", agent="", rationale="", parent=None
    )
    with patch("hub.cli._api", return_value={"id": 1}) as api:
        cmd_propose(args)
    assert api.call_args.args[1] == "/api/tasks"
