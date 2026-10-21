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
        data={**data, "verdict": "approved", "comments": "ok"},
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
