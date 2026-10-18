"""Обратная связь запуска исполнителя на карточке (#1817).

Сообщение после «Ок», плашка шагов, кнопка неактивна, пока идёт заказ; единая
таблица причин закрытия; одна итоговая запись в ленте на заказ; ни кода, ни
секретов в плашке.
"""

from __future__ import annotations

import pytest

from hub import config
from hub.csrf import token_for_session_cookie
from hub.services import admin as admin_svc
from hub.services import executor_orders as orders
from hub.services import launch_feedback
from tests import test_executor_orders as _eo
from tests.test_executor_orders import (
    HUB_URL,
    _code_of,
    _launch,
    _launch_redeem,
    _order_row,
    _orders,
    _redeem,
    _rows,
    _task,
)

world = _eo.world
keys = _eo.keys
_clean_limiter = _eo._clean_limiter
_no_cloud = _eo._no_cloud


async def _cookie(world):
    session = await admin_svc.create_browser_session(world.db, world.human_id)
    await world.db.commit()
    return (
        {"Cookie": f"{config.HUB_COOKIE_NAME}={session}", "Origin": HUB_URL},
        token_for_session_cookie(session),
    )


async def test_launch_message_and_repeat_refused(world):
    """AC-1: после «Ок» — «Заказ #N создан, ждёт k8»; повтор — 409 с номером; облако — своё."""
    chosen = await _task(world, "запуск с карточки")
    headers, csrf = await _cookie(world)

    async def press(host: str = "k8"):
        return await world.client.post(
            f"/tasks/{chosen}/web-executor-slot",
            data={"host": host, "csrf_token": csrf},
            headers=headers,
            follow_redirects=True,
        )

    ok = await press()
    assert ok.status_code == 200, ok.text
    placed = await _rows(world.db, "SELECT id FROM executor_orders")
    assert len(placed) == 1
    number = placed[0]["id"]
    assert f"Заказ #{number} создан, ждёт k8" in ok.text
    # Кнопка неактивна и называет шаг.
    assert (
        "disabled" in ok.text
        and "отправлен хосту" not in ok.text.split("disabled")[0][-0:]
    )
    # Повтор при активном заказе: 409 с номером, второго заказа нет.
    again = await world.client.post(
        f"/tasks/{chosen}/web-executor-slot",
        data={"host": "k8", "csrf_token": csrf},
        headers=headers,
        follow_redirects=False,
    )
    assert again.status_code == 409 and f"#{number}" in again.text
    rest = await world.client.post(
        f"/api/projects/{world.slug}/executor-launch",
        json={"target": "slot:k8", "task_id": chosen},
        headers=world.human_auth,
    )
    assert rest.status_code == 409 and f"#{number}" in rest.text
    assert len(await _rows(world.db, "SELECT 1 FROM executor_orders")) == 1
    # Облако: свои состояния executor_runs, без номера заказа.
    cloud_task = await _task(world, "облако")
    await world.db.execute(
        "INSERT INTO executor_runs (task_id, submission_generation, channel, outcome) "
        "VALUES (?, 1, 'cloud', 'running')",
        (cloud_task,),
    )
    await world.db.commit()
    plaque = await launch_feedback.plaque(world.db, cloud_task)
    assert plaque is not None and plaque["kind"] == "cloud" and plaque["active"]
    assert plaque["headline"] == "Прогон запущен в облаке"
    assert "Заказ #" not in plaque["headline"]


CLOSE_CASES = [
    ("submitted", "сдан"),
    ("expired", "не выкуплен"),
    ("session_gone", "сессия"),
    ("run_finished", "прогон"),
    ("host_disabled", "отключ"),
    ("redeem_failed", "выкуп"),
    ("redeem_lost", "потерян"),
    ("reported_failed", "упал"),
    ("reported_rescued", "спас"),
]


async def _close(world, order_id: int, reason: str) -> None:
    order = await _order_row(world, order_id)
    await world.db.commit()
    assert await orders.close_order(
        world.db, order, reason, "aborted", return_task=False
    )
    await world.db.commit()


async def test_outcome_table_and_single_feed_entry(world):
    """AC-3: все причины закрытия → человеческий текст; в ленте одна итоговая запись."""
    texts = {}
    for reason, _frag in CLOSE_CASES:
        texts[reason] = launch_feedback.close_reason_text(reason, "")
        assert texts[reason] and reason not in texts[reason]
    assert len(set(texts.values())) == len(texts)
    for reason, frag in CLOSE_CASES:
        assert frag in texts[reason].lower(), (reason, texts[reason])
    # Код причины хоста перекрывает общее «упал»; неизвестный код — other.
    assert (
        launch_feedback.close_reason_text("reported_failed", "vm_failed")
        != texts["reported_failed"]
    )
    assert launch_feedback.reason_code_text(
        "what_is_this"
    ) == launch_feedback.reason_code_text("other")

    # Отчёт failed с reason_code: одна запись в ленте (report и close_order не двоят).
    task_id = await _task(world, "итог с кодом")
    lived = await _launch_redeem(world, task_id)
    oid = lived["order"]["id"]
    url = f"/api/executor-hosts/k8/orders/{oid}/report"
    body = {"outcome": "failed", "note": "упал", "reason_code": "vm_failed"}
    feed0 = len(await _rows(world.db, "SELECT 1 FROM task_updates"))
    first = await world.client.post(url, json=body, headers=world.k8_auth)
    assert first.status_code == 200, first.text
    await world.db.commit()
    feed1 = await _rows(world.db, "SELECT content FROM task_updates")
    assert len(feed1) - feed0 == 1, [r["content"] for r in feed1[feed0:]]
    assert "vm_failed" in feed1[-1]["content"] or "ВМ" in feed1[-1]["content"]
    # Дубль — без записи; другой reason_code — 409 и причина прежняя.
    dup = await world.client.post(url, json=body, headers=world.k8_auth)
    assert dup.status_code == 200 and dup.json()["duplicate"] is True
    other = await world.client.post(
        url, json={**body, "reason_code": "agent_failed"}, headers=world.k8_auth
    )
    assert other.status_code == 409
    row = await _order_row(world, oid)
    assert row["report_reason_code"] == "vm_failed"
    assert len(await _rows(world.db, "SELECT 1 FROM task_updates")) == len(feed1)

    # Старый клиент без reason_code → other.
    old_task = await _task(world, "старый клиент")
    old = await _launch_redeem(world, old_task, "k9")
    legacy = await world.client.post(
        f"/api/executor-hosts/k9/orders/{old['order']['id']}/report",
        json={"outcome": "failed", "note": "упал"},
        headers=world.k9_auth,
    )
    assert legacy.status_code == 200
    assert (await _order_row(world, old["order"]["id"]))[
        "report_reason_code"
    ] == "other"

    # Поздний report после закрытия: принят, уточняет причину, второй записи нет.
    late_task = await _task(world, "поздний report")
    late = await _launch_redeem(world, late_task)
    lid = late["order"]["id"]
    await world.db.execute(
        "UPDATE executor_orders SET delivered_at = COALESCE(delivered_at, datetime('now')) "
        "WHERE id = ?",
        (lid,),
    )
    await world.db.commit()
    await _close(world, lid, "session_gone")
    entries = len(await _rows(world.db, "SELECT 1 FROM task_updates"))
    refined = await world.client.post(
        f"/api/executor-hosts/k8/orders/{lid}/report",
        json={"outcome": "failed", "note": "поздно", "reason_code": "timeout"},
        headers=world.k8_auth,
    )
    assert refined.status_code == 200, refined.text
    await world.db.commit()
    assert len(await _rows(world.db, "SELECT 1 FROM task_updates")) == entries
    plaque = await launch_feedback.plaque(world.db, late_task)
    assert plaque is not None and not plaque["active"]
    assert launch_feedback.reason_code_text("timeout") in plaque["outcome"]

    # Фактическая сдача сильнее причины закрытия.
    sub_task = await _task(world, "сдача сильнее")
    sub = await _launch_redeem(world, sub_task)
    await _eo._submit(world, sub_task)
    await _close(world, sub["order"]["id"], "session_gone")
    plaque = await launch_feedback.plaque(world.db, sub_task)
    assert plaque is not None and "сдан" in plaque["outcome"].lower()


async def test_plaque_fragment_refresh_and_no_secrets(world):
    """AC-4: фрагмент плашки обновляется отдельно; кода и секретов в нём нет."""
    task_id = await _task(world, "фрагмент")
    headers, _csrf = await _cookie(world)
    lived = await _launch_redeem(world, task_id)
    oid = lived["order"]["id"]
    url = f"/partials/launch-plaque/{task_id}"
    first = await world.client.get(url, headers=headers)
    assert first.status_code == 200
    assert "<html" not in first.text and f"Заказ #{oid}" in first.text
    assert (
        "hx-get" in first.text and "every" in first.text
    )  # активный — самообновляется
    phase = await world.client.post(
        f"/api/executor-hosts/k8/orders/{oid}/phase",
        json={"phase": "agent_running"},
        headers=world.k8_auth,
    )
    assert phase.status_code == 200
    second = await world.client.get(url, headers=headers)
    assert second.text != first.text and "агент работает" in second.text.lower()
    # Код исполнителя в reason: отчёт отказан (422), плашка его не несёт.
    code = lived["code"]
    leaked = await world.client.post(
        f"/api/executor-hosts/k8/orders/{oid}/report",
        json={"outcome": "failed", "reason_code": "other", "reason": f"код {code}"},
        headers=world.k8_auth,
    )
    assert leaked.status_code == 422
    # Секрет по виду — тоже; безопасный reason доходит до плашки.
    secret = await world.client.post(
        f"/api/executor-hosts/k8/orders/{oid}/report",
        json={"outcome": "failed", "reason": "token=abcdef123456789"},
        headers=world.k8_auth,
    )
    assert secret.status_code == 422
    ok = await world.client.post(
        f"/api/executor-hosts/k8/orders/{oid}/report",
        json={
            "outcome": "failed",
            "reason_code": "vm_failed",
            "reason": "ВМ не стартовала",
        },
        headers=world.k8_auth,
    )
    assert ok.status_code == 200, ok.text
    final = await world.client.get(url, headers=headers)
    assert (
        "<html" not in final.text and "hx-trigger" not in final.text
    )  # итог — не опрашивает
    for needle in (code, code.removeprefix("AH-"), "abcdef123456789"):
        assert needle not in final.text and needle not in second.text
    assert "ВМ не стартовала" in final.text
