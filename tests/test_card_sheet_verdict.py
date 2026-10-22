"""Лист «На доработку» и вердикт с карточки (#1813, AC-1)."""

from __future__ import annotations

from httpx import AsyncClient

from tests.card_sheet_support import SHEET_HEADERS, sheet_form
from tests.web_support import _web_task_in_review


async def test_sheet_changes_requested_posts_generation(client: AsyncClient):
    task_id = await _web_task_in_review(client)
    page = (await client.get(f"/tasks/{task_id}")).text
    form, data = sheet_form(page, "sh-cr")
    assert form.attrs["action"] == f"/tasks/{task_id}/web-review-verdict"
    gen = (await client.get(f"/api/tasks/{task_id}")).json()["submission_generation"]
    assert data["expected_generation"] == str(gen)

    resp = await client.post(
        form.attrs["action"],
        data={
            **data,
            "verdict": "changes_requested",
            "comments": "лист: причина возврата",
            "findings_text": "high: гонка @ hub/a.py:12",
        },
        headers=SHEET_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert "Отправлено" in resp.text and "data-c2-done" in resp.text

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["latest_review"]["verdict"] == "changes_requested"
    assert body["latest_review"]["submission_generation"] == gen
    assert body["latest_review"]["findings"][0]["message"] == "гонка"


async def test_sheet_answer_names_the_live_status_not_a_promise(client: AsyncClient):
    task_id = await _web_task_in_review(client)
    page = (await client.get(f"/tasks/{task_id}")).text
    sheet = "sh-ok-risk" if 'id="sh-ok-risk"' in page else "sh-ok"
    form, data = sheet_form(page, sheet)
    resp = await client.post(
        form.attrs["action"],
        data={**data, "verdict": "approved", "comments": "ok", "confirm_risk": "1"},
        headers=SHEET_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    live = (await client.get(f"/api/tasks/{task_id}")).json()["status"]
    assert f"<code>{live}</code>" in resp.text
    for promise in ("исполнитель исправит", "гейт вольёт сам"):
        assert promise not in resp.text


async def test_sheet_refusal_is_visible_text_and_keeps_the_record_clean(
    client: AsyncClient,
):
    task_id = await _web_task_in_review(client)
    page = (await client.get(f"/tasks/{task_id}")).text
    form, data = sheet_form(page, "sh-cr")
    resp = await client.post(
        form.attrs["action"],
        data={**data, "verdict": "changes_requested", "comments": ""},
        headers=SHEET_HEADERS,
    )
    assert resp.status_code == 422
    assert 'class="c2-sheet-note"' in resp.text and "<script" not in resp.text
    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["latest_review"] is None


async def _risk_sheet(client: AsyncClient):
    task_id = await _web_task_in_review(client)
    page = (await client.get(f"/tasks/{task_id}")).text
    form, data = sheet_form(page, "sh-ok-risk")
    assert data["risk_ack"] == "required"
    return task_id, form, {**data, "verdict": "approved"}


async def test_high_risk_accept_needs_the_tick_on_the_server(client: AsyncClient):
    task_id, form, data = await _risk_sheet(client)
    resp = await client.post(
        form.attrs["action"],
        data={**data, "comments": "риск приемлем"},
        headers=SHEET_HEADERS,
    )
    assert resp.status_code == 422 and "подтвержд" in resp.text
    assert 'class="c2-sheet-note"' in resp.text
    assert (await client.get(f"/api/tasks/{task_id}")).json()["latest_review"] is None


async def test_high_risk_accept_needs_a_real_reason(client: AsyncClient):
    task_id, form, data = await _risk_sheet(client)
    resp = await client.post(
        form.attrs["action"],
        data={**data, "confirm_risk": "1", "comments": "   "},
        headers=SHEET_HEADERS,
    )
    assert resp.status_code == 422 and "причин" in resp.text
    assert (await client.get(f"/api/tasks/{task_id}")).json()["latest_review"] is None


async def test_high_risk_accept_records_confirmation_and_reason(
    client: AsyncClient, db
):
    task_id, form, data = await _risk_sheet(client)
    resp = await client.post(
        form.attrs["action"],
        data={
            **data,
            "confirm_risk": "1",
            "comments": "  риск приемлем: флаг выключен ",
        },
        headers=SHEET_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    review = (await client.get(f"/api/tasks/{task_id}")).json()["latest_review"]
    assert review["verdict"] == "approved"
    from hub import repository as repo

    feed = " ".join(
        str(dict(u)["content"]) for u in await repo.get_task_updates(db, task_id)
    )
    assert "риск приемлем: флаг выключен" in feed
    assert "при высоком риске" in feed


async def test_plain_verdicts_do_not_need_the_risk_tick(client: AsyncClient):
    task_id = await _web_task_in_review(client)
    resp = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={"verdict": "approved", "comments": "ok"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


async def test_risk_sheet_cannot_carry_another_verdict(client: AsyncClient):
    task_id, form, data = await _risk_sheet(client)
    resp = await client.post(
        form.attrs["action"],
        data={**data, "verdict": "changes_requested", "comments": ""},
        headers=SHEET_HEADERS,
    )
    assert resp.status_code == 422
    assert (await client.get(f"/api/tasks/{task_id}")).json()["latest_review"] is None


async def test_risk_sheet_keeps_the_text_vs_verdict_contradiction_guard(
    client: AsyncClient,
):
    task_id, form, data = await _risk_sheet(client)
    resp = await client.post(
        form.attrs["action"],
        data={
            **data,
            "confirm_risk": "1",
            "comments": "CHANGES_REQUESTED: сначала исправьте",
        },
        headers=SHEET_HEADERS,
    )
    assert resp.status_code == 422, resp.text
    assert (await client.get(f"/api/tasks/{task_id}")).json()["latest_review"] is None


async def test_partial_state_evidence_offers_the_risk_sheet(client: AsyncClient, db):
    """Риск, повторённый в «почему», не пропадает из выбора листа (#1813 gen2)."""
    from hub import web
    from hub.services import card_c2, decision_brief
    from tests.state_support import drive_to_review, make_state_task

    task_id = await make_state_task(db)
    await drive_to_review(client, db, task_id)
    await db.execute(
        "DELETE FROM task_evidence WHERE task_id=? AND ac_id='AC-2'", (task_id,)
    )
    await db.commit()
    from hub import repository as repo

    row = dict(await repo.get_task(db, task_id))
    brief = await decision_brief.build(db, row, {"repair_offered": False})
    assert any(r["severity"] == "high" for r in brief["risks"]), "исходный риск есть"
    view = card_c2.card_view(row, brief)
    assert not [r for r in view["risks"] if r["severity"] == "high"], (
        "после сокращения повторов риска в показе уже нет"
    )
    page = (await client.get(f"/tasks/{task_id}")).text
    assert 'id="sh-ok-risk"' in page and 'id="sh-ok"' not in page
    form, data = sheet_form(page, "sh-ok-risk")
    assert data["risk_ack"] == "required"
    assert web  # импорт нужен только для явности зависимости
