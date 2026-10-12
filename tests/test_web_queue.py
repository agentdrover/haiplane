from __future__ import annotations

import re
from httpx import AsyncClient

from tests.web_support import (
    _form_carries_policy_version,  # noqa: F401
    _draft_with_readiness,
)


# ---- Project selector and filtering in Web UI (#339) ----


async def _web_two_projects(client: AsyncClient, db) -> tuple[int, int]:
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    pid_a = await repo_module.create_project(db, slug="ui-a", name="UI A")
    resp = await client.post(
        "/api/tasks", json={"title": "Epic UI-A", "task_type": "epic"}
    )
    epic_a = resp.json()["id"]
    await repo_module.update_task(db, epic_a, project_id=pid_a)
    resp = await client.post(
        "/api/tasks", json={"title": "Epic UI-B", "task_type": "epic"}
    )
    epic_b = resp.json()["id"]
    await db.commit()
    return epic_a, epic_b


async def test_tasks_page_filters_by_project(client: AsyncClient, db):
    # AC-1 (#339)
    epic_a, epic_b = await _web_two_projects(client, db)

    resp = await client.get("/tasks?project=ui-a&type=epic")
    assert resp.status_code == 200
    assert "Epic UI-A" in resp.text
    assert "Epic UI-B" not in resp.text
    assert 'id="project-select"' in resp.text
    assert "selected" in resp.text

    resp = await client.get("/tasks?type=epic")
    assert "Epic UI-A" in resp.text and "Epic UI-B" in resp.text


async def test_dashboard_and_inbox_filter_by_project(client: AsyncClient, db):
    epic_a, epic_b = await _web_two_projects(client, db)

    resp = await client.get("/?project=ui-a")
    assert resp.status_code == 200
    assert "Epic UI-A" in resp.text
    # Task cards/lists link to /tasks/{id}; the global activity feed keeps
    # plain-text mentions and is intentionally not project-scoped in V1.
    assert f'href="/tasks/{epic_a}"' in resp.text.replace("'", '"')
    assert f'href="/tasks/{epic_b}"' not in resp.text.replace("'", '"')

    resp = await client.get("/partials/kanban?project=ui-a")
    assert "Epic UI-A" in resp.text and "Epic UI-B" not in resp.text


async def test_projects_page_lists_and_links(client: AsyncClient, db):
    from hub.db import seed_default_project

    await seed_default_project(db)
    resp = await client.get("/projects")
    assert resp.status_code == 200
    assert "default" in resp.text
    assert "/tasks?project=default" in resp.text


async def test_task_detail_shows_project_badge(client: AsyncClient, db):
    epic_a, _ = await _web_two_projects(client, db)
    resp = await client.get(f"/tasks/{epic_a}")
    assert resp.status_code == 200
    assert "Project" in resp.text
    assert "ui-a" in resp.text


# ---------------------------------------------------------------------------
# Tasks outside every epic are named, not just findable (#571)
# ---------------------------------------------------------------------------


async def _orphan(client: AsyncClient, title: str, status: str | None = None) -> int:
    """A task with no parent — which is what POST /api/tasks makes by default."""
    task_id = (await client.post("/api/tasks", json={"title": title})).json()["id"]
    if status:
        await client.post(f"/api/tasks/{task_id}/status", json={"status": status})
    return task_id


async def test_tasks_outside_any_epic_are_shown_as_their_own_group(client: AsyncClient):
    # AC-1 (#571): the epic-shaped views are built on list_live_epics, so a task
    # with no parent appears in none of them. It IS reachable in the flat list —
    # the original statement claiming "invisible everywhere" was wrong — but
    # nothing said HOW MANY there were. On production that was 51 rows.
    await _orphan(client, "Orphan one")
    await _orphan(client, "Orphan two")

    for path in ("/partials/epics", "/projects"):
        page = await client.get(path)
        assert page.status_code == 200, page.text
        assert "Без эпика" in page.text, f"{path} must name the group"
        assert "no_epic=1" in page.text, f"{path} must link to the filtered list"

    # And the number must be the count, not a placeholder.
    assert "2 живых" in (await client.get("/partials/epics")).text


async def test_orphan_group_counts_live_tasks_only(client: AsyncClient, db):
    # AC-2 (#571): archived and terminal rows are not live work. `failed` is
    # terminal in FINAL_STATUSES, and #569 shipped a criterion that forgot it —
    # spec review caught that one, so it is asserted here rather than trusted.
    live = await _orphan(client, "Still open")
    done = await _orphan(client, "Finished")
    failed = await _orphan(client, "Broken")
    archived = await _orphan(client, "Put away")
    await db.execute("UPDATE tasks SET status='completed' WHERE id=?", (done,))
    await db.execute("UPDATE tasks SET status='failed' WHERE id=?", (failed,))
    await db.execute("UPDATE tasks SET archived=1 WHERE id=?", (archived,))
    await db.commit()

    from hub import repository as repo

    assert await repo.count_live_orphan_tasks(db) == 1, (
        "only the open task counts: completed, failed and archived are not live"
    )
    assert "1 живых" in (await client.get("/partials/epics")).text

    # An epic with no parent is not an orphan TASK — it is the top of a tree.
    epic = (
        await client.post("/api/tasks", json={"title": "An epic", "task_type": "epic"})
    ).json()["id"]
    assert epic
    assert await repo.count_live_orphan_tasks(db) == 1, "epics are not orphans"
    assert live


async def test_orphan_filter_is_a_separate_parameter(client: AsyncClient, db):
    # AC-3 (#571): the link needed a filter that did not exist. Overloading
    # parent_id was the tempting route and it would have broken
    # test_tasks_list_filters_ignore_blank_parent_id, which pins that a blank
    # parent_id means "no filter" — and with it every link that passes an empty
    # box. So the mode arrives as its own flag.
    epic = (
        await client.post(
            "/api/tasks", json={"title": "Parent epic", "task_type": "epic"}
        )
    ).json()["id"]
    child = (
        await client.post(
            "/api/tasks",
            json={"title": "Child of epic", "task_type": "feature", "parent_id": epic},
        )
    ).json()["id"]
    orphan = await _orphan(client, "No parent at all")

    filtered = await client.get("/tasks/list", params={"no_epic": "1"})
    assert filtered.status_code == 200, filtered.text
    assert f"/tasks/{orphan}" in filtered.text
    assert f"/tasks/{child}" not in filtered.text, (
        "a task under an epic is not an orphan"
    )
    assert f"/tasks/{epic}" not in filtered.text, "the epic itself is not an orphan"

    # The old contract is untouched: blank parent_id still disables filtering,
    # so both rows come back.
    unfiltered = await client.get("/tasks/list", params={"parent_id": ""})
    assert f"/tasks/{orphan}" in unfiltered.text
    assert f"/tasks/{child}" in unfiltered.text

    # And the full page accepts the same flag, not just the HTMX fragment —
    # the link in the group points at /tasks, so a flag wired into only one of
    # the two routes would give a link that quietly ignores it.
    page = await client.get("/tasks", params={"no_epic": "1"})
    assert page.status_code == 200
    assert f"/tasks/{child}" not in page.text


# ---------------------------------------------------------------------------
# A counter and the list behind its link tell the same story (#617)
# ---------------------------------------------------------------------------


async def _orphan_with_status(client: AsyncClient, db, title: str, status: str) -> int:
    task_id = (await client.post("/api/tasks", json={"title": title})).json()["id"]
    await db.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
    await db.commit()
    return task_id


async def test_orphan_counter_and_its_link_agree(client: AsyncClient, db):
    # AC-1 (#617). Found by looking at production, not by a failing test: the
    # line said "Без эпика: 19 живых" and its link opened 51 rows, because the
    # list filter took a single status while "live" is the negation of a set.
    live = [
        await _orphan_with_status(client, db, "still open", "open"),
        await _orphan_with_status(client, db, "in review", "review"),
    ]
    done = [
        await _orphan_with_status(client, db, "finished", "completed"),
        await _orphan_with_status(client, db, "broken", "failed"),
        await _orphan_with_status(client, db, "dropped", "rejected"),
    ]

    from hub import repository as repo

    counted = await repo.count_live_orphan_tasks(db)
    assert counted == len(live), "precondition: the counter counts live ones"

    page = await client.get("/tasks/list", params={"no_epic": "1", "state": "live"})
    assert page.status_code == 200, page.text
    for task_id in live:
        assert f"/tasks/{task_id}" in page.text
    for task_id in done:
        assert f"/tasks/{task_id}" not in page.text, (
            "a finished orphan must appear neither in the number nor in the list"
        )

    # And the markup actually carries that link, so the two cannot drift apart.
    for path in ("/projects", "/partials/epics"):
        assert "state=live" in (await client.get(path)).text, (
            f"{path} link must carry it"
        )


async def test_state_filter_works_on_both_routes_and_refuses_junk(
    client: AsyncClient, db
):
    # AC-2 (#617): a parameter honoured by only one of the two routes gives a
    # link that silently ignores it — #571 shipped exactly that shape and a
    # mutation caught it. And an unknown value must fail loudly: a filter that
    # quietly does nothing leaves the caller believing the list was narrowed.
    running = await _orphan_with_status(client, db, "being worked on", "running")
    drafted = await _orphan_with_status(client, db, "a draft", "draft")

    for path in ("/tasks/list", "/tasks"):
        inflight = await client.get(path, params={"state": "inflight"})
        assert inflight.status_code == 200, inflight.text
        assert f"/tasks/{running}" in inflight.text, f"{path}: running is in flight"
        assert f"/tasks/{drafted}" not in inflight.text, f"{path}: a draft is not"

        awaiting = await client.get(path, params={"state": "awaiting"})
        assert f"/tasks/{drafted}" in awaiting.text, f"{path}: a draft awaits a human"
        assert f"/tasks/{running}" not in awaiting.text

        junk = await client.get(path, params={"state": "nonsense"})
        assert junk.status_code == 400, (
            f"{path}: an unknown mode must be refused, not ignored"
        )
        assert "nonsense" in junk.text and "live" in junk.text, (
            "the refusal must name what was passed and what is accepted"
        )


async def test_state_is_independent_and_excludes_headless_review(
    client: AsyncClient, db
):
    # AC-3 (#617): the new mode composes with the old filters instead of
    # replacing them, and a headless review belongs to the poller conveyor, not
    # to a person — the rule list_stale_by_status(require_null_review_job) and
    # the #567 card counter already follow. Disagreeing here would recreate the
    # counter/link mismatch this task removes.
    human = await _orphan_with_status(client, db, "waiting for a person", "review")
    headless = await _orphan_with_status(client, db, "conveyor review", "review")
    await db.execute("UPDATE tasks SET review_job_id='job-1' WHERE id=?", (headless,))
    await db.commit()

    awaiting = await client.get("/tasks/list", params={"state": "awaiting"})
    assert f"/tasks/{human}" in awaiting.text
    assert f"/tasks/{headless}" not in awaiting.text, (
        "a review owned by the conveyor is not a person waiting"
    )

    # The pre-existing contract is untouched: a blank parent_id still disables
    # filtering (test_tasks_list_filters_ignore_blank_parent_id pins this), and
    # status still works on its own.
    blank = await client.get("/tasks/list", params={"parent_id": "", "state": "live"})
    assert blank.status_code == 200
    assert f"/tasks/{human}" in blank.text
    by_status = await client.get("/tasks/list", params={"status": "review"})
    assert f"/tasks/{headless}" in by_status.text, (
        "state must not hijack the plain status filter"
    )


# ---------------------------------------------------------------------------
# The card's numbers lead into the work, and "in flight" stops lying (#619)
# ---------------------------------------------------------------------------


async def _project_with(client: AsyncClient, db, slug: str, name: str) -> int:
    from hub import repository as repo_module

    pid = await repo_module.create_project(db, slug=slug, name=name)
    await db.commit()
    return pid


async def test_open_is_a_queue_not_work_in_flight(client: AsyncClient, db):
    # AC-2 (#619). Live page, 10.08: it-grade-dashboard showed "в работе: 46"
    # beside "активность: 20.07" — a month of silence. All 46 were `open`:
    # approved and untouched. The most abandoned project looked like the busiest.
    from hub.db import seed_default_project
    from hub.services.dashboard import get_project_cards

    await seed_default_project(db)
    pid = await _project_with(client, db, "queue-heavy", "Queue Heavy")
    epic = (
        await client.post(
            "/api/tasks",
            json={"title": "E", "task_type": "epic", "project": "queue-heavy"},
        )
    ).json()["id"]

    async def _child(title: str, status: str) -> int:
        tid = (
            await client.post(
                "/api/tasks",
                json={"title": title, "task_type": "feature", "parent_id": epic},
            )
        ).json()["id"]
        await db.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        return tid

    for i in range(3):
        await _child(f"approved, untouched {i}", "open")
    await _child("someone is on it", "running")
    await db.commit()

    card = next(c for c in await get_project_cards(db) if c["project"]["id"] == pid)
    assert card["in_flight"] == 1, "only what someone is moving counts as work"
    assert card["queued"] == 3, "the queue is counted, not folded into work"

    page = (await client.get("/projects")).text
    # Chips after #622; the numbers themselves are asserted on the card above.
    assert ">В работе<" in page and ">В очереди<" in page
    assert ">1<" in page and ">3<" in page, (
        "hiding the queue would trade one lie for another"
    )


async def test_card_numbers_open_the_matching_list(client: AsyncClient, db):
    # AC-1 (#619): the page is an entry point, so its numbers must be doors. The
    # state modes from #617 make the number and the list agree by construction —
    # a link with a different rule behind it is the #571 defect (18 vs 51).
    from hub.db import seed_default_project

    await seed_default_project(db)
    await _project_with(client, db, "clickable", "Clickable")
    epic = (
        await client.post(
            "/api/tasks",
            json={"title": "E", "task_type": "epic", "project": "clickable"},
        )
    ).json()["id"]
    waiting = (
        await client.post(
            "/api/tasks",
            json={
                "title": "needs a decision",
                "task_type": "feature",
                "parent_id": epic,
            },
        )
    ).json()["id"]
    await db.execute("UPDATE tasks SET status='needs_decision' WHERE id=?", (waiting,))
    moving = (
        await client.post(
            "/api/tasks",
            json={"title": "being done", "task_type": "feature", "parent_id": epic},
        )
    ).json()["id"]
    await db.execute("UPDATE tasks SET status='running' WHERE id=?", (moving,))
    await db.commit()

    page = (await client.get("/projects")).text
    assert (
        "project=clickable&amp;state=awaiting" in page
        or "project=clickable&state=awaiting" in page
    )
    assert (
        "project=clickable&amp;state=inflight" in page
        or "project=clickable&state=inflight" in page
    )
    assert "Вся доска проекта" in page, (
        "the board needs an explicit door, not a hidden title link"
    )

    # Follow the links: the list must contain exactly what the number promised.
    awaiting_page = await client.get(
        "/tasks", params={"project": "clickable", "state": "awaiting"}
    )
    assert f"/tasks/{waiting}" in awaiting_page.text
    assert f"/tasks/{moving}" not in awaiting_page.text
    inflight_page = await client.get(
        "/tasks", params={"project": "clickable", "state": "inflight"}
    )
    assert f"/tasks/{moving}" in inflight_page.text
    assert f"/tasks/{waiting}" not in inflight_page.text


async def test_card_names_what_it_hides_and_adapts_its_label(client: AsyncClient, db):
    # AC-3 (#619): "ждёт вас: 30 (черновиков: 30)" is absurd when the numbers are
    # equal, and a card that shows 5 of 11 epics without saying so reads as "that
    # is all" — the silent-cap rule from #611 and #615.
    from hub.db import seed_default_project
    from hub.services.dashboard import PROJECT_CARD_EPIC_LIMIT

    await seed_default_project(db)
    await _project_with(client, db, "many-epics", "Many Epics")
    for i in range(PROJECT_CARD_EPIC_LIMIT + 2):
        eid = (
            await client.post(
                "/api/tasks",
                json={
                    "title": f"epic {i}",
                    "task_type": "epic",
                    "project": "many-epics",
                },
            )
        ).json()["id"]
        child = (
            await client.post(
                "/api/tasks",
                json={"title": f"child {i}", "task_type": "feature", "parent_id": eid},
            )
        ).json()["id"]
        await db.execute("UPDATE tasks SET status='draft' WHERE id=?", (child,))
    await db.commit()

    page = (await client.get("/projects")).text
    assert "И ещё 2" in page, "the hidden remainder must be named, not dropped"
    # #622: said ONCE in the section subtitle instead of once per card.
    assert page.count("свежие сверху") == 1, (
        "the order must be stated once for the section, not repeated per card"
    )
    assert "Всё ожидание — черновики" in page, (
        "when every waiting item is a draft, say so instead of '(черновиков: N)'"
    )
    assert "готово" in page, "a bare 1/2 does not say what it is a share of"


async def test_activity_is_relative_and_computed_once(client: AsyncClient, db):
    # AC-4 (#619): a raw date makes the reader do arithmetic, and the card's
    # question is "where is work happening". Computed on the server so the next
    # view needing the same phrase cannot word it differently.
    from hub.services.dashboard import humanize_since

    assert humanize_since("2026-08-10 09:00:00", now="2026-08-10 21:00:00") == "сегодня"
    assert humanize_since("2026-08-09 09:00:00", now="2026-08-10 21:00:00") == "вчера"
    assert (
        humanize_since("2026-07-20 09:00:00", now="2026-08-10 21:00:00")
        == "3 нед. назад"
    )
    assert (
        humanize_since("2026-06-24 09:00:00", now="2026-08-10 21:00:00")
        == "месяц назад"
    )
    assert humanize_since(None) == "не было", "silence is an answer, not an empty cell"
    # An ISO stamp with a T must not fall through as a raw string: prepared_at
    # taught that lesson in #616.
    assert (
        humanize_since("2026-08-09T09:00:00+00:00", now="2026-08-10 21:00:00")
        == "вчера"
    )

    from hub.db import seed_default_project

    await seed_default_project(db)
    # A project with REAL feed activity. The first version of this assertion
    # seeded an empty project, where the card says "не было" — so a mutation that
    # printed the raw date survived, because there was no date to print. Found by
    # mutation, not by re-reading.
    await _project_with(client, db, "has-activity", "Has Activity")
    epic = (
        await client.post(
            "/api/tasks",
            json={"title": "E", "task_type": "epic", "project": "has-activity"},
        )
    ).json()["id"]
    child = (
        await client.post(
            "/api/tasks", json={"title": "c", "task_type": "feature", "parent_id": epic}
        )
    ).json()["id"]
    await client.post(
        f"/api/tasks/{child}/updates",
        json={"agent": "dev", "kind": "status", "content": "worked"},
    )
    await db.execute(
        "UPDATE task_updates SET created_at='2026-07-20 09:00:00' WHERE task_id=?",
        (child,),
    )
    await db.commit()

    page = (await client.get("/projects")).text
    # #622 renamed the label to «Обновлялся:» — a verb says what the date means.
    assert "Обновлялся:" in page
    # The exact date now lives in a .u-sr-only span (a title attribute is unusable
    # on touch). So strip what the eye cannot see before judging what it sees —
    # the first version of this assertion looked at a 60-character window and
    # would have failed an honest implementation.
    visible = re.sub(r'<span class="u-sr-only">.*?</span>', "", page, flags=re.S)
    shown = visible.split("Обновлялся:", 1)[1][:60]
    assert "2026-07-20" not in shown, (
        "the visible value must be relative; the exact date belongs to assistive text"
    )
    assert "2026-07-20" in page, "and the exact date must still be available somewhere"
    assert "назад" in shown or "сегодня" in shown or "вчера" in shown, (
        f"expected a relative phrase, got: {shown.strip()[:40]!r}"
    )


# ---------------------------------------------------------------------------
# The number on a card and the list behind its link, part three (#621)
# ---------------------------------------------------------------------------


async def _bulk_noise_tasks(db, count: int, status: str = "open") -> None:
    """`count` tasks belonging to no project — pure limit pressure.

    Inserted through SQL rather than the API on purpose: this fixture exists to
    push the interesting rows PAST the page limit, and a hundred round trips
    through the create pipeline would buy nothing but seconds.

    ``status`` is a parameter because noise only presses where the filter under
    test lets it through: 150 `open` rows are invisible to a test of the
    `awaiting` mode, and a test whose noise gets filtered out for it passes on
    the broken code. That mistake was made once here and caught by a test that
    went green before its fix existed.
    """
    await db.executemany(
        f"INSERT INTO tasks (title, task_type, status) VALUES (?, 'task', '{status}')",  # noqa: E501, S608
        [(f"noise {status} {i}",) for i in range(count)],
    )
    await db.commit()


async def test_project_filter_survives_the_page_limit(client: AsyncClient, db):
    # AC-1 (#621). Live, 12.08: audit-evidence promised 17 queued and its link
    # opened ZERO rows, because /tasks took the newest 100 tasks GLOBALLY and
    # only then dropped the ones belonging to other projects. An empty page is
    # indistinguishable from "this project has no work" — the quiet kind of lie.
    #
    # The fixture puts the project's rows BEHIND a hundred newer ones on
    # purpose. A test whose rows all fit inside the limit passes on the broken
    # code too, which is exactly how this defect survived #339 and #370.
    from hub.db import seed_default_project

    await seed_default_project(db)
    await _project_with(client, db, "old-work", "Old Work")
    epic = (
        await client.post(
            "/api/tasks",
            json={"title": "E", "task_type": "epic", "project": "old-work"},
        )
    ).json()["id"]
    buried = (
        await client.post(
            "/api/tasks",
            json={
                "title": "buried by newer work",
                "task_type": "feature",
                "parent_id": epic,
            },
        )
    ).json()["id"]
    await db.execute("UPDATE tasks SET status='open' WHERE id=?", (buried,))
    await db.commit()

    await _bulk_noise_tasks(db, 150)

    unscoped = await client.get("/tasks")
    assert f"/tasks/{buried}" not in unscoped.text, (
        "precondition: the noise really does push the project past the limit"
    )

    for params in ({"project": "old-work"}, {"project": "old-work", "state": "queued"}):
        page = await client.get("/tasks", params=params)
        assert page.status_code == 200, page.text
        assert f"/tasks/{buried}" in page.text, (
            f"{params}: the project filter has to reach the query and run BEFORE "
            "the limit — filtering afterwards spends the whole limit on other "
            "projects' rows and returns an empty page"
        )


async def test_the_queue_counts_epics_the_same_way_its_link_does(
    client: AsyncClient, db
):
    # AC-2 (#621). Live, 12.08: notesforllm promised 4 queued and opened 5,
    # calc-kids promised 1 and opened 2. The counter excludes epics
    # (project_work_summary), the state filter did not — so a project's own epic
    # sitting in `open` appeared in the list but not in the number.
    #
    # Resolved in favour of excluding, the same asymmetry #567 settled for "in
    # flight": an epic lives its whole life in `open` as a container, so it is
    # not "approved and nobody started it".
    import re

    from hub.db import seed_default_project
    from hub.services.dashboard import get_project_cards

    await seed_default_project(db)
    pid = await _project_with(client, db, "one-epic", "One Epic")
    epic = (
        await client.post(
            "/api/tasks",
            json={
                "title": "a container, not a queue item",
                "task_type": "epic",
                "project": "one-epic",
            },
        )
    ).json()["id"]
    queued = (
        await client.post(
            "/api/tasks",
            json={"title": "really queued", "task_type": "feature", "parent_id": epic},
        )
    ).json()["id"]
    await db.execute("UPDATE tasks SET status='open' WHERE id IN (?,?)", (epic, queued))
    await db.commit()

    card = next(c for c in await get_project_cards(db) if c["project"]["id"] == pid)
    assert card["queued"] == 1, "precondition: the counter does not count the epic"

    for path in ("/tasks", "/tasks/list"):
        page = await client.get(path, params={"project": "one-epic", "state": "queued"})
        assert page.status_code == 200, page.text
        # Anchored on the ROW marker, not on a bare "/tasks/{id}": a child row
        # links to its own parent, so the loose form finds the epic in the very
        # row that proves it was excluded. Same marker the production walk
        # counted rows by.
        rows = re.findall(r'id="task-row-(\d+)"', page.text)
        assert rows == [str(queued)], (
            f"{path}: the list must hold exactly what the number promised — the "
            f"epic is a container in the count, so it cannot be a queue item in "
            f"the list; got rows {rows}"
        )


# ---------------------------------------------------------------------------
# The scope survives the first click on a filter (#626)
# ---------------------------------------------------------------------------


async def _scoped_project_fixture(client: AsyncClient, db) -> tuple[int, int]:
    """A project holding one draft and one running task, buried under newer rows.

    Returns (draft id, running id). The burial matters: #621 fixed the page by
    moving the project INTO the query, and a fixture whose rows fit inside the
    limit cannot tell a scoped fragment from an unscoped one.
    """
    from hub.db import seed_default_project

    await seed_default_project(db)
    await _project_with(client, db, "scoped", "Scoped")
    epic = (
        await client.post(
            "/api/tasks",
            json={"title": "E", "task_type": "epic", "project": "scoped"},
        )
    ).json()["id"]

    async def _child(title: str, status: str) -> int:
        tid = (
            await client.post(
                "/api/tasks",
                json={"title": title, "task_type": "feature", "parent_id": epic},
            )
        ).json()["id"]
        await db.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        return tid

    drafted = await _child("scoped draft", "draft")
    running = await _child("scoped running", "running")
    await db.commit()
    # Noise in BOTH statuses under test: `open` rows are invisible to the
    # `awaiting` mode, so open-only noise would leave that test proving nothing.
    await _bulk_noise_tasks(db, 150, status="open")
    await _bulk_noise_tasks(db, 150, status="draft")
    return drafted, running


async def test_filter_change_keeps_the_project_and_state_it_arrived_with(
    client: AsyncClient, db
):
    # AC-1 (#626). Live, 12.08: /tasks?project=default&state=awaiting showed 28
    # rows — exactly what the card promised. Changing ANY filter sent htmx to
    # /tasks/list, which knew nothing of either parameter, and the table became
    # 100 rows across every project while the selector still said "Default".
    # Widening a list looks exactly like filtering it, which is what makes this
    # quiet.
    import re

    drafted, running = await _scoped_project_fixture(client, db)

    # What the page shows when you arrive from a card.
    arrived = await client.get(
        "/tasks", params={"project": "scoped", "state": "awaiting"}
    )
    # #630 renders the awaiting mode as the queue, so the row marker moved from
    # task-row- to queue-item-. What this test is about — the scope surviving a
    # filter change — did not.
    assert re.findall(r'id="queue-item-(\d+)"', arrived.text) == [str(drafted)]

    # What htmx asks for when a filter changes — now carrying the same scope.
    after_click = await client.get(
        "/tasks/list",
        params={"project": "scoped", "state": "awaiting", "priority": "medium"},
    )
    assert after_click.status_code == 200, after_click.text
    # The fragment answers in the SAME shape the page did — a queue for a queue
    # (#630). Serving a table into the same div would rearrange the screen under
    # a reader who only touched a priority.
    rows = re.findall(r'id="queue-item-(\d+)"', after_click.text)
    assert rows == [str(drafted)], (
        "changing a filter must NARROW what was on screen, not silently replace "
        f"it with every project's rows; got {len(rows)} rows"
    )
    assert str(running) not in rows, "the state mode has to survive the click too"


async def test_fragment_and_page_agree_on_the_project_scope(client: AsyncClient, db):
    # AC-2 (#626). Compared as SETS, not sizes: two lists of equal length can
    # hold different rows, and that mistake would let this pass on broken code.
    import re

    drafted, running = await _scoped_project_fixture(client, db)

    for params in ({"project": "scoped"}, {"project": "scoped", "state": "live"}):
        page = await client.get("/tasks", params=params)
        fragment = await client.get("/tasks/list", params=params)
        assert fragment.status_code == 200, fragment.text
        page_rows = set(re.findall(r'id="task-row-(\d+)"', page.text))
        frag_rows = set(re.findall(r'id="task-row-(\d+)"', fragment.text))
        assert page_rows == frag_rows, (
            f"{params}: the fragment and the page must answer the same question — "
            f"page had {len(page_rows)} rows, fragment {len(frag_rows)}"
        )
        assert {str(drafted), str(running)} <= page_rows, (
            "precondition: the project's own rows are in the scoped answer"
        )


async def test_the_filter_bar_carries_the_scope_it_was_opened_with(
    client: AsyncClient, db
):
    # AC-3 (#626). The server side alone is not a fix: hx-include collects
    # ".filter-bar [name]", and the project selector lives in the header, OUTSIDE
    # that container. Measured on production before this task, the bar sent
    # exactly eight fields and none of them was project, state or no_epic — so a
    # fixed handler would still never be told.
    import re

    await _scoped_project_fixture(client, db)
    page = await client.get(
        "/tasks", params={"project": "scoped", "state": "awaiting", "no_epic": "1"}
    )
    bar = page.text[
        page.text.index('class="filter-bar"') : page.text.index('id="task-table-wrap"')
    ]
    sent = set(re.findall(r'name="([^"]+)"', bar))
    assert {"project", "state", "no_epic"} <= sent, (
        f"the bar must send the scope it was opened with; it sends {sorted(sent)}"
    )
    assert 'value="scoped"' in bar and 'value="awaiting"' in bar, (
        "the hidden fields must carry the CURRENT values, not empty placeholders"
    )


async def test_project_selector_keeps_the_other_filters(client: AsyncClient, db):
    # AC-4 (#626). The selector is a GET form; submitting it replaces the whole
    # query string. Its own template said so: "keeps other query params out of
    # scope for V1". Narrowing by project should not clear the status you were
    # looking at.
    import re

    await _scoped_project_fixture(client, db)
    page = await client.get(
        "/tasks", params={"project": "scoped", "status": "draft", "priority": "high"}
    )
    form = re.search(r'<form[^>]*class="project-selector".*?</form>', page.text, re.S)
    assert form, "the selector form must be on the page"
    carried = dict(re.findall(r'name="(\w+)"\s+value="([^"]*)"', form.group(0)))
    assert carried.get("status") == "draft" and carried.get("priority") == "high", (
        f"switching project must keep the other filters; the form carries {carried}"
    )

    # ...and the dashboard, which SHARES this template, must not grow task-page
    # filter fields in its own URL.
    dash = await client.get("/", params={"project": "scoped"})
    dash_form = re.search(
        r'<form[^>]*class="project-selector".*?</form>', dash.text, re.S
    )
    assert dash_form, "the dashboard has the selector too"
    assert 'name="status"' not in dash_form.group(0), (
        "the dashboard has no task filters to preserve — carrying them there "
        "would put meaningless parameters in its URL"
    )


async def test_the_applied_state_mode_is_visible_and_removable(client: AsyncClient, db):
    # AC-5 (#626). A filter with no on-screen representation cannot be read or
    # undone: today the only way out of state=awaiting is Reset, which drops the
    # project as well. So the mode gets a control, and clearing it keeps the
    # project.
    import re

    await _scoped_project_fixture(client, db)
    page = (
        await client.get("/tasks", params={"project": "scoped", "state": "awaiting"})
    ).text
    assert "state-chip" in page, "an applied mode must be visible on the page"
    clear = re.search(r'class="[^"]*state-chip-clear[^"]*"\s+href="([^"]+)"', page)
    assert clear, "the mode must be removable without hunting for Reset"
    target = clear.group(1).replace("&amp;", "&")
    assert "project=scoped" in target, (
        f"clearing the mode must keep the project; it goes to {target}"
    )
    assert "state=" not in target, "…and must actually drop the mode"


# ---------------------------------------------------------------------------
# The dashboard and the inbox stop filtering by project after their limit (#627)
# ---------------------------------------------------------------------------


async def _dashboard_project_fixture(client: AsyncClient, db) -> dict[str, int]:
    """A project with one task per interesting status, buried under newer rows.

    40 noise rows PER STATUS, because these lists are capped at 20 — a fixture
    that fits inside the cap proves nothing, and this is the third task in a row
    where that trap was the real risk (#621, #626).
    """
    from hub.db import seed_default_project

    await seed_default_project(db)
    await _project_with(client, db, "dash", "Dash")
    epic = (
        await client.post(
            "/api/tasks", json={"title": "E", "task_type": "epic", "project": "dash"}
        )
    ).json()["id"]

    ids: dict[str, int] = {}
    for status in ("draft", "review", "running", "needs_decision"):
        tid = (
            await client.post(
                "/api/tasks",
                json={
                    "title": f"dash {status}",
                    "task_type": "feature",
                    "parent_id": epic,
                },
            )
        ).json()["id"]
        await db.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        ids[status] = tid
    await db.commit()
    for status in ("draft", "review", "running", "needs_decision"):
        await _bulk_noise_tasks(db, 40, status=status)
    return ids


async def test_dashboard_keeps_the_project_rows_past_its_limit(client: AsyncClient, db):
    # AC-1 (#627). Measured before the fix: with 40 newer rows per status, the
    # project's own tasks were absent from data.active_tasks, draft_tasks,
    # review_tasks, needs_decision_tasks AND inbox['decisions'] — every list is
    # capped at 20 in the service layer, and the project filter ran afterwards.
    # Choosing an empty board over a wrong one is not a choice a reader can see.
    from hub import services

    ids = await _dashboard_project_fixture(client, db)

    # Asserted at the SERVICE layer, where the cap lives. The first version of
    # this test read the whole dashboard page and passed on the broken code —
    # the epic list renders links to its own children, so the ids were on the
    # page for a reason that had nothing to do with the board.
    data = await services.get_dashboard_data(db, project="dash")
    inbox = await services.get_inbox_data(db, project="dash")

    def _ids(items):
        return {t.id if hasattr(t, "id") else t.get("id") for t in items}

    assert ids["running"] in _ids(data.active_tasks), "board lost the running task"
    assert ids["draft"] in _ids(data.draft_tasks), "board lost the draft"
    assert ids["review"] in _ids(data.review_tasks), "board lost the review"
    assert ids["needs_decision"] in _ids(data.needs_decision_tasks), (
        "board lost the decision"
    )
    assert ids["draft"] in _ids(inbox["drafts"]), "inbox lost the draft"
    assert ids["needs_decision"] in _ids(inbox["decisions"]), (
        "inbox lost the decision — this is the list that was measured empty"
    )

    # Nothing from outside the project rode along.
    assert not any(t.title.startswith("noise") for t in data.draft_tasks), (
        "a scoped board must not carry other projects' rows"
    )


async def test_no_consumer_filters_by_project_after_its_limit(client: AsyncClient, db):
    # AC-2 (#627). All four consumers, not three of four: the same defect left
    # in one place is the same defect. #370 fixed the API, #621 the tasks page,
    # and this is the third appearance — so the technique goes, not one more
    # caller.
    import re
    from pathlib import Path

    ids = await _dashboard_project_fixture(client, db)

    # Named per fragment, and deliberately NOT "at least one row survived": the
    # draft queue is ordered created_at ASC, so the project's oldest draft rides
    # at the front whether or not scoping works. A mutation that removed the
    # scoping passed against the loose version of this assertion. Each id below
    # is one the id-DESC ordering genuinely buries.
    expected = {
        "/partials/inbox": [ids["needs_decision"]],
        "/partials/kanban": [ids["running"], ids["review"], ids["needs_decision"]],
    }
    for path, must_appear in expected.items():
        frag = await client.get(path, params={"project": "dash"})
        assert frag.status_code == 200, frag.text
        found = set(re.findall(r"/tasks/(\d+)", frag.text))
        missing = [i for i in must_appear if str(i) not in found]
        assert not missing, (
            f"{path}: the project's rows {missing} did not survive — the "
            "fragment is capped at 20 before the project could be applied"
        )

    # And the technique itself is gone, so a fifth caller cannot reintroduce it.
    web = (Path(__file__).resolve().parent.parent / "hub" / "web.py").read_text()
    assert "_filter_by_ids" not in web, (
        "post-filtering a limited list by project is the defect itself; leaving "
        "the helper in place invites the next caller to repeat it"
    )


# ---------------------------------------------------------------------------
# Approving from the inbox acts on what was SELECTED (#628)
# ---------------------------------------------------------------------------


async def _ready_draft(
    client: AsyncClient, db, title: str, *, project_epic=None
) -> int:
    """A DoR-ready draft, optionally hung under an epic so it has a project."""
    task_id = await _draft_with_readiness(client, title, ready=True)
    if project_epic is not None:
        await db.execute(
            "UPDATE tasks SET parent_id=?, task_type='feature' WHERE id=?",
            (project_epic, task_id),
        )
        await db.commit()
    return task_id


async def _status_of(db, task_id: int) -> str:
    rows = await db.execute_fetchall("SELECT status FROM tasks WHERE id=?", (task_id,))
    return rows[0]["status"]


async def test_group_approve_touches_only_what_was_selected(client: AsyncClient, db):
    # AC-1 (#628). Measured on the old button: viewing a project whose inbox
    # held ONE ready draft, the click approved two — the second belonged to a
    # project the reader could not see. Both went draft → open. A label reading
    # "(1)" that moves two rows is not an off-by-one, it is an action whose
    # radius is invisible until afterwards.
    from hub.db import seed_default_project

    await seed_default_project(db)
    await _project_with(client, db, "picked", "Picked")
    epic = (
        await client.post(
            "/api/tasks",
            json={"title": "E", "task_type": "epic", "project": "picked"},
        )
    ).json()["id"]
    chosen_a = await _ready_draft(client, db, "chosen a", project_epic=epic)
    chosen_b = await _ready_draft(client, db, "chosen b", project_epic=epic)
    shown_not_chosen = await _ready_draft(
        client, db, "shown, not ticked", project_epic=epic
    )
    outside = await _ready_draft(client, db, "another project entirely")

    # The selection has to be reachable from the page, not only from a curl:
    # a server that honours task_ids while the inbox renders no checkboxes is
    # a fix nobody can use, and a mutation that removed them stayed green
    # until this assertion existed.
    page = (await client.get("/partials/inbox")).text
    for task_id in (chosen_a, shown_not_chosen):
        assert f'name="task_ids" value="{task_id}"' in page, (
            f"#{task_id} has no checkbox — the reader cannot tick it"
        )

    resp = await client.post(
        "/tasks/web-batch-approve-selected",
        data={"task_ids": [str(chosen_a), str(chosen_b)]},
    )
    assert resp.status_code in (200, 303), resp.text

    assert await _status_of(db, chosen_a) == "open"
    assert await _status_of(db, chosen_b) == "open"
    assert await _status_of(db, shown_not_chosen) == "draft", (
        "a task that was on screen but NOT ticked must not be approved"
    )
    assert await _status_of(db, outside) == "draft", (
        "a task the reader never saw must not be approved — this is the "
        "measured defect: the old button approved it too"
    )


async def test_empty_selection_approves_nothing(client: AsyncClient, db):
    # AC-2 (#628). The trap worth naming: an empty list read as "then approve
    # everything" would rebuild the same defect in new clothes. Nothing is the
    # only honest reading of nothing.
    from hub.db import seed_default_project

    await seed_default_project(db)
    ready = await _ready_draft(client, db, "ready and untouched")

    resp = await client.post("/tasks/web-batch-approve-selected", data={})
    assert resp.status_code in (200, 303, 400), resp.text
    assert await _status_of(db, ready) == "draft", (
        "an empty selection means nothing, never everything"
    )


async def test_single_approve_survives_the_group_path(client: AsyncClient, db):
    # AC-3 (#628). Two approaches, and the single one is not swallowed by the
    # group: one task, one decision, same gates.
    from hub.db import seed_default_project

    await seed_default_project(db)
    one = await _ready_draft(client, db, "approved on its own")
    other = await _ready_draft(client, db, "left alone")

    resp = await client.post(f"/tasks/{one}/web-approve")
    assert resp.status_code in (200, 303), resp.text
    assert await _status_of(db, one) == "open"
    assert await _status_of(db, other) == "draft"


async def test_skipped_tasks_are_named_with_their_reason(client: AsyncClient, db):
    # AC-4 (#628). Ticking five and getting three, with no word about the other
    # two, is its own lie — and the reasons already exist in BatchApproveResult;
    # the old handler simply threw the result away.
    from hub.db import seed_default_project

    await seed_default_project(db)
    good = await _ready_draft(client, db, "passes the gate")
    unrefined = await _draft_with_readiness(client, "never refined", ready=False)

    resp = await client.post(
        "/tasks/web-batch-approve-selected",
        data={"task_ids": [str(good), str(unrefined)]},
        headers={"HX-Request": "true"},
    )
    import re

    assert resp.status_code == 200, resp.text
    assert await _status_of(db, good) == "open"
    assert await _status_of(db, unrefined) == "draft"

    # Anchored INSIDE the result block, not anywhere on the page. The loose
    # version passed with the result thrown away, because a skipped draft stays
    # in the inbox and its id is on the page for an unrelated reason — the same
    # mistake as reading the whole dashboard to test the board.
    block = re.search(r'class="inbox-batch-skipped".*?</div>', resp.text, re.S)
    assert block, "skipped tasks must be reported, not silently dropped"
    assert str(unrefined) in block.group(0), (
        "a task the reader explicitly ticked and did not get must be named"
    )
    assert re.search(r"#\d+:\s*\w", block.group(0)), (
        "…and named WITH its reason, not merely listed"
    )
    assert str(good) in resp.text


# ---------------------------------------------------------------------------
# Reading the list is a read: no recompute per row, no writes on GET (#629)
# ---------------------------------------------------------------------------


async def _ready_row(
    db,
    client: AsyncClient,
    title: str,
    *,
    dor: bool,
    prepared_by: str = "",
    legacy_marker: bool = False,
) -> int:
    """A task whose readiness signal is set up through the DB, not the pipeline.

    The signal has TWO shapes and both must survive: the modern one is the
    ``prepared_by`` column, the legacy one is an update carrying
    ANALYST_PREPARED_MARKER for tasks refined before that column existed.
    """
    from hub.web import ANALYST_PREPARED_MARKER

    task_id = (await client.post("/api/tasks", json={"title": title})).json()["id"]
    await db.execute(
        "UPDATE tasks SET dor_passed=?, prepared_by=? WHERE id=?",
        (1 if dor else 0, prepared_by, task_id),
    )
    if legacy_marker:
        await db.execute(
            "INSERT INTO task_updates (task_id, agent, kind, content) "
            "VALUES (?, 'bot', 'status', ?)",
            (task_id, f"{ANALYST_PREPARED_MARKER}: readiness score=99"),
        )
    await db.commit()
    return task_id


async def test_task_list_does_not_recompute_readiness_per_row(
    client: AsyncClient, db, monkeypatch
):
    # AC-1 (#629). Measured before the fix: _analyst_ready_map looped over the
    # rows and called get_readiness for each one — up to 100 recomputes to draw
    # one binary badge, while dor_passed and prepared_by sit in the row already.
    from hub import services

    for i in range(30):
        await _ready_row(
            db,
            client,
            f"row {i}",
            dor=i % 2 == 0,
            prepared_by="bot" if i % 4 == 0 else "",
        )

    calls = {"n": 0}
    real = services.get_readiness

    async def counting(*args, **kwargs):
        calls["n"] += 1
        return await real(*args, **kwargs)

    monkeypatch.setattr(services, "get_readiness", counting)

    resp = await client.get("/tasks")
    assert resp.status_code == 200, resp.text
    assert calls["n"] == 0, (
        f"rendering the list recomputed readiness {calls['n']} times; the badge "
        "must be built from fields already read with the row, so the query count "
        "cannot depend on how long the list is"
    )


async def test_get_on_the_task_list_writes_nothing(client: AsyncClient, db):
    # AC-2 (#629). get_readiness repairs stale persisted values as a side effect
    # (refinement.py, "lazy repair" from #250) — under a write lock, on a GET.
    # A read that writes is a read that can block.
    for i in range(12):
        await _ready_row(db, client, f"stale {i}", dor=True, legacy_marker=True)
    await db.execute("UPDATE tasks SET readiness_score=1 WHERE readiness_score IS NULL")
    await db.commit()

    before = db.total_changes
    resp = await client.get("/tasks")
    assert resp.status_code == 200, resp.text
    assert db.total_changes == before, (
        f"GET /tasks changed {db.total_changes - before} rows; reading a list "
        "must not write"
    )


async def test_readiness_badge_keeps_its_meaning(client: AsyncClient, db):
    # AC-3 (#629). The signal is NOT dor_passed: it is dor_passed AND evidence
    # that an analyst prepared the task — the prepared_by column, or, for rows
    # older than that column, an update carrying the marker. Replacing it with
    # the bare column would leave the same badge meaning something else, which
    # is the quiet kind of change this task exists to avoid.
    #
    # WHAT DOES CHANGE, AND IT IS NOT NOTHING: dor_passed is now read from the
    # column instead of being recomputed on every render. The two can disagree
    # for rows refined before persistence existed — that is why "lazy repair"
    # was written in the first place (#250). The repair still runs where a
    # single task is opened and at refine; it stops running a hundred times to
    # draw a list. This test pins the column as the source, deliberately.
    modern = await _ready_row(
        db, client, "prepared, modern", dor=True, prepared_by="analyst-bot"
    )
    legacy = await _ready_row(
        db, client, "prepared, legacy", dor=True, legacy_marker=True
    )
    bare = await _ready_row(db, client, "ready but unprepared", dor=True)
    unready = await _ready_row(
        db, client, "prepared but not ready", dor=False, prepared_by="analyst-bot"
    )

    from hub.web import _analyst_ready_map
    from hub import services

    tasks = await services.list_tasks(db, limit=50)
    ready = await _analyst_ready_map(db, tasks)

    assert ready[modern] is True, "prepared_by is the modern form of the signal"
    assert ready[legacy] is True, (
        "an update carrying the marker is the legacy form — dropping it would "
        "silently un-ready every task refined before the column existed"
    )
    assert ready[bare] is False, "DoR alone was never this badge's meaning"
    assert ready[unready] is False, "preparation without DoR is not readiness"

    # And the filter agrees with the badge — they are the same signal.
    page = await client.get("/tasks", params={"analyst_ready": "1"})
    assert f'id="task-row-{modern}"' in page.text
    assert f'id="task-row-{legacy}"' in page.text
    assert f'id="task-row-{bare}"' not in page.text


# ---------------------------------------------------------------------------
# The tasks section opens on what awaits a human (#630, direction A)
# ---------------------------------------------------------------------------


async def _queue_fixture(client: AsyncClient, db) -> dict[str, int]:
    """One task per kind of waiting, plus noise that must not be on the queue."""
    from hub.db import seed_default_project

    await seed_default_project(db)
    await _project_with(client, db, "queue-proj", "Queue Proj")
    epic = (
        await client.post(
            "/api/tasks",
            json={"title": "E", "task_type": "epic", "project": "queue-proj"},
        )
    ).json()["id"]

    async def _child(title: str, status: str, **cols) -> int:
        tid = (
            await client.post(
                "/api/tasks",
                json={"title": title, "task_type": "feature", "parent_id": epic},
            )
        ).json()["id"]
        await db.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        for col, val in cols.items():
            await db.execute(f"UPDATE tasks SET {col}=? WHERE id=?", (val, tid))  # noqa: S608
        return tid

    ids = {
        "draft": await _child("needs approval", "draft"),
        "question": await _child("asked something", "needs_info"),
        "decision": await _child("needs a call", "needs_decision"),
        "review": await _child("submitted for review", "review"),
        # A review the poller owns, not a person — must NOT be on the queue.
        "headless": await _child("machine review", "review", review_job_id="job-1"),
        "running": await _child("in flight", "running"),
        "queued": await _child("approved, untouched", "open"),
        "done": await _child("finished long ago", "completed"),
    }
    await db.commit()
    return ids


async def test_tasks_page_opens_on_what_awaits_a_human(client: AsyncClient, db):
    # AC-1 (#630). Measured before: the first screen held 100 rows of which 61
    # were completed and 18 were drafts waiting for this reader's approval,
    # mixed together — the section answered "what exists", never "what is
    # needed from me".
    import re

    ids = await _queue_fixture(client, db)

    page = await client.get("/tasks")
    assert page.status_code == 200, page.text
    shown = set(re.findall(r'id="(?:task-row|queue-item)-(\d+)"', page.text))

    for key in ("draft", "question", "decision", "review"):
        assert str(ids[key]) in shown, f"{key} waits for a human and must be here"
    for key in ("done", "running", "queued", "headless"):
        assert str(ids[key]) not in shown, (
            f"{key} does not wait for a human — the default screen is the queue, "
            "not the archive"
        )


async def test_mode_chip_and_its_list_agree_for_all_four(client: AsyncClient, db):
    # AC-2 (#630). The counter-vs-list divergence has shipped three times in
    # this codebase (#571, #619, #621). Here four counters sit next to their own
    # links, so the same defect has four new places to appear.
    import re

    await _queue_fixture(client, db)

    page = (await client.get("/tasks")).text
    chips = dict(
        (m.group(1), int(m.group(2)))
        for m in re.finditer(r'data-mode="(\w+)"[^>]*data-count="(\d+)"', page)
    )
    assert set(chips) == {"awaiting", "inflight", "queued", "live"}, (
        f"all four modes must be switchable, got {sorted(chips)}"
    )

    for mode, promised in chips.items():
        listing = await client.get("/tasks", params={"state": mode})
        rows = re.findall(r'id="(?:task-row|queue-item)-(\d+)"', listing.text)
        assert len(rows) == promised, (
            f"mode {mode}: the chip promises {promised}, the list holds "
            f"{len(rows)} — counter and list must come from one definition"
        )


async def test_queue_groups_by_required_action(client: AsyncClient, db):
    # AC-3 (#630). "Ждёт вас: 37" is a pile; what a reader can act on is
    # "approve 35, answer 1, decide 0, accept 1". The headless-review exclusion
    # is not invented here — it is the rule TASK_STATE_FILTERS['awaiting'] and
    # project_work_summary already follow: a review with review_job_id belongs
    # to the poller.
    ids = await _queue_fixture(client, db)
    page = (await client.get("/tasks")).text

    for group in (
        "queue-group-draft",
        "queue-group-needs_info",
        "queue-group-needs_decision",
        "queue-group-review",
    ):
        assert group in page, f"group {group} must exist, even when empty"

    assert f'id="queue-item-{ids["headless"]}"' not in page, (
        "a review owned by the poller is not a person's queue item"
    )
    assert f'id="queue-item-{ids["review"]}"' in page

    # An EMPTY group still renders, and that is the point: "Принять решение 0"
    # is an answer, while a group that disappears when it empties leaves the
    # reader wondering whether it ever existed. The fixture above fills all four
    # groups, so emptying them takes a filter — without this the rule is
    # unpinned, and a mutation that dropped empty groups stayed green.
    narrowed = (
        await client.get("/tasks", params={"state": "awaiting", "priority": "critical"})
    ).text
    assert 'id="queue-item-' not in narrowed, "precondition: nothing is critical here"
    for group in (
        "queue-group-draft",
        "queue-group-needs_info",
        "queue-group-needs_decision",
        "queue-group-review",
    ):
        assert group in narrowed, (
            f"{group} vanished when it emptied — zero has to be said out loud"
        )


async def test_queue_view_keeps_the_project_scope(client: AsyncClient, db):
    # AC-4 (#630). #626 taught the filter bar to carry the scope; the mode chips
    # are a new set of links, so they get the same rule or the defect returns in
    # a new place.
    import re

    await _queue_fixture(client, db)
    page = (await client.get("/tasks", params={"project": "queue-proj"})).text
    links = re.findall(r'data-mode="\w+"[^>]*href="([^"]+)"', page) or re.findall(
        r'href="([^"]*state=\w+[^"]*)"', page
    )
    assert links, "the mode chips must be links"
    for href in links:
        assert "project=queue-proj" in href.replace("&amp;", "&"), (
            f"switching mode must keep the project; this one drops it: {href}"
        )


async def test_queue_reuses_the_selection_approval(client: AsyncClient, db):
    # AC-5 (#630). #628 built selection-based approval; a second mechanism here
    # would be the fourth time this codebase grew two surfaces for one meaning.
    ids = await _queue_fixture(client, db)
    page = (await client.get("/tasks")).text

    assert 'name="task_ids"' in page, "drafts must be selectable in the queue"
    assert f'value="{ids["draft"]}"' in page
    assert "/tasks/web-batch-approve-selected" in page, (
        "the queue must post to the endpoint #628 already built, not a new one"
    )
