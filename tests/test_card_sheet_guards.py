"""Защита листов от устаревшего экрана по каждому действию (#1813).

approve/reject - поколение постановки; verdict - поколение сдачи (в сервисе);
decide и return-to-work - ожидаемое состояние и поколение сдачи. Конфликт - 409
с честным текстом, записей нет; повтор того же нажатия - тоже 409.
"""

from __future__ import annotations

from hub import repository as repo
from tests.card_sheet_support import SHEET_HEADERS, sheet_form
from tests.test_decision_brief import _task_in
from tests.web_support import _draft_with_readiness


async def _status(db, task_id: int) -> str:
    return str((await repo.get_task(db, task_id))["status"])


async def test_approve_sheet_pins_statement_generation(client, db):
    task_id = await _draft_with_readiness(client, "ready", ready=True)
    page = (await client.get(f"/tasks/{task_id}")).text
    form, data = sheet_form(page, "sh-approve")
    assert form.attrs["action"] == f"/tasks/{task_id}/web-approve"
    gen = (await repo.get_task(db, task_id))["statement_generation"]
    assert data["expected_statement_generation"] == str(gen)

    # Постановку переписали, пока лист был открыт.
    await db.execute(
        "UPDATE tasks SET statement_generation = statement_generation + 1 WHERE id=?",
        (task_id,),
    )
    await db.commit()
    stale = await client.post(form.attrs["action"], data=data, headers=SHEET_HEADERS)
    assert stale.status_code == 409
    assert "постановк" in stale.text and 'class="c2-sheet-note"' in stale.text
    assert await _status(db, task_id) == "draft"


async def test_approve_sheet_reports_the_live_status_and_replay_is_refused(client, db):
    task_id = await _draft_with_readiness(client, "ready", ready=True)
    page = (await client.get(f"/tasks/{task_id}")).text
    form, data = sheet_form(page, "sh-approve")
    first = await client.post(form.attrs["action"], data=data, headers=SHEET_HEADERS)
    assert first.status_code == 200, first.text
    live = await _status(db, task_id)
    assert live != "draft" and f"<code>{live}</code>" in first.text
    assert "data-c2-done" in first.text
    again = await client.post(form.attrs["action"], data=data, headers=SHEET_HEADERS)
    assert again.status_code == 409
    assert await _status(db, task_id) == live


async def test_dor_refusal_is_not_reported_as_sent(client, db):
    task_id = await _draft_with_readiness(client, "not ready", ready=False)
    page = (await client.get(f"/tasks/{task_id}")).text
    form, data = sheet_form(page, "sh-approve")
    resp = await client.post(form.attrs["action"], data=data, headers=SHEET_HEADERS)
    assert resp.status_code == 422
    assert "data-c2-done" not in resp.text and "Отправлено" not in resp.text
    assert await _status(db, task_id) == "draft"


async def test_decide_sheet_refuses_a_moved_state(client, db):
    task_id = await _task_in(db, "needs_decision")
    page = (await client.get(f"/tasks/{task_id}")).text
    form, data = sheet_form(page, "sh-rework")
    assert data["expected_status"] == "needs_decision"
    await db.execute("UPDATE tasks SET status='failed' WHERE id=?", (task_id,))
    await db.commit()
    resp = await client.post(
        form.attrs["action"],
        data={**data, "action": "rework", "instructions": "починить"},
        headers=SHEET_HEADERS,
    )
    assert resp.status_code == 409
    assert "needs_decision" in resp.text and "failed" in resp.text
    assert await _status(db, task_id) == "failed"


async def test_return_to_work_sheet_refuses_a_new_submission(client, db):
    task_id = await _task_in(db, "review", submission_generation=2, claimed_by="bot")
    page = (await client.get(f"/tasks/{task_id}")).text
    form, data = sheet_form(page, "sh-return")
    assert data["expected_submission_generation"] == "2"
    await db.execute(
        "UPDATE tasks SET submission_generation = 3 WHERE id=?", (task_id,)
    )
    await db.commit()
    resp = await client.post(
        form.attrs["action"],
        data={**data, "reason": "исполнитель пропал"},
        headers=SHEET_HEADERS,
    )
    assert resp.status_code == 409
    assert "сдач" in resp.text
    assert await _status(db, task_id) == "review"


async def test_native_post_without_script_gets_the_conflict_on_the_page(client, db):
    task_id = await _draft_with_readiness(client, "ready", ready=True)
    page = (await client.get(f"/tasks/{task_id}")).text
    form, data = sheet_form(page, "sh-approve")
    await db.execute(
        "UPDATE tasks SET statement_generation = statement_generation + 1 WHERE id=?",
        (task_id,),
    )
    await db.commit()
    resp = await client.post(form.attrs["action"], data=data, follow_redirects=False)
    assert resp.status_code == 303
    assert "review_error=" in resp.headers["location"]
    assert await _status(db, task_id) == "draft"


async def test_old_forms_without_expectations_still_work(client, db):
    """Формы «Подробностей» не несут expected_*: гард не должен их ломать."""
    task_id = await _draft_with_readiness(client, "ready", ready=True)
    resp = await client.post(
        f"/tasks/{task_id}/web-approve", data={}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert await _status(db, task_id) != "draft"
