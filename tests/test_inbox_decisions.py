"""Входящие и карточка читают ОДНУ справку (#1810, AC-4)."""

from __future__ import annotations

import aiosqlite

from hub import config
from hub import repository as repo
from hub import repository_ci_integrity as integrity_store
from hub import services, web
from hub.services import decision_brief
from tests.test_auto_verdict import _post_review, _submitted_task


async def test_inbox_and_card_share_brief(
    client, db: aiosqlite.Connection, monkeypatch
):
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "off")
    task_id = await _submitted_task(client, db, "brief-share", {"verdict": "human"})
    await _post_review(client, task_id)
    task = dict(await repo.get_task(db, task_id) or {})
    await integrity_store.save_ci_integrity_scan(
        db, task_id, task["submission_sha"], "clean", "[]", ""
    )
    await db.execute(
        "UPDATE machine_reviews SET principal_id=9, provider_tokens=250000 "
        "WHERE task_id=?",
        (task_id,),
    )
    await db.commit()

    inbox = await services.get_inbox_data(db)
    (row,) = [r for r in inbox["decision_queue"] if r["task_id"] == task_id]
    card = await web._card_decision_brief(db, task, False)

    assert card is not None
    assert row["brief_line"] == decision_brief.recommend_line(card)
    assert row["brief_line"].startswith("Принять"), "случай чистый: советуем принять"

    page = await client.get(f"/tasks/{task_id}")
    assert page.status_code == 200
