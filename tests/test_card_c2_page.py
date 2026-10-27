"""Страница карточки C2 (#1812): о чём, тон, экранирование, CSS-ограничения."""

from __future__ import annotations

import html
import re

from tests.card_c2_css_support import STATIC
from tests.test_decision_brief import _task_in


def _section(page: str, name: str) -> str:
    start = page.index(f'data-c2-section="{name}"')
    return page[start : page.index("</section>", start)]


async def test_about_prints_plain_summary_escaped(client, db):
    task_id = await _task_in(db, "running")
    marker = "<script>MARK-1812</script>"
    await db.execute(
        "UPDATE tasks SET plain_summary=?, problem_statement=? WHERE id=?",
        (marker, "Текст проблемы, который нельзя подставлять", task_id),
    )
    await db.commit()
    page = (await client.get(f"/tasks/{task_id}")).text
    about = _section(page, "about")
    assert html.escape(marker) in about
    assert marker not in page
    assert "Простым языком не описано" not in about


async def test_about_without_summary_says_so_and_folds_the_title(client, db):
    task_id = await _task_in(db, "running")
    await db.execute(
        "UPDATE tasks SET problem_statement=? WHERE id=?",
        ("Текст проблемы, который нельзя подставлять", task_id),
    )
    await db.commit()
    page = (await client.get(f"/tasks/{task_id}")).text
    about = _section(page, "about")
    assert "Простым языком не описано" in about
    assert "Текст проблемы" not in about, "решение #1811 gen3: не подставлять"
    assert re.search(r"<details[^>]*>\s*<summary>Исходное название", about)


async def test_tone_and_shape_are_in_the_markup(client, db):
    task_id = await _task_in(db, "running", review_verdict="changes_requested")
    page = (await client.get(f"/tasks/{task_id}")).text
    header = _section(page, "header")
    assert f"#{task_id}" in header
    assert re.search(r'data-tone="(red|amber|blue|green|gray)"', page)
    happened = _section(page, "happened")
    assert 'data-shape="' in happened
    assert "От вас:" in happened


async def test_missing_brief_card_is_gray_not_calm(client, db):
    task_id = await _task_in(db, "open")
    page = (await client.get(f"/tasks/{task_id}")).text
    assert 'data-tone="gray"' in page
    assert 'data-tone="green"' not in page


def test_css_keeps_phone_and_desktop_promises():
    css = (STATIC / "card_c2.css").read_text()
    assert ".c2 *, .c2-details * { min-width: 0; overflow-wrap: anywhere; }" in css
    assert ".task-hero-title" in css and css.count("overflow-wrap: anywhere") >= 2
    assert ":focus-visible" in css
    assert "env(safe-area-inset" in css
    for width in ("1100px", "768px"):
        assert f"max-width: {width}" in css or f"min-width: {width}" in css
    assert "overflow-x" in css


def _details_tag(page: str) -> str:
    return re.search(r'<details[^>]*id="card-details"[^>]*>', page).group(0)


async def test_feedback_opens_the_details(client, db):
    task_id = await _task_in(db, "running")
    quiet = (await client.get(f"/tasks/{task_id}")).text
    assert " open" not in _details_tag(quiet)
    loud = (await client.get(f"/tasks/{task_id}?approve_error=ошибка")).text
    assert " open" in _details_tag(loud), "ответ на действие спрятан в свёрнутом блоке"


async def test_action_link_points_at_the_form_that_exists(client, db):
    review = await _task_in(db, "review")
    page = (await client.get(f"/tasks/{review}")).text
    # #1813: при наличии панели «К действию» открывает главный лист; форма
    # «Подробностей» остаётся запасным путём без скрипта.
    found = re.search(r'class="c2-btn" href="#(sh-[a-z-]+)" data-sheet="\1"', page)
    assert found, "«К действию» ведёт на лист панели"
    assert f'id="{found.group(1)}"' in page
    assert 'id="review-verdict-form"' in page
    running = await _task_in(db, "running")
    page = (await client.get(f"/tasks/{running}")).text
    assert 'class="c2-btn" href="#task-controls"' in page
    assert 'id="task-controls"' in page


async def test_action_link_keeps_repair_target_and_primary_sheet(
    client, db, monkeypatch
):
    """#2035: «К действию» совмещает #1998 и #1813.

    Круг починки предложен на running с текущим вердиктом: ссылка ведёт на его
    форму - и без действий панели, и с ними (лист панели её не заслоняет).
    Без круга починки при действиях панели ссылка открывает главный лист.
    """
    from hub import web

    task_id = await _task_in(
        db,
        "running",
        result_kind="commit",
        submission_generation=1,
        review_verdict="changes_requested",
        review_verdict_generation=1,
    )

    offered = {"repair": True}

    async def offers(_db, _task_id, _row):
        return [], offered["repair"], False

    monkeypatch.setattr(web, "_card_executor_offers", offers)
    real = web.card_actions.for_page

    def no_actions(*args, **kwargs):
        return {**real(*args, **kwargs), "actions": []}

    monkeypatch.setattr(web.card_actions, "for_page", no_actions)
    page = (await client.get(f"/tasks/{task_id}")).text
    assert 'class="c2-btn" href="#executor-repair-form"' in page
    assert 'class="c2-btn" href="#review-replace-form"' not in page

    monkeypatch.setattr(web.card_actions, "for_page", real)
    page = (await client.get(f"/tasks/{task_id}")).text
    assert 'class="c2-btn" href="#executor-repair-form"' in page, (
        "лист не заслоняет круг"
    )

    offered["repair"] = False
    page = (await client.get(f"/tasks/{task_id}")).text
    found = re.search(r'class="c2-btn" href="#(sh-[a-z-]+)" data-sheet="\1"', page)
    assert found, "с действиями и без круга починки - главный лист"
    assert f'id="{found.group(1)}"' in page
