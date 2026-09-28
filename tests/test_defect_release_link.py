"""Defect → release link and the culprit suggestion (#917, feature #907).

Three properties are pinned here:

- the release's membership is a recorded fact — ``pipeline_merges.released_sha``
  stamped at release-merge time (#950) — matched to ``releases.deployed_sha`` by
  one written rule, and rows that match no deploy are counted, never folded in;
- a suggested culprit is a hypothesis: it is stored and shown apart from
  ``caused_by_task_id``, which only an explicit confirmation writes;
- "no candidates" always carries its reason, so an empty list cannot read as
  "nobody broke it".
"""

from __future__ import annotations

import argparse
import json

import aiosqlite
import pytest

from hub import db as hub_db
from hub.repository import DefectPassportError, set_defect_passport
from hub.services import defect_release

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_GHOST = "c" * 40

_MINE = {
    "add_tasks_release_id",
    "idx_tasks_release_id",
    "create_defect_cause_suggestions",
}


async def _default_project(db: aiosqlite.Connection) -> int:
    await db.execute(
        "INSERT OR IGNORE INTO projects (slug, name) VALUES ('default', 'default')"
    )
    rows = await db.execute_fetchall("SELECT id FROM projects WHERE slug='default'")
    return int(rows[0]["id"])


async def _task(
    db: aiosqlite.Connection, title: str, areas: list[str] | None = None
) -> int:
    cur = await db.execute(
        "INSERT INTO tasks (title, description, status, affected_areas) "
        "VALUES (?, '', 'completed', ?)",
        (title, json.dumps(areas or [])),
    )
    return int(cur.lastrowid or 0)


async def _release(
    db: aiosqlite.Connection,
    sha: str,
    *,
    project_id: int | None,
    status: str = "success",
) -> int:
    cur = await db.execute(
        "INSERT INTO releases (project_id, deployed_sha, ref, status, source) "
        "VALUES (?, ?, 'refs/heads/main', ?, 'ci')",
        (project_id, sha, status),
    )
    return int(cur.lastrowid or 0)


async def _merge(
    db: aiosqlite.Connection,
    task_id: int,
    pr: int,
    released_sha: str,
    *,
    project_id: int | None,
) -> None:
    await db.execute(
        "INSERT INTO pipeline_merges "
        "(project_id, pr_number, task_id, merge_sha, released_pr, released_sha) "
        "VALUES (?, ?, ?, ?, 900, ?)",
        (project_id, pr, task_id, f"{pr:040d}", released_sha),
    )


async def _released_world(db: aiosqlite.Connection) -> dict[str, int]:
    """One release carrying two tasks in different areas, plus a defect."""
    pid = await _default_project(db)
    web = await _task(db, "карточка задачи", ["hub/web.py", "hub/templates"])
    docs = await _task(db, "документация", ["docs/"])
    release = await _release(db, SHA_A, project_id=pid)
    await _merge(db, web, 101, SHA_A, project_id=pid)
    await _merge(db, docs, 102, SHA_A, project_id=pid)
    defect = await _task(db, "карточка падает", ["hub/templates/task_detail.html"])
    await db.commit()
    return {"pid": pid, "web": web, "docs": docs, "release": release, "defect": defect}


# --- AC-1 -------------------------------------------------------------------


async def test_candidates_from_release_and_areas(db):
    world = await _released_world(db)
    await set_defect_passport(db, world["defect"], release_id=world["release"])

    suggestion = await defect_release.suggest_causes(db, world["defect"])

    assert [c.task_id for c in suggestion.candidates] == [world["web"]]
    only = suggestion.candidates[0]
    # The reason is the overlap itself, not a score: the reader can check it.
    assert only.overlap == ["hub/templates/task_detail.html ↔ hub/templates"]
    assert suggestion.reason == ""
    assert suggestion.release_id == world["release"]


# --- AC-2 -------------------------------------------------------------------


async def test_suggestion_is_not_a_fact(client, db):
    world = await _released_world(db)

    resp = await client.post(
        f"/api/tasks/{world['defect']}/refine",
        json={"release_id": world["release"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The link is a fact; the culprit is not.
    assert body["release_id"] == world["release"]
    assert body["caused_by_task_id"] is None

    card = (await client.get(f"/api/tasks/{world['defect']}")).json()
    assert card["caused_by_task_id"] is None
    shown = card["cause_suggestion"]
    assert [c["task_id"] for c in shown["candidates"]] == [world["web"]]
    assert shown["candidates"][0]["confirmed"] is False

    stored = await db.execute_fetchall(
        "SELECT candidate_task_id, release_id FROM defect_cause_suggestions "
        "WHERE defect_task_id=?",
        (world["defect"],),
    )
    assert [(r["candidate_task_id"], r["release_id"]) for r in stored] == [
        (world["web"], world["release"])
    ]

    # Only an explicit confirmation writes caused_by; the hypothesis stays.
    resp = await client.post(
        f"/api/tasks/{world['defect']}/refine",
        json={"caused_by_task_id": world["web"]},
    )
    assert resp.status_code == 200, resp.text
    card = (await client.get(f"/api/tasks/{world['defect']}")).json()
    assert card["caused_by_task_id"] == world["web"]
    assert card["cause_suggestion"]["candidates"][0]["confirmed"] is True


# --- AC-3 -------------------------------------------------------------------


async def test_release_membership_uses_the_matching_rule(db):
    pid = await _default_project(db)
    matched = await _task(db, "попал в выкат", ["hub/web.py"])
    manual = await _task(db, "ручной мерж", ["hub/web.py"])
    returned = await _task(db, "возврат develop←main", ["hub/web.py"])
    short = await _task(db, "короткий sha", ["hub/web.py"])
    release = await _release(db, SHA_A, project_id=pid)
    # Case and surrounding space are spelling, not a different commit.
    await _merge(db, matched, 1, f"  {SHA_A.upper()} ", project_id=pid)
    await _merge(db, manual, 2, SHA_GHOST, project_id=pid)
    await _merge(db, returned, 3, SHA_B, project_id=pid)
    # A prefix is not a match: "starts with" is how two commits get confused.
    await _merge(db, short, 4, SHA_A[:12], project_id=pid)
    await db.commit()

    membership = await defect_release.release_membership(db, release)

    assert membership.task_ids == [matched]
    assert membership.unmatched_rows == 3

    defect = await _task(db, "дефект", ["hub/web.py"])
    await set_defect_passport(db, defect, release_id=release)
    suggestion = await defect_release.suggest_causes(db, defect)
    assert [c.task_id for c in suggestion.candidates] == [matched]
    assert suggestion.unmatched_rows == 3


async def test_membership_ignores_other_projects(db):
    pid = await _default_project(db)
    cur = await db.execute(
        "INSERT INTO projects (slug, name, status) VALUES ('other', 'other', 'active')"
    )
    other = int(cur.lastrowid or 0)
    ours = await _task(db, "наш", ["hub/web.py"])
    theirs = await _task(db, "чужой", ["hub/web.py"])
    release = await _release(db, SHA_A, project_id=pid)
    await _merge(db, ours, 1, SHA_A, project_id=pid)
    await _merge(db, theirs, 2, SHA_A, project_id=other)
    await db.commit()

    membership = await defect_release.release_membership(db, release)

    assert membership.task_ids == [ours]


# --- AC-4 -------------------------------------------------------------------


async def test_no_candidates_names_the_reason(db):
    world = await _released_world(db)

    # No area declared on the defect.
    blind = await _task(db, "дефект без области", [])
    await set_defect_passport(db, blind, release_id=world["release"])
    suggestion = await defect_release.suggest_causes(db, blind)
    assert suggestion.candidates == []
    assert suggestion.reason == defect_release.REASON_NO_AREAS

    # A release nobody stamped: its membership is unknown, not empty.
    bare = await _release(db, SHA_B, project_id=world["pid"])
    lost = await _task(db, "дефект в пустом выкате", ["hub/web.py"])
    await set_defect_passport(db, lost, release_id=bare)
    suggestion = await defect_release.suggest_causes(db, lost)
    assert suggestion.candidates == []
    assert suggestion.reason == defect_release.REASON_NO_MEMBERS

    # Both missing: both named.
    both = await _task(db, "ни области, ни состава", [])
    await set_defect_passport(db, both, release_id=bare)
    suggestion = await defect_release.suggest_causes(db, both)
    assert suggestion.reason == (
        f"{defect_release.REASON_NO_AREAS}; {defect_release.REASON_NO_MEMBERS}"
    )

    # Known membership, no overlap: still a named reason, not silence.
    elsewhere = await _task(db, "дефект в CI", [".github/workflows/ci.yml"])
    await set_defect_passport(db, elsewhere, release_id=world["release"])
    suggestion = await defect_release.suggest_causes(db, elsewhere)
    assert suggestion.candidates == []
    assert suggestion.reason == defect_release.REASON_NO_OVERLAP

    # No release on the defect at all.
    unlinked = await _task(db, "дефект без релиза", ["hub/web.py"])
    suggestion = await defect_release.suggest_causes(db, unlinked)
    assert suggestion.candidates == []
    assert suggestion.reason == defect_release.REASON_NO_RELEASE


# --- the link itself --------------------------------------------------------


async def test_release_link_must_resolve(db):
    world = await _released_world(db)
    with pytest.raises(DefectPassportError, match="release #9999"):
        await set_defect_passport(db, world["defect"], release_id=9999)

    failed = await _release(db, SHA_B, project_id=world["pid"], status="failure")
    with pytest.raises(DefectPassportError, match="failure"):
        await set_defect_passport(db, world["defect"], release_id=failed)

    rows = await db.execute_fetchall(
        "SELECT release_id FROM tasks WHERE id=?", (world["defect"],)
    )
    assert rows[0]["release_id"] is None


async def test_release_of_another_project_is_refused(db):
    world = await _released_world(db)
    cur = await db.execute(
        "INSERT INTO projects (slug, name, status) VALUES ('other', 'other', 'active')"
    )
    foreign = await _release(db, SHA_B, project_id=int(cur.lastrowid or 0))
    with pytest.raises(DefectPassportError, match="project"):
        await set_defect_passport(db, world["defect"], release_id=foreign)


async def test_unknown_release_is_a_4xx_over_api(client, db):
    world = await _released_world(db)
    resp = await client.post(
        f"/api/tasks/{world['defect']}/refine", json={"release_id": 9999}
    )
    assert resp.status_code in (400, 422), resp.text
    assert "release #9999" in resp.text


async def test_card_without_release_carries_no_suggestion(client, db):
    world = await _released_world(db)
    card = (await client.get(f"/api/tasks/{world['defect']}")).json()
    assert card["release_id"] is None
    assert card["cause_suggestion"] is None


async def test_suggestions_are_recorded_once(db):
    world = await _released_world(db)
    await set_defect_passport(db, world["defect"], release_id=world["release"])
    await defect_release.record_suggestions(db, world["defect"])
    await defect_release.record_suggestions(db, world["defect"])
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS n FROM defect_cause_suggestions WHERE defect_task_id=?",
        (world["defect"],),
    )
    assert rows[0]["n"] == 1


# --- surfaces ---------------------------------------------------------------


def test_cli_refine_builds_release_payload():
    from hub.cli import _build_refine_payload

    assert _build_refine_payload(argparse.Namespace(release_id=7)) == {"release_id": 7}


def test_cli_refine_parser_accepts_release_id():
    from hub.cli import build_parser

    args = build_parser().parse_args(["refine", "5", "--release-id", "7"])
    assert args.release_id == 7


def test_mcp_refine_publishes_release_id():
    import inspect

    from hub.mcp_server import hub_prepare_developer_task, hub_refine_task

    assert "release_id" in inspect.signature(hub_refine_task).parameters
    # Preparing a statement is not where a defect gets its release.
    assert "release_id" not in inspect.signature(hub_prepare_developer_task).parameters


def test_mcp_status_names_the_suggestion():
    from hub.mcp_server import _cause_suggestion_line

    line = _cause_suggestion_line(
        {
            "release_id": 3,
            "cause_suggestion": {
                "release_id": 3,
                "candidates": [
                    {
                        "task_id": 11,
                        "title": "x",
                        "overlap": ["a ↔ a"],
                        "confirmed": False,
                    }
                ],
                "reason": "",
                "unmatched_rows": 2,
            },
        }
    )
    assert "#11" in line and "гипотеза" in line and "2" in line
    no = _cause_suggestion_line(
        {
            "release_id": 3,
            "cause_suggestion": {
                "release_id": 3,
                "candidates": [],
                "reason": defect_release.REASON_NO_AREAS,
                "unmatched_rows": 0,
            },
        }
    )
    assert defect_release.REASON_NO_AREAS in no
    assert _cause_suggestion_line({"release_id": None}) == ""


# --- migrations -------------------------------------------------------------


async def _fresh(migrations) -> aiosqlite.Connection:
    from unittest.mock import patch

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.executescript(hub_db._SCHEMA)
    try:
        with patch.object(hub_db, "_MIGRATIONS", migrations):
            await hub_db._migrate(conn)
    except BaseException:
        # A migration that raises must not leave the aiosqlite worker thread
        # alive: the test would fail and then hang the process at exit.
        await conn.close()
        raise
    return conn


async def test_migration_on_clean_and_filled_db_is_idempotent():
    names = [name for name, _ in hub_db._MIGRATIONS]
    assert _MINE <= set(names)

    clean = await _fresh(hub_db._MIGRATIONS)
    try:
        await hub_db._migrate(clean)
        cols = {
            r["name"] for r in await clean.execute_fetchall("PRAGMA table_info(tasks)")
        }
        assert "release_id" in cols
        tables = {
            r["name"]
            for r in await clean.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "defect_cause_suggestions" in tables
    finally:
        await clean.close()

    before = [m for m in hub_db._MIGRATIONS if m[0] not in _MINE]
    filled = await _fresh(before)
    try:
        await filled.execute(
            "INSERT INTO tasks (title, description, status) VALUES ('old', '', 'open')"
        )
        await filled.commit()
        await hub_db._migrate(filled)
        await filled.execute(
            "DELETE FROM _migrations WHERE name IN (?, ?, ?)", tuple(_MINE)
        )
        await hub_db._migrate(filled)
        rows = await filled.execute_fetchall("SELECT release_id FROM tasks")
        assert [r["release_id"] for r in rows] == [None]
    finally:
        await filled.close()


async def test_web_passport_shows_release_and_hypothesis(client, db):
    """Finding a73039f3: the card shows the release and the hypothesis apart
    from caused_by, and a release alone counts as a filled passport."""
    world = await _released_world(db)
    resp = await client.post(
        f"/api/tasks/{world['defect']}/refine", json={"release_id": world["release"]}
    )
    assert resp.status_code == 200, resp.text

    page = (await client.get(f"/tasks/{world['defect']}")).text

    assert f'data-defect-release="{world["release"]}"' in page
    assert SHA_A[:12] in page
    hypothesis = page.split("data-cause-suggestion", 1)[1].split("</dd>", 1)[0]
    assert f"/tasks/{world['web']}" in hypothesis
    assert "hub/templates/task_detail.html ↔ hub/templates" in hypothesis
    assert "не подтверждено" in page.split("data-cause-suggestion", 1)[0][-200:]
    # caused_by stays the fact column: still empty.
    assert "не установлено" in page

    blind = await _task(db, "дефект без области", [])
    await db.commit()
    resp = await client.post(
        f"/api/tasks/{blind}/refine", json={"release_id": world["release"]}
    )
    assert resp.status_code == 200, resp.text
    page = (await client.get(f"/tasks/{blind}")).text
    # Not a bug by work_type: only release_id makes the passport show at all.
    assert "data-defect-passport" in page
    assert f"кандидатов нет: {defect_release.REASON_NO_AREAS}" in page


async def test_projectless_release_carries_the_default_projects_merges(db):
    """#915: ci.yml reports deploys without a project, the gate stamps merges
    of the default project — on prod every release row is project-less, so a
    strict ``project_id IS ?`` found no membership at all."""
    pid = await _default_project(db)
    cur = await db.execute(
        "INSERT INTO projects (slug, name, status) VALUES ('other', 'other', 'active')"
    )
    other = int(cur.lastrowid or 0)
    ours = await _task(db, "наш", ["hub/web.py"])
    theirs = await _task(db, "чужой", ["hub/web.py"])
    release = await _release(db, SHA_A, project_id=None)
    await _merge(db, ours, 1, SHA_A, project_id=pid)
    await _merge(db, theirs, 2, SHA_A, project_id=other)
    await db.commit()

    membership = await defect_release.release_membership(db, release)

    assert membership.task_ids == [ours]
    assert membership.unmatched_rows == 0
