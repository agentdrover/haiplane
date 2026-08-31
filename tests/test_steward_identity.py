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
