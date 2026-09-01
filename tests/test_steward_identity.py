"""Как прогон стюарда добирается до хаба (#1120).

Cursor отбрасывает mcpServers на пути в облачный ран, поэтому токен в
заголовках до прогона не доезжает — это установленный факт, ради которого
#1084 сделала одноразовый код для ревьюера. Здесь тот же приём для стюарда,
и проверяется не «выдаётся ли код», а три вещи, на которых держится граница:
код привязан к задаче И генерации, сессия ходит ровно по двум операциям
#1021, и без канала прогон не запускается вовсе.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import aiosqlite
import pytest
from fastapi.routing import APIRoute

from hub import config
from hub import repository as repo
from hub.auth import chat_pair_route_allowed
from hub.services import chat_pair
from hub.services import steward_shadow as sh
from hub.services import admin as admin_svc

STEWARD_OPS = (
    ("GET", "/api/tasks/{task_id}/steward-evidence"),
    ("POST", "/api/tasks/{task_id}/steward-judgement"),
)

_FILL = re.compile(r"\{[^}/]+\}")


async def _steward_principal(db: aiosqlite.Connection, monkeypatch) -> int:
    """Принципал steward с ключом, который хаб узнаёт по хешу."""
    principal = await admin_svc.create_principal(
        db, kind="agent", username="steward-bot", role_slug="steward"
    )
    key = await admin_svc.create_api_key(db, principal["id"], name="steward-run")
    monkeypatch.setattr(config, "STEWARD_HUB_TOKEN", key["plaintext_key"])
    # Не косметика: открытый режим (пустой HUB_TOKENS или выключенный auth)
    # никогда не читает bearer, поэтому сессия, привязанная к принципалу, там
    # неисполнима — и канал честно отказывает. Тесты про КАНАЛ, значит режим
    # должен быть боевым.
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "HUB_TOKENS", {"env-token": SimpleNamespace()})
    return int(principal["id"])


async def _task(db: aiosqlite.Connection, *, generation: int = 1) -> int:
    task_id = await repo.create_task(
        db,
        title="сдача на суд",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="pda_claude",
        rationale="",
        status="review",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(db, task_id, submission_generation=generation)
    await db.commit()
    return task_id


async def test_the_run_gets_a_task_bound_code(db: aiosqlite.Connection, monkeypatch):
    """#1120 AC-1: прогон получает одноразовый код и инструкцию, что с ним делать.

    Без этого агент стартует и молчит: пакет за дверью, а ключа от двери у
    него нет. Блок доставки называет обе операции и ничего сверх них.
    """
    principal_id = await _steward_principal(db, monkeypatch)
    task_id = await _task(db)

    block = await sh.identity_delivery(db, task_id, 1, "https://hub.example")

    assert block, "канал доставки обязан появиться"
    assert "/api/auth/chat-pair/redeem" in block
    assert f"/api/tasks/{task_id}/steward-evidence" in block
    assert f"/api/tasks/{task_id}/steward-judgement" in block

    code = re.search(r'"code":"([^"]+)"', block)
    assert code, "код обязан быть в блоке"
    session = await chat_pair.redeem_code(db, code.group(1))
    assert session is not None
    assert session["kind"] == "steward"
    assert session["bound_task_id"] == task_id
    assert session["principal_id"] == principal_id


async def test_a_code_does_not_cross_generations(db: aiosqlite.Connection, monkeypatch):
    """#1120 AC-2: код помнит генерацию, а не только задачу.

    Урок #1084 дословно: без bound_generation issue_code пишет NULL, redeem
    пропускает сравнение, и весь пин ниже становится мёртвым кодом. Тогда
    код, выданный на сдачу #1, судил бы сдачу #2 — другой код, тот же ключ.
    """
    await _steward_principal(db, monkeypatch)
    task_id = await _task(db)

    block = await sh.identity_delivery(db, task_id, 1, "https://hub.example")
    code = re.search(r'"code":"([^"]+)"', block).group(1)
    session = await chat_pair.redeem_code(db, code)

    assert session is not None
    identity = await chat_pair.resolve_session(db, session["token"])
    assert identity is not None
    assert identity.chat_pair_generation == 1, "генерация обязана быть записана"
    assert identity.chat_pair_task_id == task_id


async def test_the_session_walks_the_steward_allowlist(
    db: aiosqlite.Connection, monkeypatch
):
    """#1120 AC-3: сессии открыты ровно две операции, и обе про свою задачу.

    Список маршрутов для сессии — ТОТ ЖЕ, что для токена (#1021): два
    описания одной границы разъезжаются, и разъезжается всегда то, которое
    шире.
    """
    await _steward_principal(db, monkeypatch)
    task_id = await _task(db)
    block = await sh.identity_delivery(db, task_id, 1, "https://hub.example")
    code = re.search(r'"code":"([^"]+)"', block).group(1)
    session = await chat_pair.redeem_code(db, code)
    identity = await chat_pair.resolve_session(db, session["token"])

    for method, template in STEWARD_OPS:
        path = template.replace("{task_id}", str(task_id))
        assert chat_pair_route_allowed(method, path, identity), f"{method} {path}"

    # Чужая задача закрыта теми же двумя маршрутами: код привязан к задаче.
    for method, template in STEWARD_OPS:
        alien = template.replace("{task_id}", str(task_id + 777))
        assert not chat_pair_route_allowed(method, alien, identity), alien

    # И перебор: всё остальное — отказ, а не «наверное, можно».
    from hub.app import app

    walked = 0
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for verb in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            if (verb, route.path) in STEWARD_OPS:
                continue
            walked += 1
            assert not chat_pair_route_allowed(
                verb, _FILL.sub(str(task_id), route.path), identity
            ), f"{verb} {route.path} не должен быть открыт стюарду"
    assert walked > 20, "перебор обязан быть перебором, а не парой примеров"


async def test_without_a_principal_there_is_no_code(
    db: aiosqlite.Connection, monkeypatch
):
    """Нет принципала за токеном — нет кода, и прогон не стартует.

    Отказ здесь дешевле оплаченного молчания: агент без ключа доживёт до
    дедлайна и не вернёт ничего. Именно это #1105 и проверяет своим
    предохранителем — тут проверяется, что канал честно говорит «не могу».
    """
    monkeypatch.setattr(config, "STEWARD_HUB_TOKEN", "")
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "HUB_TOKENS", {"env-token": SimpleNamespace()})
    task_id = await _task(db)

    assert await sh.identity_delivery(db, task_id, 1, "https://hub.example") is None

    # И с токеном, за которым не стоит принципал, — тоже None.
    monkeypatch.setattr(config, "STEWARD_HUB_TOKEN", "ochk_нет-такого-ключа")
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    assert await sh.identity_delivery(db, task_id, 1, "https://hub.example") is None


def test_the_delivery_block_is_empty_without_a_code():
    """Инструкция, называющая несуществующий ключ, учит его выдумывать."""
    assert sh.delivery_block(1, "", "https://hub.example") == ""
    assert sh.delivery_block(1, "ABC-123", "") == ""


async def test_a_steward_session_is_not_a_human(db: aiosqlite.Connection, monkeypatch):
    """Сессия стюарда не человек и не агент — ветки «не агент → человек» её не пустят.

    Та же проверка, что #1021 сделала для токена: роль здесь другая дорога к
    тому же принципалу, а не новый актор со своими правами.
    """
    await _steward_principal(db, monkeypatch)
    task_id = await _task(db)
    block = await sh.identity_delivery(db, task_id, 1, "https://hub.example")
    code = re.search(r'"code":"([^"]+)"', block).group(1)
    session = await chat_pair.redeem_code(db, code)
    identity = await chat_pair.resolve_session(db, session["token"])

    assert identity.is_steward is True
    assert identity.is_human is False
    assert identity.is_agent is False
    assert identity.permissions == config.STEWARD_PERMS

    request = SimpleNamespace(state=SimpleNamespace(identity=identity))
    from fastapi import HTTPException

    from hub.auth import require_human_or_admin

    with pytest.raises(HTTPException) as gate:
        require_human_or_admin(request)
    assert gate.value.status_code == 403


# ---------------------------------------------------------------------------
# Находки ревью сдачи #1 (grok, отчёт 180)
# ---------------------------------------------------------------------------


async def _live_session(db, monkeypatch, task_id: int, generation: int) -> str:
    """Настоящий путь: минт → обмен → токен. Ни одного вычеканенного вручную."""
    block = await sh.identity_delivery(db, task_id, generation, "https://hub.example")
    assert block, "канал обязан выдать код"
    code = re.search(r'"code":"([^"]+)"', block).group(1)
    session = await chat_pair.redeem_code(db, code)
    assert session is not None
    return session["token"]


async def test_the_pin_is_read_not_just_written(
    db: aiosqlite.Connection, client, monkeypatch
):
    """Сессия генерации 1 не судит генерацию 2 (находка high).

    Привязка была построена и нигде не читалась: код помнил генерацию, а
    двери её не спрашивали. Проверяется ИСПОЛНЕНИЕМ — живой минт, живой
    обмен, живой HTTP, — потому что тот же класс дефекта на #1084 пережил
    два круга ревью именно как «привязка есть, чтения нет».
    """
    await _steward_principal(db, monkeypatch)
    task_id = await _task(db, generation=1)
    token = await _live_session(db, monkeypatch, task_id, 1)
    auth = {"Authorization": f"Bearer {token}"}

    # Автор пересдал, пока прогон думал.
    await repo.update_task(db, task_id, submission_generation=2)
    await db.commit()

    alien = await client.get(
        f"/api/tasks/{task_id}/steward-evidence?generation=2", headers=auth
    )
    assert alien.status_code == 403, alien.text
    assert alien.json()["detail"]["reason"] == "steward_generation_mismatch"

    # И суждение о чужой генерации не принимается тем же кодом.
    filed = await client.post(
        f"/api/tasks/{task_id}/steward-judgement",
        headers=auth,
        json={
            "generation": 2,
            "kind": "verdict",
            "verdict": "approve",
            "confidence": "high",
        },
    )
    assert filed.status_code == 403, filed.text
    assert filed.json()["detail"]["reason"] == "steward_generation_mismatch"


async def test_a_new_mint_does_not_disarm_a_live_run(
    db: aiosqlite.Connection, monkeypatch
):
    """Минт для генерации 2 не гасит неиспользованный код генерации 1.

    Пересдача во время прогона обезоруживала оплаченного агента: его код
    сгорал в чужом минте, и он доходил до двери с мёртвым ключом — умирал по
    дедлайну, не сказав ничего.
    """
    await _steward_principal(db, monkeypatch)
    task_id = await _task(db, generation=1)

    first = await sh.identity_delivery(db, task_id, 1, "https://hub.example")
    code_one = re.search(r'"code":"([^"]+)"', first).group(1)

    # Пересдача: хаб заказывает прогон на новую генерацию и минтит свой код.
    second = await sh.identity_delivery(db, task_id, 2, "https://hub.example")
    code_two = re.search(r'"code":"([^"]+)"', second).group(1)
    assert code_two != code_one

    still_alive = await chat_pair.redeem_code(db, code_one)
    assert still_alive is not None, "код живого прогона обязан пережить чужой минт"
    identity = await chat_pair.resolve_session(db, still_alive["token"])
    assert identity.chat_pair_generation == 1


async def test_two_codes_for_one_generation_still_impossible(
    db: aiosqlite.Connection, monkeypatch
):
    """Сужение по генерации не отменяет саму цель гашения.

    Два живых кода на ОДНУ сдачу — это две двери в один пакет; ровно от
    этого гашение и защищает, и оно осталось на месте.
    """
    await _steward_principal(db, monkeypatch)
    task_id = await _task(db, generation=1)

    first = await sh.identity_delivery(db, task_id, 1, "https://hub.example")
    code_one = re.search(r'"code":"([^"]+)"', first).group(1)
    again = await sh.identity_delivery(db, task_id, 1, "https://hub.example")
    code_two = re.search(r'"code":"([^"]+)"', again).group(1)

    assert code_two != code_one
    assert await chat_pair.redeem_code(db, code_one) is None, "старый код сгорел"
    assert await chat_pair.redeem_code(db, code_two) is not None


async def test_the_pinned_session_reads_its_own_generation(
    db: aiosqlite.Connection, client, monkeypatch
):
    """Своя генерация читается, и запроса даже не требуется.

    Пин — не только запрет: он же и ответ на вопрос «какую сдачу судить».
    Иначе прогон обязан был бы угадывать номер, а угадывание тут стоит
    суждения о чужом коде.
    """
    from hub.services.steward_dispatch import order_run

    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    await _steward_principal(db, monkeypatch)
    task_id = await _task(db, generation=1)
    await order_run(db, task_id, 1)
    token = await _live_session(db, monkeypatch, task_id, 1)

    packet = await client.get(
        f"/api/tasks/{task_id}/steward-evidence",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert packet.status_code == 200, packet.text
    assert packet.json()["generation"] == 1


# ---------------------------------------------------------------------------
# Находки ревью сдачи #2 (grok, отчёт 184) — третий круг вокруг пина
# ---------------------------------------------------------------------------
#
# Механизм ошибался трижды по-разному: на #1084 пин не передавался, в первой
# сдаче писался и не читался, во второй читался на одном входе и не читался
# на другом. Значит проверять надо не «тот случай, который назвали», а ВСЕ
# входы разом — списком, который можно прочитать и пересчитать.


async def _pinned_call(client, method: str, task_id: int, token: str, **kw):
    """Один вход стюарда, вызванный живым HTTP с живым токеном."""
    auth = {"Authorization": f"Bearer {token}"}
    if method == "GET":
        query = kw.get("query")
        suffix = f"?generation={query}" if query is not None else ""
        return await client.get(
            f"/api/tasks/{task_id}/steward-evidence{suffix}", headers=auth
        )
    return await client.post(
        f"/api/tasks/{task_id}/steward-judgement",
        headers=auth,
        json={
            "generation": kw.get("body_generation", 1),
            "kind": "verdict",
            "verdict": "approve",
            "confidence": "high",
        },
    )


async def test_every_pinned_entrance_is_listed(db: aiosqlite.Connection):
    """Список входов, где пин обязан сверяться, читается и совпадает с allowlist.

    Границу, которую нельзя перечислить, нельзя и проверить: каждый новый
    вход начинал с нуля именно потому, что «проверить пин» жило в каждой
    двери отдельно.
    """
    from hub.services.steward_evidence import STEWARD_PINNED_ENTRANCES

    listed = {tuple(entry.split(" ", 1)) for entry in STEWARD_PINNED_ENTRANCES}
    assert listed == set(STEWARD_OPS), (
        "перечень пиновых входов обязан совпадать с allowlist #1021: "
        "вход без пина — это вход, который начнёт с нуля"
    )


async def test_a_stale_pin_is_refused_on_every_entrance(
    db: aiosqlite.Connection, client, monkeypatch
):
    """После пересдачи ВСЕ входы отказывают — включая GET без query.

    Это и была третья дыра: GET без параметра тихо отдавал пакет своей
    генерации, и прогон дожёвывал код, который перестал быть предметом
    ревью. Тихое чтение старого пакета неотличимо от чтения живого.
    """
    from hub.services.steward_dispatch import order_run

    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    await _steward_principal(db, monkeypatch)
    task_id = await _task(db, generation=1)
    await order_run(db, task_id, 1)
    token = await _live_session(db, monkeypatch, task_id, 1)

    # До пересдачи все входы работают.
    assert (await _pinned_call(client, "GET", task_id, token)).status_code == 200

    # Автор пересдал, пока прогон думал.
    await repo.update_task(db, task_id, submission_generation=2)
    await db.commit()

    refusals = {
        "GET без query": await _pinned_call(client, "GET", task_id, token),
        "GET со своей генерацией": await _pinned_call(
            client, "GET", task_id, token, query=1
        ),
        "POST со своей генерацией": await _pinned_call(
            client, "POST", task_id, token, body_generation=1
        ),
    }
    for name, resp in refusals.items():
        assert resp.status_code == 403, f"{name}: {resp.status_code} {resp.text}"
        assert resp.json()["detail"]["reason"] == "steward_pin_stale", name


async def test_asking_for_another_generation_is_refused_on_every_entrance(
    db: aiosqlite.Connection, client, monkeypatch
):
    """Чужую генерацию не пускает ни один вход — даже когда пин ещё актуален."""
    from hub.services.steward_dispatch import order_run

    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    await _steward_principal(db, monkeypatch)
    task_id = await _task(db, generation=1)
    await order_run(db, task_id, 1)
    token = await _live_session(db, monkeypatch, task_id, 1)

    asked_get = await _pinned_call(client, "GET", task_id, token, query=2)
    asked_post = await _pinned_call(client, "POST", task_id, token, body_generation=2)

    for resp in (asked_get, asked_post):
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["reason"] == "steward_generation_mismatch"


async def test_a_resubmission_closes_the_run_it_outdated(
    db: aiosqlite.Connection, monkeypatch
):
    """Пересдача закрывает заказ прошлой генерации, а не оставляет его висеть.

    Дверь отказывает — но слот, оставшийся открытым, держал бы суточный
    потолок и выдачу пакета за прогон, который судил уже несуществующее.
    """
    from hub.services.steward_dispatch import (
        RUN_SUPERSEDED,
        close_finished_runs,
        order_run,
    )
    from hub.db import fetchall

    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    await _steward_principal(db, monkeypatch)
    task_id = await _task(db, generation=1)
    assert await order_run(db, task_id, 1) is not None, "заказ обязан разместиться"

    await repo.update_task(db, task_id, submission_generation=2)
    await db.commit()
    closed = await close_finished_runs(db)

    assert closed == 1
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE task_id=?", (task_id,))
    row = dict(rows[0])
    assert row["status"] == RUN_SUPERSEDED
    assert "пересдана" in row["closed_reason"]
