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
    # Authorship is recorded from the token (#360), and it is still a draft.
    assert task["source"] == "human"
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


# --- #916: defect clocks ----------------------------------------------------
#
# time-to-detect  = detected_at - releases.deployed_at (via tasks.release_id)
# time-to-restore = resolved_at - detected_at
# resolved_at is stamped when the defect reaches `completed`; a row missing a
# fact is counted under its reason and never enters a median.


async def _close(db: aiosqlite.Connection, task_id: int) -> None:
    from hub import repository as repo

    await repo.update_task(db, task_id, status="completed")
    await db.commit()


async def _clocks(db: aiosqlite.Connection) -> dict:
    from hub.services.orchestration import practice_metrics

    return (await practice_metrics(db))["prod_defect_clocks"]


async def test_restore_clock_from_recorded_facts(client, db):
    release = await _release(db, SHA_NEW, None)
    await db.commit()
    task_id = (await _file(client))["task"]["id"]
    # Known facts: deployed 5h ago, noticed 2h ago -> detect 3h, restore ~2h.
    await db.execute(
        "UPDATE releases SET deployed_at=datetime('now', '-5 hours') WHERE id=?",
        (release,),
    )
    await db.execute(
        "UPDATE tasks SET detected_at=datetime('now', '-2 hours') WHERE id=?",
        (task_id,),
    )
    await db.commit()

    rows = await db.execute_fetchall(
        "SELECT resolved_at FROM tasks WHERE id=?", (task_id,)
    )
    assert rows[0]["resolved_at"] is None
    await _close(db, task_id)
    rows = await db.execute_fetchall(
        "SELECT resolved_at, completed_at FROM tasks WHERE id=?", (task_id,)
    )
    assert rows[0]["resolved_at"] == rows[0]["completed_at"]

    clocks = await _clocks(db)
    assert clocks["defects"] == 1
    ttd, ttr = clocks["time_to_detect"], clocks["time_to_restore"]
    assert ttd["measured"] == 1 and ttd["unmeasurable_total"] == 0
    assert ttd["median_hours"] == 3.0
    assert ttr["measured"] == 1 and ttr["unmeasurable_total"] == 0
    assert 1.99 <= ttr["median_hours"] <= 2.01

    # Reopened and closed again: the stamp stays, it records the first recovery.
    from hub import repository as repo

    await db.execute(
        "UPDATE tasks SET resolved_at='2000-01-01 00:00:00' WHERE id=?", (task_id,)
    )
    await repo.update_task(db, task_id, status="running")
    await _close(db, task_id)
    rows = await db.execute_fetchall(
        "SELECT resolved_at FROM tasks WHERE id=?", (task_id,)
    )
    assert rows[0]["resolved_at"] == "2000-01-01 00:00:00"


async def test_resolved_at_is_stamped_on_every_completion_path(client, db):
    from hub import repository as repo

    task_id = (await _file(client))["task"]["id"]
    status = (
        await db.execute_fetchall("SELECT status FROM tasks WHERE id=?", (task_id,))
    )[0]["status"]
    assert await repo.transition_status_if(
        db, task_id, expected_from=status, new_status="completed"
    )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT resolved_at, completed_at FROM tasks WHERE id=?", (task_id,)
    )
    assert rows[0]["resolved_at"] and rows[0]["resolved_at"] == rows[0]["completed_at"]


async def test_resolved_at_is_not_stamped_on_non_prod_tasks(db):

    cur = await db.execute(
        "INSERT INTO tasks (title, description) VALUES ('plain feature', 'x')"
    )
    task_id = int(cur.lastrowid or 0)
    await _close(db, task_id)
    rows = await db.execute_fetchall(
        "SELECT resolved_at FROM tasks WHERE id=?", (task_id,)
    )
    assert rows[0]["resolved_at"] is None
    assert (await _clocks(db))["defects"] == 0


async def test_clocks_never_reconstructed(client, db):
    release = await _release(db, SHA_NEW, None)
    await db.execute(
        "UPDATE releases SET deployed_at=datetime('now', '-5 hours') WHERE id=?",
        (release,),
    )
    await db.commit()

    measured = (await _file(client, title="измеримый дефект"))["task"]["id"]
    await db.execute(
        "UPDATE tasks SET detected_at=datetime('now', '-1 hours') WHERE id=?",
        (measured,),
    )
    # No detected_at: updated_at and created_at are right there, and neither
    # may stand in for it.
    blind = (await _file(client, title="без detected_at"))["task"]["id"]
    await db.execute("UPDATE tasks SET detected_at=NULL WHERE id=?", (blind,))
    # Detected but never bound to a release.
    unbound = (await _file(client, title="без релиза"))["task"]["id"]
    await db.execute(
        "UPDATE tasks SET release_id=NULL, detected_at=datetime('now', '-1 hours') "
        "WHERE id=?",
        (unbound,),
    )
    # Still open: no resolved_at yet.
    still_open = (await _file(client, title="ещё открыт"))["task"]["id"]
    await db.execute(
        "UPDATE tasks SET detected_at=datetime('now', '-1 hours') WHERE id=?",
        (still_open,),
    )
    # Closed before the stamp existed: completed, resolved_at empty.
    legacy = (await _file(client, title="закрыт до #916"))["task"]["id"]
    await db.execute(
        "UPDATE tasks SET status='completed', completed_at=datetime('now'), "
        "detected_at=datetime('now', '-1 hours') WHERE id=?",
        (legacy,),
    )
    await db.commit()
    for task_id in (measured, blind, unbound):
        await _close(db, task_id)

    clocks = await _clocks(db)
    assert clocks["defects"] == 5
    ttd, ttr = clocks["time_to_detect"], clocks["time_to_restore"]

    assert ttd["measured"] == 3  # measured, still_open, legacy
    assert ttd["unmeasurable"] == {"no_detected_at": 1, "no_release": 1}
    assert ttd["unmeasurable_total"] == 2
    assert ttd["median_hours"] == 4.0

    assert ttr["measured"] == 2  # measured, unbound
    assert ttr["unmeasurable"] == {
        "no_detected_at": 1,
        "open": 1,
        "closed_without_resolved_at": 1,
    }
    assert ttr["unmeasurable_total"] == 3
    # Only the rows with both facts: the blind row, closed now and created
    # moments ago, would pull the median to ~0 if it were counted.
    assert 0.99 <= ttr["median_hours"] <= 1.01


async def test_clocks_exclude_negative_durations(client, db):
    release = await _release(db, SHA_NEW, None)
    await db.execute(
        "UPDATE releases SET deployed_at=datetime('now', '+1 hours') WHERE id=?",
        (release,),
    )
    await db.commit()
    task_id = (await _file(client))["task"]["id"]
    await _close(db, task_id)
    await db.execute(
        "UPDATE tasks SET detected_at=datetime('now') WHERE id=?", (task_id,)
    )
    await db.commit()

    clocks = await _clocks(db)
    assert clocks["time_to_detect"]["unmeasurable"] == {"detected_before_deploy": 1}
    assert clocks["time_to_detect"]["median_hours"] is None
    await db.execute(
        "UPDATE tasks SET detected_at=datetime('now', '+2 hours') WHERE id=?",
        (task_id,),
    )
    await db.commit()
    clocks = await _clocks(db)
    assert clocks["time_to_restore"]["unmeasurable"] == {"resolved_before_detected": 1}
    assert clocks["time_to_restore"]["median_hours"] is None


async def test_metrics_page_shows_defect_clocks(client, db):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "Часы прод-дефекта" in resp.text
    assert "time-to-restore" in resp.text


async def test_mcp_practice_metrics_names_defect_clocks():
    from hub.mcp_server import hub_practice_metrics

    clocks = {
        "defects": 3,
        "time_to_detect": {
            "measured": 1,
            "median_hours": 4.0,
            "unmeasurable": {"no_release": 2},
            "unmeasurable_total": 2,
        },
        "time_to_restore": {
            "measured": 2,
            "median_hours": 1.5,
            "unmeasurable": {"open": 1},
            "unmeasurable_total": 1,
        },
    }
    with patch("hub.mcp_server._api_get", new_callable=AsyncMock) as get:
        get.return_value = {"since_days": 90, "prod_defect_clocks": clocks}
        result = await hub_practice_metrics()
    text = result.content[0].text
    assert (
        "Prod defects (3): time-to-detect median 4.0h over 1, "
        "unmeasurable 2 (no_release 2); time-to-restore median 1.5h over 2, "
        "unmeasurable 1 (open 1)"
    ) in text


def test_cli_defect_clocks_prints_the_section(capsys):
    import json
    import sys

    from hub import cli

    clocks = {"defects": 0}
    with (
        patch.object(sys, "argv", ["oc-hub", "defect-clocks", "--since-days", "30"]),
        patch.object(
            cli, "_api", return_value={"since_days": 30, "prod_defect_clocks": clocks}
        ) as api,
    ):
        rc = cli.main()
    assert rc in (0, None)
    assert api.call_args.args[:2] == ("GET", "/api/metrics/practices?since_days=30")
    assert json.loads(capsys.readouterr().out) == clocks
