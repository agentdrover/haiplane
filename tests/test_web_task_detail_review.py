"""#1207: the task card in review puts the submission where the reader is.

One template serves every status, and in review the reader has one job — a
verdict. Until #1207 the material for that verdict sat in a 360px sidebar,
the criteria evidence inside a 180px scroll box, while the wide column held
the statement the same person had approved at DoR. These tests pin the
review layout by ORDER in the markup and by the absence of the note class
that capped the evidence, not by pixels: a template is what a test can read.

Fixtures are the ones #823 introduced — the layout is judged on the same
page the evidence panel was judged on.
"""

from __future__ import annotations

import re

from httpx import AsyncClient

from tests.test_web import (
    _machine_report,
    _web_task_in_review,
    _web_task_in_review_with_test_ac,
)

STRIP_MARKER = "task-review-strip"


def _description_at(page: str, text: str) -> int:
    # The description is printed inside its own container; the same words may
    # legitimately appear in the hero title, so the anchor is the container.
    marker = 'class="task-description-text'
    assert marker in page, "the description container must exist"
    return page.index(text, page.index(marker))


async def _review_task_with_two_acs(client: AsyncClient, db) -> int:
    from hub import repository as repo_module

    task_id = await _web_task_in_review(client)
    resp = await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "description": "Статья постановки номер тысяча двести семь",
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "given": "панель собрана",
                    "when": "открыта карточка",
                    "then": "первое ожидание печатается однажды",
                    "verifiable_by": "test",
                    "test_ref": "tests/test_web.py::test_evidence_panel_renders_at_the_verdict_gate",
                },
                {
                    "id": "AC-2",
                    "given": "панель собрана",
                    "when": "открыта карточка",
                    "then": "второе ожидание печатается однажды",
                    "verifiable_by": "test",
                    "test_ref": "tests/test_web.py::test_missing_evidence_names_its_cause",
                },
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    row = dict(await repo_module.get_task(db, task_id))
    for ac_id in ("AC-1", "AC-2"):
        await repo_module.upsert_ac_test_result(
            db,
            task_id=task_id,
            ac_id=ac_id,
            generation=row["submission_generation"],
            status="pass",
        )
    await db.commit()
    return task_id


# ---- AC-1: in review the evidence and the verdict come before the statement


async def test_review_layout_puts_evidence_before_statement(client: AsyncClient, db):
    task_id = await _web_task_in_review_with_test_ac(client, db)
    await client.post(
        f"/api/tasks/{task_id}/refine",
        json={"description": "Постановка, утверждённая на DoR"},
    )

    page = (await client.get(f"/tasks/{task_id}")).text

    evidence_at = page.index("Проверено по этой сдаче:")
    verdict_at = page.index("web-review-verdict")
    description_at = _description_at(page, "Постановка, утверждённая на DoR")
    summary_at = page.index("что говорит исполнитель")
    assert evidence_at < verdict_at < description_at, (
        "in review the reader decides first and re-reads the statement second"
    )
    # #823 AC-4 keeps its order inside the new column.
    assert evidence_at < summary_at < verdict_at

    # The evidence used to live in a note capped at 180px. Its container is
    # its own thing now: nothing between the evidence heading and the first
    # criterion opens a task-action-note.
    head = page[page.rfind("<", 0, evidence_at) : evidence_at]
    assert "task-action-note" not in head, "evidence must not sit in the capped note"
    container_open = page.rfind("<div", 0, evidence_at)
    assert 'class="task-evidence"' in page[container_open:evidence_at]


# ---- AC-2: a summary strip names every evidence block, silence included


async def test_review_summary_strip_names_every_block(client: AsyncClient, db):
    task_id = await _web_task_in_review_with_test_ac(client, db)

    page = (await client.get(f"/tasks/{task_id}")).text
    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()

    assert STRIP_MARKER in page
    strip = page[page.index(STRIP_MARKER) : page.index("task-evidence")]
    for label in (
        "Критерии",
        "CI по коммиту",
        "Предпас",
        "Машинное ревью",
        "Свежесть постановки",
        "Без запроса",
    ):
        assert label in strip, f"the strip must name {label!r}"
    assert re.search(r"1\s*из\s*1", strip), "criteria are counted as N из M"

    # A block that went silent is named with its cause — the fixture has gaps
    # (no CI run, no prepass) and each one says why.
    missing = brief["evidence_coverage"]["checks_missing"]
    assert missing, "the fixture must have blocks without a signal"
    assert "не дали сигнала" in page
    for check in missing:
        assert check["reason"] in page, f"{check['check']} went silent without a cause"


# ---- AC-3: every other status renders as before


async def test_non_review_statuses_keep_statement_first(client: AsyncClient, db):
    draft = (
        await client.post(
            "/api/tasks",
            json={"title": "Draft stays", "source": "agent", "agent": "bot"},
        )
    ).json()["id"]
    opened = (await client.post("/api/tasks", json={"title": "Open stays"})).json()[
        "id"
    ]
    done = (await client.post("/api/tasks", json={"title": "Done stays"})).json()["id"]
    await db.execute("UPDATE tasks SET status='completed' WHERE id=?", (done,))
    await db.commit()
    for task_id in (draft, opened, done):
        await client.post(
            f"/api/tasks/{task_id}/refine",
            json={"description": f"Описание задачи {task_id} стоит первым"},
        )

    for task_id in (draft, opened, done):
        page = (await client.get(f"/tasks/{task_id}")).text
        assert page.count("task-content-card") >= 1
        description_at = _description_at(
            page, f"Описание задачи {task_id} стоит первым"
        )
        first_card = page.index("task-content-card")
        assert first_card < description_at < page.index("task-sidebar-card"), (
            f"#{task_id}: the description is the first card of the main column"
        )
        assert "Task Controls" in page
        assert STRIP_MARKER not in page, f"#{task_id}: no review strip outside review"
        assert "web-review-verdict" not in page


# ---- AC-4: each fact once — Then text, hero badges


async def test_review_page_prints_each_fact_once(client: AsyncClient, db):
    task_id = await _review_task_with_two_acs(client, db)

    page = (await client.get(f"/tasks/{task_id}")).text

    for then in (
        "первое ожидание печатается однажды",
        "второе ожидание печатается однажды",
    ):
        assert page.count(then) == 1, f"{then!r} printed {page.count(then)} times"
    for badge in (
        "badge-review",
        "badge-type-task",
        "badge-priority-medium",
        "badge-human",
        "badge-auto",
    ):
        # The hero carries the badge; the sidebar used to repeat it.
        assert page.count(f"badge {badge}") == 1, f"{badge} printed more than once"


# ---- AC-5: the two verdict buttons are one pair


async def test_verdict_buttons_are_adjacent(client: AsyncClient):
    task_id = await _web_task_in_review(client)

    page = (await client.get(f"/tasks/{task_id}")).text

    form_at = page.index('id="review-verdict-form"')
    form = page[form_at : page.index("</form>", form_at)]
    approve_at = form.index('value="approved"')
    changes_at = form.index('value="changes_requested"')
    between = form[min(approve_at, changes_at) : max(approve_at, changes_at)]
    assert "<textarea" not in between, "no field may separate the two verdicts"
    assert 'id="review-cr-hint"' in form
    assert 'id="review-ack-repeat"' in form


# ---- AC-6: empty sections shrink to a line but keep saying what is absent


def _element_text(page: str, anchor: str) -> str:
    """The markup of the element carrying ``anchor`` in its opening tag."""
    at = page.index(anchor)
    open_at = page.rfind("<", 0, at)
    tag = re.match(r"<(\w+)", page[open_at:]).group(1)
    close_at = page.index(f"</{tag}>", at)
    return page[open_at:close_at]


async def test_empty_sections_collapse_to_a_line_but_stay_named(client: AsyncClient):
    task_id = await _web_task_in_review(client)
    resp = await client.post(f"/api/tasks/{task_id}/refine", json={"work_type": "bug"})
    assert resp.status_code == 200, resp.text

    page = (await client.get(f"/tasks/{task_id}")).text

    # The evidence block may quote the same absence as a silent check — that
    # is its own sentence. This test reads the element each section became.
    for anchor, phrase in (
        ('id="task-live-checks"', "никто не наблюдал"),
        ('id="task-messages"', "не переписывались"),
        ("data-defect-passport", "не заполнен"),
    ):
        element = _element_text(page, anchor)
        assert phrase in element, f"the absence {phrase!r} must still be stated"
        assert element.startswith("<div"), f"{anchor} is a line in review, not a card"
        assert "task-content-card" not in element.split(">", 1)[0]
    # The message form survives the collapse: a line is not a dead end.
    assert f"/tasks/{task_id}/web-message" in page


# ---- Finding 43845acc (machine review of submission #1): the strip must not
# read "0 / N" as a clean review when the run was incomplete, left findings
# unjudged, or was the implementer's own. The block below says all that in
# words (#549, #728); the tile is what the eye reads first, so it says it too.


async def test_strip_names_incomplete_and_unresolved_review(client: AsyncClient, db):
    task_id = await _web_task_in_review_with_test_ac(client, db)
    await _machine_report(
        client,
        task_id,
        findings_confirmed=[],
        incomplete=True,
        unresolved=[{"title": "shadow path", "why": "no agent could judge it"}],
    )

    page = (await client.get(f"/tasks/{task_id}")).text

    strip = page[page.index(STRIP_MARKER) : page.index("task-evidence")]
    assert "0 / " in strip
    assert "прогон неполный" in strip
    assert "1 не проверено" in strip
    tile_at = strip.index("Машинное ревью")
    tile_open = strip.rfind('<div class="task-review-tile', 0, tile_at)
    assert "task-review-tile--ok" not in strip[tile_open:tile_at], (
        "an incomplete run with unjudged findings is not a green tile"
    )
