"""Блок справки в HTML карточки (#1810, AC-4): тот же build(), что у входящих."""

from __future__ import annotations

import html

import aiosqlite

from hub import repository as repo
from hub.services import decision_brief
from tests.test_decision_brief_whitelist import _task


async def test_the_card_page_shows_the_brief_block(
    client, db: aiosqlite.Connection, monkeypatch
):
    task_id, _ = await _task(client, db, monkeypatch, "card-accept", 1)
    task = dict(await repo.get_task(db, task_id) or {})
    brief = await decision_brief.build(db, task)
    assert brief["accept_proven"]
    assert brief["recommend"]["action"] == "Принять"

    page = (await client.get(f"/tasks/{task_id}")).text
    assert 'id="decision-brief"' in page
    assert 'data-decision-action="Принять"' in page
    for why in brief["recommend"]["why"]:
        assert html.escape(why) in page, why
    for check in brief["checks"]:
        assert html.escape(check["name"]) in page


async def test_the_card_block_shows_doubt_and_escapes_text(
    client, db: aiosqlite.Connection, monkeypatch
):
    task_id, ids = await _task(client, db, monkeypatch, "card-doubt", 1)
    await db.execute(
        "UPDATE machine_reviews SET provider_tokens=NULL WHERE id=?", (ids[0],)
    )
    marker = "<script>MARK-1810</script>"
    await db.execute(
        "UPDATE tasks SET risks=? WHERE id=?",
        (
            '[{"kind": "other", "severity": "high", "description": "%s"}]'
            % marker.replace('"', "'"),
            task_id,
        ),
    )
    await db.commit()
    page = (await client.get(f"/tasks/{task_id}")).text
    assert 'data-decision-action="Проверить"' in page
    assert html.escape(marker.replace('"', "'")) in page
    assert marker not in page, "текст риска не экранирован"
