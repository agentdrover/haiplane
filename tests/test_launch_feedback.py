"""Обратная связь запуска исполнителя на карточке (#1817).

Сообщение после «Ок», плашка шагов, кнопка неактивна, пока идёт заказ; единая
таблица причин закрытия; одна итоговая запись в ленте на заказ; ни кода, ни
секретов в плашке.
"""

from __future__ import annotations


from hub import config
from hub.csrf import token_for_session_cookie
from hub.services import admin as admin_svc
from hub.services import executor_orders as orders
from hub.services import launch_feedback
from tests import test_executor_orders as _eo
from tests.test_executor_orders import (
    HUB_URL,
    _launch_redeem,
    _order_row,
    _rows,
    _task,
)

LEAK = "abcdef123456789"  # pragma: allowlist secret
SHA = "a1b2c3d4e5f6"  # pragma: allowlist secret

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

    pressed = await world.client.post(
        f"/tasks/{chosen}/web-executor-slot",
        data={"host": "k8", "csrf_token": csrf},
        headers=headers,
        follow_redirects=False,
    )
    assert pressed.status_code == 303, pressed.text
    ok = await world.client.get(f"/tasks/{chosen}", headers=headers)
    assert ok.status_code == 200, ok.text
    placed = await _rows(world.db, "SELECT id FROM executor_orders")
    assert len(placed) == 1
    number = placed[0]["id"]
    assert f"Заказ #{number} создан, ждёт k8" in ok.text
    # Кнопка неактивна и называет шаг.
    assert "Запуск идёт" in ok.text and "disabled" in ok.text
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
    # Тот же заказ другим хостом (слот k9 свободен) и другая задача на том же хосте:
    # через REST, где в ответе только причина (на странице номер виден и в плашке).
    second_task = await _task(world, "вторая задача")
    for task_id, host in ((chosen, "k9"), (second_task, "k8")):
        refused = await world.client.post(
            f"/api/projects/{world.slug}/executor-launch",
            json={"target": f"slot:{host}", "task_id": task_id},
            headers=world.human_auth,
        )
        assert refused.status_code == 409, refused.text
        assert f"заказ #{number}" in refused.json()["detail"]["reason"]
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
    # Код без префикса AH- по виду не секрет: его ловит только сверка по хешу.
    bare = await world.client.post(
        f"/api/executor-hosts/k8/orders/{oid}/report",
        json={"outcome": "failed", "reason": f"вот {code.removeprefix('AH-')} конец"},
        headers=world.k8_auth,
    )
    assert bare.status_code == 422
    # Секрет по виду — тоже; безопасный reason доходит до плашки.
    secret = await world.client.post(
        f"/api/executor-hosts/k8/orders/{oid}/report",
        json={"outcome": "failed", "reason": f"token={LEAK}"},
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
    for needle in (
        code,
        code.removeprefix("AH-"),
        LEAK,
    ):
        assert needle not in final.text and needle not in second.text
    assert "ВМ не стартовала" in final.text


async def test_report_reason_refuses_hub_tokens(world, monkeypatch):
    """Codex P1: токен хаба произвольного вида (длинный hex) в reason не принимается."""
    token = "ab" * 32  # pragma: allowlist secret
    monkeypatch.setattr(
        config, "HUB_TOKENS", {token: config.TokenIdentity("b", "agent")}
    )
    task_id = await _task(world, "токен в reason")
    lived = await _launch_redeem(world, task_id)
    oid = lived["order"]["id"]
    refused = await world.client.post(
        f"/api/executor-hosts/k8/orders/{oid}/report",
        json={"outcome": "failed", "reason": f"вот {token} конец"},
        headers=world.k8_auth,
    )
    assert refused.status_code == 422
    assert (await _order_row(world, oid))["report_outcome"] == ""


async def test_reaper_return_writes_one_feed_entry(world):
    """Codex P2: возврат задачи reaper'ом и закрытие заказа дают одну запись, не две."""
    from hub.services import chat_pair as cp

    task_id = await _task(world, "возврат reaper")
    lived = await _launch_redeem(world, task_id)
    await _eo._claim_and_start(world, task_id, lived["session"])
    before = len(await _rows(world.db, "SELECT 1 FROM task_updates"))
    await world.db.execute(
        "UPDATE chat_pair_sessions SET expires_at = datetime('now', '-1 minute') "
        "WHERE bound_task_id = ?",
        (task_id,),
    )
    await world.db.commit()
    assert await cp.release_expired_implementer_tasks(world.db) == [task_id]
    await world.db.commit()
    row = await _order_row(world, lived["order"]["id"])
    assert row["state"] == "closed" and row["final_entry_at"]
    entries = await _rows(
        world.db,
        "SELECT agent, content FROM task_updates WHERE content LIKE '%закрыт%' "
        "OR content LIKE '%возвращена%'",
    )
    assert len(await _rows(world.db, "SELECT 1 FROM task_updates")) - before == 1, (
        entries
    )


async def test_plaque_fragment_refreshes_page_when_launch_ends(world):
    """Codex P3: когда запуск кончился, HTMX-опрос просит перезагрузить страницу (кнопка вне фрагмента)."""
    task_id = await _task(world, "конец запуска")
    headers, _csrf = await _cookie(world)
    lived = await _launch_redeem(world, task_id)
    url = f"/partials/launch-plaque/{task_id}"
    live = await world.client.get(url, headers={**headers, "HX-Request": "true"})
    assert "hx-refresh" not in {k.lower() for k in live.headers}
    failed = await world.client.post(
        f"/api/executor-hosts/k8/orders/{lived['order']['id']}/report",
        json={"outcome": "failed", "reason_code": "vm_failed"},
        headers=world.k8_auth,
    )
    assert failed.status_code == 200
    done = await world.client.get(url, headers={**headers, "HX-Request": "true"})
    assert done.headers.get("HX-Refresh") == "true"
    plain = await world.client.get(
        url, headers=headers
    )  # обычная загрузка — без обновления
    assert "hx-refresh" not in {k.lower() for k in plain.headers}


async def test_old_reports_and_entries_are_backfilled(world):
    """Codex P2: миграции подгоняют старые report и итоговые записи под новые правила."""
    from hub.db import _MIGRATIONS

    sql = dict(_MIGRATIONS)
    task_id = await _task(world, "старый report")
    lived = await _launch_redeem(world, task_id)
    oid = lived["order"]["id"]
    # Строка в виде «до выката»: report старого клиента, кода и метки нет.
    await world.db.execute(
        "UPDATE executor_orders SET report_outcome = 'failed', "
        "reported_at = datetime('now'), report_reason_code = '', final_entry_at = NULL "
        "WHERE id = ?",
        (oid,),
    )
    await world.db.execute(sql["backfill_executor_orders_report_reason_code"])
    await world.db.execute(sql["backfill_executor_orders_final_entry_at"])
    await world.db.commit()
    row = await _order_row(world, oid)
    assert row["report_reason_code"] == "other" and row["final_entry_at"]
    # Идентичный повтор старого клиента — дубль, не 409.
    again = await world.client.post(
        f"/api/executor-hosts/k8/orders/{oid}/report",
        json={"outcome": "failed"},
        headers=world.k8_auth,
    )
    assert again.status_code == 200 and again.json()["duplicate"] is True


async def test_final_entry_is_written_once_in_every_close_path(world):
    """Codex круг 2: сдача сама фиксирует итог; submitted-report + reaper; поздний rescued."""
    from hub import repository as repo
    from hub.services import chat_pair as cp

    async def feed_len() -> int:
        await world.db.commit()
        return len(await _rows(world.db, "SELECT 1 FROM task_updates"))

    # (1) Сдача приняла заказ без report хоста: заказ запись не добавляет (её уже
    # дала сдача: «Захват снят…»), метка стоит, поздний report её не двоит.
    t1 = await _task(world, "сдача без report")
    o1 = (await _launch_redeem(world, t1))["order"]["id"]
    await _eo._submit(world, t1)
    before = await feed_len()
    await orders.sweep_executor_orders(world.db)
    assert await feed_len() - before == 1
    assert (await _order_row(world, o1))["final_entry_at"]
    # Поздний submitted-report запись не двоит.
    late = await world.client.post(
        f"/api/executor-hosts/k8/orders/{o1}/report",
        json={"outcome": "submitted"},
        headers=world.k8_auth,
    )
    assert late.status_code == 200 and await feed_len() - before == 1

    # (2) submitted-report, затем сессия истекла и reaper вернул задачу: запись одна.
    t2 = await _task(world, "report и reaper")
    l2 = await _launch_redeem(world, t2)
    await _eo._claim_and_start(world, t2, l2["session"])
    rep = await world.client.post(
        f"/api/executor-hosts/k8/orders/{l2['order']['id']}/report",
        json={"outcome": "submitted"},
        headers=world.k8_auth,
    )
    assert rep.status_code == 200
    before = await feed_len()
    await world.db.execute(
        "UPDATE chat_pair_sessions SET expires_at = datetime('now', '-1 minute') "
        "WHERE bound_task_id = ?",
        (t2,),
    )
    await world.db.commit()
    assert await cp.release_expired_implementer_tasks(world.db) == [t2]
    assert await feed_len() - before == 0

    # (3) Поздний rescued после закрытия reaper'ом: ветка и sha видны в плашке.
    t3 = await _task(world, "поздний rescued")
    l3 = await _launch_redeem(world, t3)
    await _close(world, l3["order"]["id"], "session_gone")
    await repo.update_task(world.db, t3, branch=f"task-{t3}/work")
    await world.db.commit()
    resc = await world.client.post(
        f"/api/executor-hosts/k8/orders/{l3['order']['id']}/report",
        json={
            "outcome": "rescued",
            "branch": f"task-{t3}/rescue-7",
            "sha": SHA,
            "reason_code": "agent_failed",
        },
        headers=world.k8_auth,
    )
    assert resc.status_code == 200, resc.text
    plaque = await launch_feedback.plaque(world.db, t3)
    assert plaque is not None
    assert f"task-{t3}/rescue-7" in plaque["outcome"] and SHA in plaque["outcome"]


async def test_repeat_launch_after_claim_names_the_active_order(world):
    """Codex P3: когда задача уже claimed/running, отказ всё равно называет номер заказа."""
    from hub import repository as repo

    task_id = await _task(world, "уже в работе")
    lived = await _launch_redeem(world, task_id)
    await _eo._claim_and_start(world, task_id, lived["session"])
    assert (await repo.get_task(world.db, task_id))["status"] == "running"
    again = await world.client.post(
        f"/api/projects/{world.slug}/executor-launch",
        json={"target": "slot:k9", "task_id": task_id},
        headers=world.human_auth,
    )
    assert again.status_code == 409
    assert f"заказ #{lived['order']['id']}" in again.json()["detail"]["reason"]
