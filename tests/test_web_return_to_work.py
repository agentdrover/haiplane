"""#1995: отказ web-кнопки читается в браузере, а не сырым JSON без charset.

На проде POST /tasks/1935/web-return-to-work дал 422 «не прошло проверку поле:
reason»; браузер показал JSON кракозябрами, потому что ответ шёл как
``application/json`` без charset. Веб-маршруты на отказ сервиса (400/409/422)
отвечают страницей карточки (браузер) или фрагментом с HX-Retarget (HTMX);
/api/* остаётся JSON.
"""

from __future__ import annotations

import html
import logging

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from hub import repository as repo
from hub import web
from tests.web_support import _web_task_in_review

BROWSER = {"Accept": "text/html"}
HTMX = {"Accept": "text/html", "HX-Request": "true"}


async def _open_task(client: AsyncClient) -> int:
    return (await client.post("/api/tasks", json={"title": "Refusal open"})).json()[
        "id"
    ]


async def _running_held_task(client: AsyncClient, db) -> int:
    task_id = await _open_task(client)
    await repo.update_task(db, task_id, status="running", claimed_by="agent-a")
    await db.commit()
    return task_id


async def _status(client: AsyncClient, task_id: int) -> str:
    return (await client.get(f"/api/tasks/{task_id}")).json()["status"]


def _assert_readable_page(resp, *, status: int) -> str:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"] == "text/html; charset=utf-8", resp.headers
    text = html.unescape(resp.text)
    assert "<html" in text and 'role="alert"' in text, text[:400]
    return text


def _assert_htmx_fragment(resp, *, status: int) -> str:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("text/html"), resp.headers
    assert resp.headers.get("HX-Retarget"), "ошибка должна быть вставлена"
    assert resp.headers.get("HX-Reswap"), resp.headers
    assert "<html" not in resp.text, "HTMX получает фрагмент, не страницу"
    assert 'role="alert"' in resp.text
    return html.unescape(resp.text)


async def test_web_button_refusals_render_a_readable_page(
    client: AsyncClient, db, monkeypatch
):
    review = await _web_task_in_review(client)
    held = await _running_held_task(client, db)
    idle = await _open_task(client)
    typed = "Причина из старой вкладки"
    route = "web-return-to-work"

    # 422: пустая причина. Браузер — страница карточки с видимой ошибкой.
    resp = await client.post(
        f"/tasks/{review}/{route}", data={"reason": "   "}, headers=BROWSER
    )
    text = _assert_readable_page(resp, status=422)
    assert "Форма заполнена неверно" in text and "reason" in text
    assert f"/tasks/{review}/{route}" in text, "форма возврата на месте"
    assert await _status(client, review) == "review", "отказ не меняет состояние"

    # 409: держатель есть. Введённая причина сохранена в textarea формы.
    resp = await client.post(
        f"/tasks/{held}/{route}", data={"reason": typed}, headers=BROWSER
    )
    text = _assert_readable_page(resp, status=409)
    assert "return_running_has_work" in text and "claimed_by" in text
    area = text[text.index('id="task-return-reason"') :]
    assert typed in area[: area.index("</textarea>")], "ввод потерян"
    assert await _status(client, held) == "running"

    # 400: задача уже open (старая вкладка). Формы уже нет, ошибка есть.
    resp = await client.post(
        f"/tasks/{idle}/{route}", data={"reason": typed}, headers=BROWSER
    )
    text = _assert_readable_page(resp, status=400)
    assert "current status: open" in text and "Хаб отклонил запрос" in text
    assert await _status(client, idle) == "open"

    # HTMX: тот же статус, фрагмент и HX-Retarget/HX-Reswap.
    cases = [
        (review, {"reason": ""}, 422),
        (held, {"reason": typed}, 409),
        (idle, {"reason": typed}, 400),
    ]
    for task_id, data, status in cases:
        resp = await client.post(f"/tasks/{task_id}/{route}", data=data, headers=HTMX)
        text = _assert_htmx_fragment(resp, status=status)
        assert "<script" not in text

    # Ещё два web-* маршрута: ошибка формы (нет поля) и отказ сервиса (409).
    resp = await client.post(
        f"/tasks/{review}/web-answer", data={"resume": "true"}, headers=BROWSER
    )
    text = _assert_readable_page(resp, status=422)
    assert "answer" in text
    resp = await client.post(
        f"/tasks/{review}/web-answer", data={"resume": "true"}, headers=HTMX
    )
    _assert_htmx_fragment(resp, status=422)

    resp = await client.post(
        f"/tasks/{held}/web-decide", data={"action": "bogus"}, headers=BROWSER
    )
    text = _assert_readable_page(resp, status=400)
    assert "action" in text
    resp = await client.post(
        f"/tasks/{held}/web-decide", data={"action": "bogus"}, headers=HTMX
    )
    _assert_htmx_fragment(resp, status=400)

    # Текст отказа экранирован: ввод пользователя не становится разметкой.
    evil = {"title": "t", "task_type": "<i>z</i>"}
    resp = await client.post("/tasks/create", data=evil, headers=HTMX)
    assert resp.status_code == 400
    assert "<i>z</i>" not in resp.text and "&lt;i&gt;" in resp.text
    resp = await client.post("/tasks/create", data=evil, headers=BROWSER)
    assert "<i>z</i>" not in resp.text and "&lt;i&gt;" in resp.text

    # То же на карточке: текст сервиса экранирован и в доске ревью, и в карточке.
    async def _refuse(*_a, **_k):
        raise HTTPException(409, "<i>q</i>")

    with monkeypatch.context() as patch:
        patch.setattr(web.services, "return_to_work", _refuse)
        for task_id in (review, idle):
            resp = await client.post(
                f"/tasks/{task_id}/{route}", data={"reason": typed}, headers=BROWSER
            )
            assert resp.status_code == 409
            assert "<i>q</i>" not in resp.text and "&lt;i&gt;q" in resp.text

    # Страница отдаёт htmx правило вставки 4xx-ответов с HX-Retarget.
    shell = (await client.get("/")).text
    assert "[400, 403, 409, 422].indexOf(xhr.status)" in shell

    # Без карточки в пути — малая страница, не JSON.
    resp = await client.post(
        "/tasks/create", data={"title": "t", "task_type": "bad"}, headers=BROWSER
    )
    text = _assert_readable_page(resp, status=400)
    assert "task_type" in text

    # Клиент без Accept: text/html (curl) — прежний JSON, но с charset=utf-8.
    resp = await client.post(f"/tasks/{review}/{route}", data={"reason": " "})
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/json; charset=utf-8"
    assert "reason" in resp.json()["detail"]
    resp = await client.post(f"/tasks/{review}/web-answer", data={"resume": "true"})
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/json; charset=utf-8"
    assert isinstance(resp.json()["detail"], list)

    # /api/*: прежний JSON-контракт без изменений.
    resp = await client.post(
        f"/api/tasks/{idle}/return-to-work", json={"reason": "x"}, headers=BROWSER
    )
    assert resp.status_code == 400
    assert resp.headers["content-type"] == "application/json"
    assert "current status: open" in resp.json()["detail"]
    resp = await client.post(
        f"/api/tasks/{idle}/return-to-work", json={}, headers=BROWSER
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/json"
    assert isinstance(resp.json()["detail"], list)


def _refusal_records(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "hub.web" and "web form refused" in r.getMessage()
    ]


async def test_reason_validation_kind_is_logged(client: AsyncClient, db, caplog):
    task_id = await _web_task_in_review(client)
    route = f"/tasks/{task_id}/web-return-to-work"
    secret = "Я" * 5001
    cases = [
        ({"abandon_active_job": "false"}, "missing"),
        ({"reason": "   "}, "blank"),
        ({"reason": secret}, "too_long"),
    ]
    for data, kind in cases:
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="hub.web"):
            resp = await client.post(route, data=data, headers=BROWSER)
        assert resp.status_code == 422, resp.text
        lines = _refusal_records(caplog)
        assert len(lines) == 1, (kind, lines)
        assert f"kind={kind}" in lines[0] and "field=reason" in lines[0], lines
        assert route in lines[0], "в строке путь"
        assert "ЯЯЯ" not in lines[0] and "   " not in lines[0].split("kind=")[1], (
            "значение поля в лог не попадает"
        )
    assert await _status(client, task_id) == "review"

    page = (await client.get(f"/tasks/{task_id}")).text
    area = page[page.index('id="task-return-reason"') :]
    tag = area[: area.index(">")]
    assert 'maxlength="5000"' in tag, tag


async def test_accept_header_quality_decides_html_or_json(client: AsyncClient, db):
    """Accept читается с q: q=0 — отказ от типа, HTML не хуже JSON — страница."""
    task_id = await _open_task(client)
    url = f"/tasks/{task_id}/web-return-to-work"
    cases = [
        ("application/json, text/html;q=0", "json"),
        ("text/html;q=0.5, application/json", "json"),
        ("text/html, application/json", "html"),
        ("text/html,application/xhtml+xml,*/*;q=0.8", "html"),
        ("text/html;q=bad", "html"),
        ("*/*", "json"),
        ("", "json"),
    ]
    for accept, want in cases:
        resp = await client.post(url, data={"reason": "x"}, headers={"Accept": accept})
        assert resp.status_code == 400, (accept, resp.text)
        kind = (
            "html" if resp.headers["content-type"].startswith("text/html") else "json"
        )
        assert kind == want, (accept, resp.headers["content-type"])


@pytest.mark.parametrize(
    "state, route, fields, patched",
    [
        (
            "needs_decision",
            "web-decide",
            {
                "action": "bogus",
                "decision_summary": "<b>итог</b>",
                "instructions": "<i>фикс</i>",
            },
            None,
        ),
        ("needs_info", "web-answer", {"answer": "<b>ответ</b>"}, "answer_question"),
        ("draft", "web-reject", {"comment": "<b>причина</b>"}, "reject_task"),
        ("open", "web-message", {"body": "<b>привет</b>"}, "send_message"),
        ("review", "web-message", {"body": "<b>привет</b>"}, "send_message"),
    ],
)
async def test_refused_card_forms_keep_the_typed_text(
    client: AsyncClient, db, monkeypatch, state, route, fields, patched
):
    """Отказ не стирает ввод в форме карточки, и ввод экранирован."""
    task_id = await _open_task(client)
    await repo.update_task(db, task_id, status=state)
    await db.commit()
    if patched:

        async def _refuse(*_a, **_k):
            raise HTTPException(409, "отказ сервиса")

        monkeypatch.setattr(web.services, patched, _refuse)
    resp = await client.post(f"/tasks/{task_id}/{route}", data=fields, headers=BROWSER)
    assert resp.status_code in (400, 409), resp.text
    assert f"/tasks/{task_id}/{route}" in resp.text, "форма осталась"
    for value in fields.values():
        if "<" in value:
            assert value not in resp.text, "ввод не экранирован"
            assert value.replace("<", "&lt;").replace(">", "&gt;") in resp.text


def _tag(page: str, marker: str) -> str:
    start = page.rindex("<", 0, page.index(marker))
    return page[start : page.index(">", start) + 1]


async def test_refusal_restores_choices_and_marks_them_unsaved(client: AsyncClient, db):
    """Выбор человека переживает отказ, а автообновление его не стирает."""
    task_id = await _open_task(client)
    await repo.update_task(db, task_id, status="needs_decision")
    await db.commit()
    resp = await client.post(
        f"/tasks/{task_id}/web-decide",
        data={
            "action": "bogus",
            "pr_disposition": "abandon",
            "decision_summary": "итог",
        },
        headers=BROWSER,
    )
    assert resp.status_code == 400
    page = resp.text
    abandon = _tag(page, 'value="abandon"')
    assert " selected" in abandon, "abandon сброшен на умолчание"
    assert " selected" not in _tag(page, 'value="deliver"')
    assert "data-hub-restored" in _tag(page, 'name="pr_disposition"')
    assert "data-hub-restored" in _tag(page, 'name="decision_summary"')
    assert "data-hub-restored" not in _tag(page, 'name="instructions"')

    # Галочка «бросить job» тоже возвращается.
    held = await _open_task(client)
    await repo.update_task(db, held, status="fix_requested", job_id="job-x")
    await db.commit()
    resp = await client.post(
        f"/tasks/{held}/web-return-to-work",
        data={"reason": " ", "abandon_active_job": "true"},
        headers=BROWSER,
    )
    assert resp.status_code == 422
    box = _tag(resp.text, 'name="abandon_active_job"')
    assert " checked" in box and "data-hub-restored" in box, box

    # Статически: hubHasUnsavedInput считает такое поле грязным (поведение в
    # браузере проверяет #1996).
    shell = (await client.get("/")).text
    rule = shell[shell.index("function dirty(el)") :]
    rule = rule[: rule.index("window.hubHasUnsavedInput")]
    assert 'hasAttribute("data-hub-restored")' in rule
    assert 'el.value !== ""' in rule
