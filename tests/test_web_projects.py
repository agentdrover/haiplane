from __future__ import annotations

import asyncio
from httpx import AsyncClient

from tests.web_support import (
    _form_carries_policy_version,  # noqa: F401
    _card,
)


# ---------------------------------------------------------------------------
# Project forms: create / edit / archive / activate (#344)
# ---------------------------------------------------------------------------


async def test_web_create_project_form(client: AsyncClient, db):
    # AC-1: form submit creates the project; it shows up in the table.
    from hub.db import seed_default_project

    await seed_default_project(db)
    resp = await client.post(
        "/projects/web-create",
        data={
            "slug": "calc-kids",
            "name": "Calc Kids",
            "repo": "mrPDA/calc-kids",
            "workspace_path": "/srv/calc",
            "default_branch": "master",
            "default_branch_policy": '{"release_base": "main"}',
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/projects"

    page = await client.get("/projects")
    assert "calc-kids" in page.text
    assert "master" in page.text

    from hub import repository as repo_module

    row = await repo_module.get_project_by_slug(db, "calc-kids")
    assert row is not None
    assert row["status"] == "active"  # web session = human path
    assert '"release_base"' in row["default_branch_policy"]


async def test_web_create_project_duplicate_slug(client: AsyncClient, db):
    # AC-2: duplicate slug lands as a form error, not a 500.
    from hub.db import seed_default_project

    await seed_default_project(db)
    resp = await client.post(
        "/projects/web-create",
        data={"slug": "default", "name": "Dup"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error=" in resp.headers["location"]

    page = await client.get(resp.headers["location"])
    assert page.status_code == 200
    assert "already exists" in page.text


async def test_web_create_project_bad_policy_json(client: AsyncClient, db):
    # AC-2: malformed policy JSON is a form error, not a 500.
    # #351 AC-3: the project must NOT be created and the error must name policy.
    from hub import repository as repo_module

    resp = await client.post(
        "/projects/web-create",
        data={"slug": "poly", "name": "Poly", "default_branch_policy": "{oops"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error=" in resp.headers["location"]
    assert await repo_module.get_project_by_slug(db, "poly") is None

    page = await client.get(resp.headers["location"])
    assert "policy" in page.text


async def test_web_create_project_bad_slug(client: AsyncClient, db):
    # #351 AC-3: no project row appears and the error names the slug field.
    from hub import repository as repo_module

    resp = await client.post(
        "/projects/web-create",
        data={"slug": "Bad Slug!", "name": "X"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error=" in resp.headers["location"]
    rows = await repo_module.list_projects(db, include_archived=True)
    assert all(r["name"] != "X" for r in rows)

    page = await client.get(resp.headers["location"])
    assert "slug" in page.text


async def test_web_create_project_deeply_nested_policy_json(client: AsyncClient, db):
    # #350 AC-1: '['*20000 makes json.loads raise RecursionError on py<3.13 —
    # must land as a form error redirect, never a 500.
    from hub import repository as repo_module

    resp = await client.post(
        "/projects/web-create",
        data={"slug": "deep", "name": "Deep", "default_branch_policy": "[" * 20000},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error=" in resp.headers["location"]
    assert await repo_module.get_project_by_slug(db, "deep") is None


async def test_web_edit_project_deeply_nested_policy_json(client: AsyncClient, db):
    from hub import repository as repo_module

    pid = await repo_module.create_project(db, slug="deep-ed", name="DeepEd")
    await db.commit()

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"name": "Changed", "default_branch_policy": "[" * 20000},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error=" in resp.headers["location"]
    row = await repo_module.get_project(db, pid)
    assert row["name"] == "DeepEd"  # nothing applied


async def test_web_create_project_concurrent_duplicate_slug(client: AsyncClient, db):
    # #350 AC-2: double-submit race — both requests pass the slug check before
    # the first INSERT unless check+insert is serialized; the loser must get a
    # 409-driven form error, not an IntegrityError 500.
    from hub import repository as repo_module

    def post():
        return client.post(
            "/projects/web-create",
            data={"slug": "race", "name": "Race"},
            follow_redirects=False,
        )

    r1, r2 = await asyncio.gather(post(), post())
    assert {r1.status_code, r2.status_code} == {303}
    locations = sorted([r1.headers["location"], r2.headers["location"]])
    assert locations[0] == "/projects"
    assert "project_error=" in locations[1]

    rows = await repo_module.list_projects(db, include_archived=True)
    assert sum(1 for r in rows if r["slug"] == "race") == 1


async def test_web_edit_project(client: AsyncClient, db):
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    pid = await repo_module.create_project(
        db, slug="edit-me", name="Before", repo_name=""
    )
    await db.commit()

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={
            "name": "After",
            "repo": "mrPDA/after",
            "workspace_path": "",
            "default_branch": "trunk",
            "default_branch_policy": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = await repo_module.get_project(db, pid)
    assert row["name"] == "After"
    assert row["repo"] == "mrPDA/after"
    assert row["default_branch"] == "trunk"


async def test_web_archive_and_unarchive_project(client: AsyncClient, db):
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    pid = await repo_module.create_project(db, slug="arch", name="Arch")
    await db.commit()

    resp = await client.post(
        f"/projects/{pid}/web-archive",
        data={"archived": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = await repo_module.get_project(db, pid)
    assert row["archived"] == 1

    resp = await client.post(
        f"/projects/{pid}/web-archive",
        data={"archived": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = await repo_module.get_project(db, pid)
    assert row["archived"] == 0


async def test_web_activate_pending_project(client: AsyncClient, db):
    # #345 loop closed in UI: pending proposal → Activate button → active.
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    pid = await repo_module.create_project(
        db, slug="pend", name="Pending", status="pending"
    )
    await db.commit()

    page = await client.get("/projects")
    # The badge says "ждёт активации" since #623: the section speaks Russian, and
    # this state stayed English only because no project on prod is ever pending,
    # so nobody could see it. What is pinned is that the state is VISIBLE and its
    # action is offered — not which language the label happens to be in.
    assert "ждёт активации" in page.text and "web-activate" in page.text

    resp = await client.post(f"/projects/{pid}/web-activate", follow_redirects=False)
    assert resp.status_code == 303
    row = await repo_module.get_project(db, pid)
    assert row["status"] == "active"


async def test_web_edit_project_name_too_long_is_form_error(client: AsyncClient, db):
    # #351 AC-4: ProjectPatch max_length=200 — the ValidationError branch of
    # web-edit must redirect with project_error, never 500.
    from hub import repository as repo_module

    pid = await repo_module.create_project(db, slug="longname", name="Long")
    await db.commit()

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"name": "x" * 201},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error=" in resp.headers["location"]
    row = await repo_module.get_project(db, pid)
    assert row["name"] == "Long"


async def test_web_edit_project_bad_policy_json(client: AsyncClient, db):
    # Review finding (#344): the edit route has its own _parse_policy_form
    # call — malformed JSON must be a form error there too, row untouched.
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    pid = await repo_module.create_project(db, slug="polyed", name="PolyEd")
    await db.commit()

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"name": "Hacked", "default_branch_policy": "{oops"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error=" in resp.headers["location"]
    row = await repo_module.get_project(db, pid)
    assert row["name"] == "PolyEd"  # ничего не применилось


async def test_web_edit_project_policy_roundtrip(client: AsyncClient, db):
    # Review finding (#344): pin 'empty textarea = keep policy' and the
    # stored-JSON → textarea → resubmit round-trip.
    import json as json_module

    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    await client.post(
        "/projects/web-create",
        data={
            "slug": "rt",
            "name": "RT",
            "default_branch_policy": '{"release_base": "main"}',
        },
        follow_redirects=False,
    )
    row = await repo_module.get_project_by_slug(db, "rt")
    pid = row["id"]

    page = await client.get("/projects")
    assert "release_base" in page.text  # policy prefilled in the edit textarea

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"default_branch_policy": '{"release_base": "trunk"}'},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = await repo_module.get_project(db, pid)
    assert json_module.loads(row["default_branch_policy"]) == {"release_base": "trunk"}

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"name": "RT2", "default_branch_policy": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = await repo_module.get_project(db, pid)
    assert row["name"] == "RT2"
    assert json_module.loads(row["default_branch_policy"]) == {
        "release_base": "trunk"
    }  # пусто = не менять


async def test_web_edit_project_empty_required_fields_keep_values(
    client: AsyncClient, db
):
    # Review finding (#344): blank name/default_branch mean 'keep', while
    # blank repo/workspace_path intentionally clear.
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    pid = await repo_module.create_project(
        db,
        slug="keep",
        name="Keeper",
        repo_name="mrPDA/keep",
        workspace_path="/srv/keep",
        default_branch="trunk",
    )
    await db.commit()

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={
            "name": "",
            "default_branch": "",
            "repo": "",
            "workspace_path": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = await repo_module.get_project(db, pid)
    assert row["name"] == "Keeper"  # пустое обязательное — не меняем
    assert row["default_branch"] == "trunk"
    assert row["repo"] == ""  # пустое необязательное — очищаем
    assert row["workspace_path"] == ""


async def test_web_provision_button_and_status(client: AsyncClient, db):
    # AC-1 (#348): Provision button for repo projects; status lands on page.
    from unittest.mock import AsyncMock

    from hub import repository as repo_module
    from hub.db import seed_default_project
    from hub.integrations.registry import plugins

    await seed_default_project(db)
    pid = await repo_module.create_project(
        db,
        slug="provui",
        name="ProvUI",
        repo_name="mrPDA/provui",
        workspace_path="/srv/provui",
    )
    await db.commit()

    page = await client.get("/projects")
    assert f"/projects/{pid}/web-provision" in page.text  # кнопка есть
    assert "ws" not in page.text or "ws ok" not in page.text

    plugins.git_ops.clone_repo = AsyncMock(return_value=(True, "cloned"))
    resp = await client.post(f"/projects/{pid}/web-provision", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/projects"
    page = await client.get("/projects")
    # #622 removed the green "ws ok" from the header: "everything is fine" is not
    # news. The provision BUTTON below is what this test is really about.
    assert "ws&nbsp;ok" not in page.text

    plugins.git_ops.clone_repo = AsyncMock(
        return_value=(False, "remote not accessible: no key")
    )
    resp = await client.post(f"/projects/{pid}/web-provision", follow_redirects=False)
    assert resp.status_code == 303
    assert "project_error=" in resp.headers["location"]
    page = await client.get(resp.headers["location"])
    assert "remote not accessible" in page.text
    # #1188: отказ, у которого есть проект, живёт у его карточки — как и
    # отказ формы. Инвариант ввела та же задача, и этот вызов её нарушал.
    assert "project_error_id=" in resp.headers["location"]
    assert "project-error-note" in _card(page.text, "provui"), (
        "нота обязана стоять у карточки своего проекта, а не под всеми"
    )
    page = await client.get("/projects")
    # #622: the badge is Russian now and the CAUSE is visible text — it used to
    # live only in a title attribute, which a phone never shows.
    assert "workspace: ошибка" in page.text
    assert "remote not accessible" in page.text, (
        "the reason must be readable on the card, not only on the redirect page"
    )


async def test_web_provision_hidden_without_repo(client: AsyncClient, db):
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    pid = await repo_module.create_project(db, slug="norepo-ui", name="NoRepo")
    await db.commit()
    page = await client.get("/projects")
    assert f"/projects/{pid}/web-provision" not in page.text


# ---------------------------------------------------------------------------
# Epics grouped by project, ordered by real activity, finished ones collapsed (#570)
# ---------------------------------------------------------------------------


async def _epic(client: AsyncClient, title: str, project: str | None = None) -> int:
    body: dict = {"title": title, "task_type": "epic"}
    if project:
        body["project"] = project
    resp = await client.post("/api/tasks", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def test_epic_list_groups_by_project_and_keeps_unassigned(
    client: AsyncClient, db
):
    # AC-1 (#570): grouping only became worth building today — until the epics
    # were split across products, 30 of 31 sat in "default" and a group-by would
    # have divided by a constant. An epic with project_id NULL must land in a
    # NAMED group: the column has no NOT NULL constraint, so "it cannot happen"
    # is not an argument.
    from hub.db import seed_default_project
    from hub.services.dashboard import UNASSIGNED_PROJECT, get_epic_board

    await seed_default_project(db)
    await db.execute(
        "INSERT INTO projects (slug, name, status) VALUES ('proj-a', 'A', 'active')"
    )
    await db.commit()
    grouped = await _epic(client, "Epic in A", project="proj-a")
    homeless = await _epic(client, "Epic with no project")
    await db.execute("UPDATE tasks SET project_id=NULL WHERE id=?", (homeless,))
    await db.commit()

    board = await get_epic_board(db)
    by_project = {g["project"]: [e.id for e in g["epics"]] for g in board["groups"]}

    assert grouped in by_project.get("proj-a", []), (
        "the epic must sit under its project"
    )
    assert homeless in by_project.get(UNASSIGNED_PROJECT, []), (
        "an epic without a project must be named, not dropped from the view"
    )
    assert board["live_total"] == sum(len(v) for v in by_project.values()), (
        "every live epic belongs to exactly one group"
    )


async def test_epics_are_ordered_by_last_subtree_activity(client: AsyncClient, db):
    # AC-2 (#570): before this the order was position ASC, id DESC — and since
    # position is 0 on every epic, effectively "newest id first", which put a
    # June-dead epic above yesterday's work.
    from hub.services.dashboard import get_epic_board

    older = await _epic(client, "Older id, fresh work")
    newer = await _epic(client, "Newer id, stale work")
    for task_id, when in (
        (older, "2026-08-09 12:00:00"),
        (newer, "2026-07-01 12:00:00"),
    ):
        child = (
            await client.post(
                "/api/tasks",
                json={
                    "title": f"child of {task_id}",
                    "task_type": "feature",
                    "parent_id": task_id,
                },
            )
        ).json()["id"]
        await client.post(
            f"/api/tasks/{child}/updates",
            json={"agent": "dev", "kind": "status", "content": "worked"},
        )
        await db.execute(
            "UPDATE task_updates SET created_at=? WHERE task_id=?", (when, child)
        )
        await db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (when, child))
    await db.commit()

    order = [e.id for g in (await get_epic_board(db))["groups"] for e in g["epics"]]
    assert order.index(older) < order.index(newer), (
        f"fresh work must outrank a bigger id: got {order}"
    )


async def test_an_administrative_touch_is_not_activity(client: AsyncClient, db):
    # AC-4 (#570). The obvious sort key was MAX(updated_at), and it was a trap:
    # updated_at is bumped by ANY write (#616). On production, ordering by it
    # floated #182, #192, #209, #371 and #394 to the top — the five epics whose
    # project had just been reassigned twenty minutes earlier. A rename must not
    # look like work.
    from hub.services.dashboard import get_epic_board

    worked_on = await _epic(client, "Someone reported progress here")
    just_renamed = await _epic(client, "Only a field was edited here")

    child = (
        await client.post(
            "/api/tasks",
            json={"title": "c", "task_type": "feature", "parent_id": worked_on},
        )
    ).json()["id"]
    await client.post(
        f"/api/tasks/{child}/updates",
        json={"agent": "dev", "kind": "status", "content": "real work"},
    )
    await db.execute(
        "UPDATE task_updates SET created_at='2026-08-05 10:00:00' WHERE task_id=?",
        (child,),
    )
    await db.execute(
        "UPDATE tasks SET updated_at='2026-08-05 10:00:00' WHERE id IN (?,?)",
        (child, worked_on),
    )
    # The administrative touch is LATER in wall-clock time, which is exactly what
    # makes this test meaningful: only the feed distinguishes them.
    await db.execute(
        "UPDATE tasks SET updated_at='2026-08-10 18:00:00' WHERE id=?", (just_renamed,)
    )
    await db.commit()

    order = [e.id for g in (await get_epic_board(db))["groups"] for e in g["epics"]]
    assert order.index(worked_on) < order.index(just_renamed), (
        f"a touched-but-idle epic must not outrank one with real work: {order}"
    )

    # An epic with no feed entry anywhere has no activity to report, so it sorts
    # LAST — and is still in the list. The first design borrowed updated_at for
    # these rows; this assertion is what killed it, because the borrowed date is
    # precisely the administrative touch above.
    assert just_renamed in order, "no activity must not mean no place in the list"
    assert order[-1] == just_renamed


async def test_done_epics_are_collapsed_not_hidden(client: AsyncClient, db):
    # AC-3 (#570): #569 filtered finished epics out entirely, so there was no way
    # back to them. The count and the contents come from ONE query — this morning
    # #571 shipped a counter that disagreed with its own link, and the whole point
    # of a number is that it can be trusted.
    from hub.services.dashboard import get_epic_board

    live = await _epic(client, "Still working")
    done = await _epic(client, "All finished")
    await db.execute("UPDATE tasks SET status='completed' WHERE id=?", (done,))
    await db.commit()

    board = await get_epic_board(db)
    live_ids = [e.id for g in board["groups"] for e in g["epics"]]
    done_ids = [e.id for e in board["done"]]

    assert live in live_ids and done not in live_ids
    assert done in done_ids, "a finished epic must remain reachable"
    assert board["done_total"] == len(done_ids), (
        "the number must be the length of what the block contains"
    )
    # Exact complement: no epic may fall out of both lists.
    all_rows = await db.execute_fetchall(
        "SELECT id FROM tasks WHERE task_type='epic' AND archived=0"
    )
    assert {r["id"] for r in all_rows} == set(live_ids) | set(done_ids), (
        "an epic matching neither condition would vanish from the UI entirely"
    )

    page = await client.get("/partials/epics")
    assert "Доделанные эпики: 1" in page.text
    assert f'href="/tasks/{done}"' in page.text.replace("'", '"'), (
        "collapsed means present in the markup, not omitted"
    )


# ---------------------------------------------------------------------------
# /projects answers "what is happening", not "where to push" (#567)
# ---------------------------------------------------------------------------


async def test_status_sets_are_derived_not_retyped():
    # AC-5 (#567): this codebase has already unified two hand-copied status
    # lists — terminal statuses in #571 and epic liveness in #570 — and one of
    # them shipped WRONG (it forgot `failed`). So "in flight" is subtraction, and
    # a status added to the enum later must land in some set or fail here.
    from hub.models import (
        ACTIVE_STATUSES,
        AWAITING_HUMAN_STATUSES,
        FINAL_STATUSES,
        IN_FLIGHT_STATUSES,
        QUEUED_STATUSES,
        TaskStatus,
    )

    # #619 took `open` out of "in flight" — approved-but-untouched is a queue, not
    # work. This assertion FAILED when that happened, which is the whole reason it
    # exists: `open` had to be given its own named set instead of falling into the
    # gap that hid `draft` until #567. Updated deliberately, not relaxed.
    assert IN_FLIGHT_STATUSES == (
        ACTIVE_STATUSES - AWAITING_HUMAN_STATUSES - QUEUED_STATUSES
    )
    assert TaskStatus.open in QUEUED_STATUSES
    assert TaskStatus.open not in IN_FLIGHT_STATUSES, (
        "an approved task nobody started is not work in progress"
    )
    assert not (IN_FLIGHT_STATUSES & AWAITING_HUMAN_STATUSES)
    assert not (IN_FLIGHT_STATUSES & QUEUED_STATUSES)
    assert not (AWAITING_HUMAN_STATUSES & FINAL_STATUSES)
    unclassified = (
        set(TaskStatus)
        - AWAITING_HUMAN_STATUSES
        - IN_FLIGHT_STATUSES
        - QUEUED_STATUSES
        - FINAL_STATUSES
    )
    assert not unclassified, f"a status belonging to no set: {unclassified}"
    # draft is the one that was outside both pre-existing sets, and the one the
    # original statement forgot. Pin it so it cannot quietly leave again.
    assert TaskStatus.draft in AWAITING_HUMAN_STATUSES


async def _project(db, slug: str, name: str, status: str = "active") -> int:
    cur = await db.execute(
        "INSERT INTO projects (slug, name, status) VALUES (?,?,?)", (slug, name, status)
    )
    await db.commit()
    return cur.lastrowid


async def test_projects_page_lists_only_live_epics_per_project(client: AsyncClient, db):
    # AC-1 (#567): the card shows work, so a finished epic has no place on it —
    # it is reachable through the collapsed block delivered by #570.
    from hub.db import seed_default_project
    from hub.services.dashboard import get_project_cards

    await seed_default_project(db)
    pid = await _project(db, "proj-live", "Live Project")
    live = (
        await client.post(
            "/api/tasks",
            json={"title": "Live epic", "task_type": "epic", "project": "proj-live"},
        )
    ).json()["id"]
    done = (
        await client.post(
            "/api/tasks",
            json={"title": "Done epic", "task_type": "epic", "project": "proj-live"},
        )
    ).json()["id"]
    await db.execute("UPDATE tasks SET status='completed' WHERE id=?", (done,))
    await db.commit()

    card = next(c for c in await get_project_cards(db) if c["project"]["id"] == pid)
    ids = [e.id for e in card["live_epics"]]
    assert ids == [live], f"only live epics belong on the card, got {ids}"

    page = await client.get("/projects")
    assert page.status_code == 200
    assert "Live Project" in page.text


async def test_project_card_counts_only_what_waits_for_a_human(client: AsyncClient, db):
    # AC-2 (#567). Two corrections live in this test, both found by fact:
    # (1) draft belongs in the count — draft→open is human-only, and production
    #     holds 39 drafts against 2 in the three statuses first named;
    # (2) a review with review_job_id set is a HEADLESS review owned by the
    #     poller, not a person waiting — the exclusion
    #     list_stale_by_status(require_null_review_job=True) already makes.
    from hub.db import seed_default_project
    from hub.services.dashboard import get_project_cards

    await seed_default_project(db)
    pid = await _project(db, "proj-count", "Counting")
    epic = (
        await client.post(
            "/api/tasks",
            json={"title": "E", "task_type": "epic", "project": "proj-count"},
        )
    ).json()["id"]

    async def _child(
        title: str, status: str, *, job: str | None = None, archived: int = 0
    ) -> int:
        tid = (
            await client.post(
                "/api/tasks",
                json={"title": title, "task_type": "feature", "parent_id": epic},
            )
        ).json()["id"]
        await db.execute(
            "UPDATE tasks SET status=?, review_job_id=?, archived=? WHERE id=?",
            (status, job, archived, tid),
        )
        return tid

    await _child("a draft", "draft")
    await _child("a question", "needs_info")
    await _child("a decision", "needs_decision")
    await _child("a human review", "review")
    await _child("a headless review", "review", job="job-abc")
    await _child("archived draft", "draft", archived=1)
    await _child("running", "running")
    await _child("in ci", "ci_check")
    await db.commit()

    card = next(c for c in await get_project_cards(db) if c["project"]["id"] == pid)
    assert card["awaiting_human"] == 4, (
        "draft + needs_info + needs_decision + human review; NOT the headless "
        "one and NOT the archived one"
    )
    assert card["drafts"] == 1, "the label needs the draft share to be honest"
    assert card["in_flight"] == 2, (
        "running + ci_check only. The EPIC is `open` too, and counting it would "
        "add a permanent +1 to every project — an epic is a container, not work "
        "in flight. This assertion is what forced that rule out into the open."
    )

    page = await client.get("/projects")
    # #622 turned the numbers into the dashboard's chip pattern: label and value
    # are separate elements. The COUNT itself is asserted on the card above.
    assert ">Ждёт вас<" in page.text and ">4<" in page.text
    assert "черновиков: 1" in page.text, (
        "a bare number reads as urgency; the label must name its composition"
    )


async def test_project_card_with_no_live_work_says_so(client: AsyncClient, db):
    # AC-3 (#567): an empty card reads as "broken", not as "nothing to do" —
    # the same rule #615 settled: silence is not an answer.
    from hub.db import seed_default_project

    await seed_default_project(db)
    await _project(db, "proj-quiet", "Quiet Project")

    page = await client.get("/projects")
    assert page.status_code == 200
    assert "Quiet Project" in page.text
    assert "Живой работы нет" in page.text


# ---------------------------------------------------------------------------
# Admin fields live with their project, and the second list is gone (#568)
# ---------------------------------------------------------------------------


async def test_projects_page_keeps_every_admin_action_available(
    client: AsyncClient, db
):
    # AC-1 (#568). The statement said "all five actions available"; spec review
    # showed that reading it literally would STRIP the guards, because the
    # template hides provision without a repo, activate outside pending, and
    # archive for `default`. So the criterion is "each action available under its
    # OWN current conditions" — and the proof is that six existing tests pass
    # untouched. This test pins the guards themselves.
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    with_repo = await repo_module.create_project(
        db, slug="has-repo", name="Has Repo", repo_name="owner/repo"
    )
    no_repo = await repo_module.create_project(db, slug="no-repo", name="No Repo")
    await db.execute("UPDATE projects SET status='pending' WHERE id=?", (no_repo,))
    await db.commit()

    page = (await client.get("/projects")).text

    assert f"/projects/{with_repo}/web-provision" in page, (
        "repo present → provision offered"
    )
    assert f"/projects/{no_repo}/web-provision" not in page, (
        "no repo → provision must stay hidden (test_web_provision_hidden_without_repo)"
    )
    assert f"/projects/{no_repo}/web-activate" in page, "pending → activate offered"
    assert f"/projects/{with_repo}/web-activate" not in page, "active → no activate"
    assert f"/projects/{no_repo}/web-archive" in page
    assert "/projects/web-create" in page, "creating a project is not per-project"
    assert f"/projects/{with_repo}/web-edit" in page

    # `default` must never offer archive: the routing fallback cannot be removed.
    default_id = (await repo_module.get_project_by_slug(db, "default"))["id"]
    assert f"/projects/{default_id}/web-archive" not in page


async def test_project_card_surfaces_pending_and_provision_error(
    client: AsyncClient, db
):
    # AC-2 (#568): a broken workspace is news, not administration. Behind the
    # disclosure it would be one unopened click away from invisible — the same
    # reasoning as #615: silence must not pass for "fine".
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    broken = await repo_module.create_project(
        db, slug="broken-ws", name="Broken WS", repo_name="owner/x"
    )
    await db.execute(
        "UPDATE projects SET provision_status='error', provision_detail='clone failed' "
        "WHERE id=?",
        (broken,),
    )
    pending = await repo_module.create_project(db, slug="waiting", name="Waiting")
    await db.execute("UPDATE projects SET status='pending' WHERE id=?", (pending,))
    await db.commit()

    page = (await client.get("/projects")).text
    header_of_broken = page.split("Broken WS", 1)[1].split("</div>", 1)[0]
    assert "workspace: ошибка" in header_of_broken, (
        "the failure must sit in the card header, above the collapsed block"
    )
    # #622 goes further than #568: the CAUSE is visible text now, not a title
    # attribute a phone never shows.
    assert "clone failed" in page, "the reason must be readable without hovering"
    header_of_pending = page.split("Waiting", 1)[1].split("</div>", 1)[0]
    assert "ждёт активации" in header_of_pending, (
        "the pending state belongs in the header too; the label is Russian since "
        "#623, which is when this state was first rendered and read"
    )


async def test_projects_page_lists_each_project_once(client: AsyncClient, db):
    # AC-3 (#568): the actual defect. Until now /projects showed every project
    # TWICE — cards on top, the routing table below — which is why the page read
    # like a draft with the old version left in. Removing the table must not lose
    # a project, and an archived one is the case that would go first.
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    kept = await repo_module.create_project(db, slug="kept", name="Kept Project")
    gone = await repo_module.create_project(db, slug="retired", name="Retired Project")
    await db.execute("UPDATE projects SET archived=1 WHERE id=?", (gone,))
    await db.commit()

    page = (await client.get("/projects")).text
    # Match the ARTICLE, not the prefix: 'class="project-card' also hits
    # project-cards, project-card-numbers and project-card-waiting — my first
    # version of this assertion counted 10 for three projects.
    assert page.count('<article class="project-card') == 3, (
        "one card per project (default, kept, retired) and no second listing"
    )
    assert "<table" not in page, "the duplicate routing table is gone"
    assert "Retired Project" in page, "an archived project must still be reachable"
    assert kept and gone


# ---------------------------------------------------------------------------
# The card order answers "where am I needed", not "where did something move"
# (#623)
# ---------------------------------------------------------------------------


async def _project_with_epic(db, *, slug: str, name: str, child_status: str):
    """A project whose epic carries one child in the given status."""
    from hub import repository as repo_module

    project_id = await repo_module.create_project(db, slug=slug, name=name)
    epic_id = await repo_module.create_task(
        db,
        title=f"Эпик проекта {name}",
        description="",
        task_type="epic",
        runtime="auto",
        source="agent",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        parent_id=None,
        priority="medium",
    )
    child_id = await repo_module.create_task(
        db,
        title=f"Задача проекта {name}",
        description="",
        task_type="task",
        runtime="auto",
        source="agent",
        assigned_agent="",
        rationale="",
        status=child_status,
        auto_review=True,
        parent_id=epic_id,
        priority="medium",
    )
    await db.execute(
        "UPDATE tasks SET project_id = ? WHERE id IN (?, ?)",
        (project_id, epic_id, child_id),
    )
    await db.commit()
    return project_id, child_id


async def test_cards_put_what_waits_for_a_human_first(db):
    # AC-2 (#623). The order used to answer "where did something move last",
    # which is the question the feed already answers. The card list is read to
    # decide where to GO, and a project holding a decision outranks a project
    # where an agent merged something an hour ago — the human is the scarce one.
    from hub.db import seed_default_project
    from hub.services.dashboard import get_project_cards
    from hub import repository as repo_module

    await seed_default_project(db)

    # Waits for a human (a draft needs approval) and has NO feed activity at all.
    waiting, _ = await _project_with_epic(
        db, slug="zhdet", name="Ждёт решения", child_status="draft"
    )
    # Nothing waits, but work moved just now — the loudest thing under the old key.
    fresh, fresh_child = await _project_with_epic(
        db, slug="svezhiy", name="Свежая активность", child_status="completed"
    )
    await repo_module.add_task_update(
        db, fresh_child, "hub", "status", "работа шла только что"
    )
    await db.commit()

    order = [c["project"]["id"] for c in await get_project_cards(db)]
    assert waiting in order and fresh in order, "precondition: both cards are built"
    assert order.index(waiting) < order.index(fresh), (
        "a project holding a human decision must come before a project that "
        f"merely saw recent activity; got order {order} "
        f"(waiting={waiting}, fresh={fresh})"
    )


async def test_activity_still_breaks_ties_within_the_same_group(db):
    # The new key must not throw the old one away: among projects that equally
    # do or do not wait for a human, "where did work happen last" is still the
    # right tiebreak — otherwise the list degenerates into an order by id, which
    # answers "which was created last" and nothing more.
    #
    # The fresher project is created FIRST on purpose, so its id is the SMALLER
    # one: with the ids agreeing with the activity, dropping the activity key
    # entirely would still pass and the test would prove nothing. A mutation
    # that removed it did exactly that until this fixture was rebuilt.
    from hub.db import seed_default_project
    from hub.services.dashboard import get_project_cards
    from hub import repository as repo_module

    await seed_default_project(db)
    newer, newer_child = await _project_with_epic(
        db, slug="novyy", name="Новая активность", child_status="completed"
    )
    older, older_child = await _project_with_epic(
        db, slug="staryy", name="Старая активность", child_status="completed"
    )
    await repo_module.add_task_update(db, older_child, "hub", "status", "давно")
    await db.execute(
        "UPDATE task_updates SET created_at = '2020-01-01 00:00:00' WHERE task_id = ?",
        (older_child,),
    )
    await repo_module.add_task_update(db, newer_child, "hub", "status", "недавно")
    await db.commit()

    order = [c["project"]["id"] for c in await get_project_cards(db)]
    assert order.index(newer) < order.index(older), (
        f"neither project waits for a human, so the fresher one leads; got {order}"
    )


async def test_a_bigger_backlog_does_not_outrank_fresher_work(db):
    # The first sort key is a BOOLEAN, and this pins why. Both projects wait for
    # a human, so the question is which to open first — and "the one where five
    # things are stuck" is not a better answer than "the one that moved today".
    # Sorting by the COUNT would nail the largest backlog to the top forever,
    # which is the same defect as ordering by id: a stable answer to a question
    # nobody asked. A mutation replacing bool(...) with the raw number survived
    # until this test existed.
    from hub.db import seed_default_project
    from hub.services.dashboard import get_project_cards
    from hub import repository as repo_module

    await seed_default_project(db)
    big, big_child = await _project_with_epic(
        db, slug="bolshoy", name="Большой бэклог", child_status="draft"
    )
    for i in range(4):
        extra = await repo_module.create_task(
            db,
            title=f"Ещё черновик {i}",
            description="",
            task_type="task",
            runtime="auto",
            source="agent",
            assigned_agent="",
            rationale="",
            status="draft",
            auto_review=True,
            parent_id=None,
            priority="medium",
        )
        await db.execute(
            "UPDATE tasks SET parent_id = (SELECT parent_id FROM tasks WHERE id = ?), "
            "project_id = ? WHERE id = ?",
            (big_child, big, extra),
        )
    await repo_module.add_task_update(db, big_child, "hub", "status", "давно")
    await db.execute(
        "UPDATE task_updates SET created_at = '2020-01-01 00:00:00' WHERE task_id = ?",
        (big_child,),
    )
    small, small_child = await _project_with_epic(
        db, slug="malenkiy", name="Одно решение", child_status="draft"
    )
    await repo_module.add_task_update(db, small_child, "hub", "status", "сегодня")
    await db.commit()

    cards = await get_project_cards(db)
    counts = {c["project"]["id"]: c["awaiting_human"] for c in cards}
    assert counts[big] > counts[small], (
        f"precondition: the big backlog really is bigger, got {counts}"
    )
    order = [c["project"]["id"] for c in cards]
    assert order.index(small) < order.index(big), (
        "both wait for a human, so the fresher one leads regardless of backlog "
        f"size; got {order} (small={small}, big={big})"
    )


async def test_project_card_shows_forge(client: AsyncClient, db):
    """#1114: на каком хостинге проект — видно в вебе, а не только в базе.

    ``owner/name`` выглядит одинаково на любом форже, так что строка repo на
    этот вопрос не отвечает.

    Проверяется ЗНАЧЕНИЕ на карточке, а не наличие слова на странице, и это
    не придирка: строка «gitverse» есть на любой странице проектов всегда —
    её печатает каждый селект формы, перебирающий FORGES. Проверка на
    вхождение подстроки оставалась бы зелёной, даже если убрать значение из
    карточки совсем или подставить туда константу. Поэтому здесь два проекта
    на разных форжах и точная пара dt/dd для каждого: подмена значения
    константой красит тест на одном из них при любом выборе константы.
    """
    from hub.db import seed_default_project

    await seed_default_project(db)
    for slug, forge in (("gv-web", "gitverse"), ("gh-web", "github")):
        resp = await client.post(
            "/projects/web-create",
            data={
                "slug": slug,
                "name": slug.upper(),
                "repo": "owner/repo",
                "default_branch": "master",
                "forge": forge,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

    from hub import repository as repo_module

    row = await repo_module.get_project_by_slug(db, "gv-web")
    assert row["forge"] == "gitverse", "селект формы обязан доезжать до записи"

    page = await client.get("/projects")
    rendered = " ".join(page.text.split())
    assert "<dt>форж</dt><dd>gitverse</dd>" in rendered
    assert "<dt>форж</dt><dd>github</dd>" in rendered


async def test_web_edit_changes_the_forge(client: AsyncClient, db):
    """Селект в форме ПРАВКИ тоже обязан доезжать до записи.

    Создание было покрыто, правка — нет, а это разные обработчики: у правки
    свой список полей, и forge в нём мог остаться неперечисленным. Тогда
    селект рисуется, пользователь выбирает, форма отправляется — и ничего не
    меняется.
    """
    from hub.db import seed_default_project

    await seed_default_project(db)
    await client.post(
        "/projects/web-create",
        data={"slug": "switch-me", "name": "Switch", "default_branch": "main"},
        follow_redirects=False,
    )

    from hub import repository as repo_module

    row = await repo_module.get_project_by_slug(db, "switch-me")
    assert row["forge"] == "github"

    resp = await client.post(
        f"/projects/{row['id']}/web-edit",
        data={"name": "Switch", "default_branch": "main", "forge": "gitverse"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    row = await repo_module.get_project_by_slug(db, "switch-me")
    assert row["forge"] == "gitverse", "селект правки обязан доезжать до записи"


async def test_http_refuses_an_unknown_forge_and_names_the_allowed(
    client: AsyncClient, db
):
    """AC-2 на HTTP: отказ не просто 422, а с перечислением допустимых.

    Модельный тест проверяет `_validated_forge`; здесь проверяется, что
    перечень доезжает до клиента через FastAPI, а не теряется по дороге —
    ровно как это проверено для default_branch_policy.
    """
    created = await client.post(
        "/api/projects", json={"slug": "bad", "name": "Bad", "forge": "gitlab"}
    )
    assert created.status_code == 422
    assert "github" in created.text and "gitverse" in created.text

    ok = await client.post("/api/projects", json={"slug": "good", "name": "Good"})
    patched = await client.patch(
        f"/api/projects/{ok.json()['id']}", json={"forge": "gitlab"}
    )
    assert patched.status_code == 422
    assert "github" in patched.text and "gitverse" in patched.text


async def test_web_create_refuses_an_unknown_forge(client: AsyncClient, db):
    """Опечатка в форже не создаёт проект молча на github."""
    from hub.db import seed_default_project

    await seed_default_project(db)
    resp = await client.post(
        "/projects/web-create",
        data={"slug": "bad-forge", "name": "Bad", "forge": "gitlab"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]

    from hub import repository as repo_module

    assert await repo_module.get_project_by_slug(db, "bad-forge") is None
