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


async def test_reject_sheet_pins_statement_generation_too(client, db):
    task_id = await _draft_with_readiness(client, "reject me", ready=True)
    page = (await client.get(f"/tasks/{task_id}")).text
    form, data = sheet_form(page, "sh-reject")
    assert form.attrs["action"] == f"/tasks/{task_id}/web-reject"
    await db.execute(
        "UPDATE tasks SET statement_generation = statement_generation + 1 WHERE id=?",
        (task_id,),
    )
    await db.commit()
    stale = await client.post(form.attrs["action"], data=data, headers=SHEET_HEADERS)
    assert stale.status_code == 409
    assert await _status(db, task_id) == "draft"
    fresh = (await client.get(f"/tasks/{task_id}")).text
    _, fresh_data = sheet_form(fresh, "sh-reject")
    ok = await client.post(
        f"/tasks/{task_id}/web-reject", data=fresh_data, headers=SHEET_HEADERS
    )
    assert ok.status_code == 200 and "data-c2-done" in ok.text
    assert await _status(db, task_id) == "rejected"


async def test_guard_and_write_share_one_write_lock(client, db, monkeypatch):
    """Сверка ожиданий и запись - в одной транзакции, а не друг за другом."""
    from hub import services as svc_pkg
    from hub import web

    task_id = await _draft_with_readiness(client, "lock probe", ready=True)
    page = (await client.get(f"/tasks/{task_id}")).text
    form, data = sheet_form(page, "sh-reject")
    seen: dict[str, bool] = {}
    original = web.services.reject_task

    async def spy(conn, *args, **kwargs):
        seen["in_transaction"] = bool(conn.in_transaction)
        return await original(conn, *args, **kwargs)

    monkeypatch.setattr(web.services, "reject_task", spy)
    resp = await client.post(form.attrs["action"], data=data, headers=SHEET_HEADERS)
    assert resp.status_code == 200, resp.text
    assert seen == {"in_transaction": True}
    assert svc_pkg  # импорт нужен только для явности зависимости


async def test_approve_checks_the_generation_where_it_writes(client, db):
    """Ожидание поколения сверяется в сервисе, под тем же локом, что и переход."""
    import pytest
    from fastapi import HTTPException

    from hub import services
    from hub.models import TaskApprove
    from hub.services import expected_state

    task_id = await _draft_with_readiness(client, "atomic approve", ready=True)
    live = int((await repo.get_task(db, task_id))["statement_generation"] or 0)
    with expected_state.expecting_statement_generation(live + 1):
        with pytest.raises(HTTPException) as moved:
            await services.approve_task(db, task_id, TaskApprove())
    assert moved.value.status_code == 409
    assert await _status(db, task_id) == "draft"
    with expected_state.expecting_statement_generation(live):
        await services.approve_task(db, task_id, TaskApprove())
    assert await _status(db, task_id) != "draft"


async def test_sheet_is_for_a_human_only(client, db, monkeypatch):
    from hub import config
    from tests.web_support import _web_project_tokens, _web_task_in_review

    task_id = await _web_task_in_review(client)
    page = (await client.get(f"/tasks/{task_id}")).text
    form, data = sheet_form(page, "sh-cr")
    monkeypatch.setattr(config, "HUB_TOKENS", _web_project_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    resp = await client.post(
        form.attrs["action"],
        data={**data, "verdict": "changes_requested", "comments": "агент"},
        headers={**SHEET_HEADERS, "Authorization": "Bearer agent-token"},
    )
    assert resp.status_code == 403
    assert 'class="c2-sheet-note"' in resp.text
    row = await repo.get_task(db, task_id)
    assert row["review_verdict"] is None


async def test_repeating_the_same_verdict_is_named_a_repeat(client, db):
    from tests.web_support import _web_task_in_review

    task_id = await _web_task_in_review(client)
    page = (await client.get(f"/tasks/{task_id}")).text
    form, data = sheet_form(page, "sh-cr")
    body = {**data, "verdict": "changes_requested", "comments": "первый"}
    first = await client.post(form.attrs["action"], data=body, headers=SHEET_HEADERS)
    assert first.status_code == 200 and "data-c2-done" in first.text
    again = await client.post(form.attrs["action"], data=body, headers=SHEET_HEADERS)
    assert again.status_code == 409, again.text
    assert "data-c2-done" not in again.text and "уже стоит" in again.text


async def test_double_tap_on_rework_dispatches_once(client, db, monkeypatch):
    """Сервис коммитит до сети: второй запрос не проходит тот же гард."""
    import asyncio

    from hub.services import lifecycle

    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    page = (await client.get(f"/tasks/{task_id}")).text
    form, data = sheet_form(page, "sh-rework")
    calls: list[int] = []

    async def slow_dispatch(conn, tid, task, instructions, *_booking):
        calls.append(tid)
        await asyncio.sleep(0.05)
        await repo.update_task(conn, tid, status="open")
        await conn.commit()

    monkeypatch.setattr(lifecycle, "_dispatch_rework", slow_dispatch)
    body = {**data, "action": "rework", "instructions": "починить"}
    first, second = await asyncio.gather(
        client.post(form.attrs["action"], data=body, headers=SHEET_HEADERS),
        client.post(form.attrs["action"], data=body, headers=SHEET_HEADERS),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    assert calls == [task_id]


async def test_parallel_identical_verdicts_say_which_one_wrote(client, db):
    import asyncio

    from tests.web_support import _web_task_in_review

    task_id = await _web_task_in_review(client)
    page = (await client.get(f"/tasks/{task_id}")).text
    form, data = sheet_form(page, "sh-cr")
    body = {**data, "verdict": "changes_requested", "comments": "двойной тап"}
    got = await asyncio.gather(
        *[
            client.post(form.attrs["action"], data=body, headers=SHEET_HEADERS)
            for _ in range(2)
        ]
    )
    assert sorted(r.status_code for r in got) == [200, 409]
    assert sum("data-c2-done" in r.text for r in got) == 1


async def test_sheets_container_exists_even_when_no_sheet_is_offered(client, db):
    """Страница в ci_check: после перехода в review лист должен уже ждать место."""
    task_id = await _task_in(db, "ci_check")
    page = (await client.get(f"/tasks/{task_id}")).text
    assert 'id="c2-sheets"' in page and "window.hubSheets" in page
    assert "data-built-review-verdict-revision" in page
    assert "data-review-verdict-revision" in page
