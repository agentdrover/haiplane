"""Исход прогона k8 по классам (#1941): коды причины критика и «после неудачного прогона».

AC-1: все коды REASON_CODES принимаются отчётом хоста и дают класс по таблице;
неизвестный входной код — 422, неизвестный сохранённый — «не классифицировано».
AC-2: after_failed_run считается из ОДНОГО снимка и гаснет от каждого события.
AC-3: REST, MCP compact/full, CLI и карточка показывают одну проекцию.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch


from hub import cli, config, mcp_server
from hub import repository as repo
from hub.services import admin as admin_svc
from hub.services import launch_feedback
from tests import test_executor_orders as _eo
from tests.test_executor_orders import (
    HUB_URL,
    _launch,
    _launch_redeem,
    _order_row,
    _rows,
    _submit,
    _task,
)

ROOT = Path(__file__).resolve().parent.parent
world = _eo.world
keys = _eo.keys
_clean_limiter = _eo._clean_limiter
_no_cloud = _eo._no_cloud

#: Таблица из постановки, записанная здесь независимо от кода: код -> (класс, имя).
CLASS_TABLE: dict[str, tuple[int, str]] = {
    "critic_unavailable": (1, "infra"),
    "critic_delivery_failed": (1, "infra"),
    "channel_check_failed": (1, "infra"),
    "setup_failed": (1, "infra"),
    "redeem_failed": (1, "infra"),
    "vm_failed": (1, "infra"),
    "hub_unavailable": (1, "infra"),
    "stale_token": (1, "infra"),
    "busy": (1, "infra"),
    "over_budget": (1, "infra"),
    "timeout": (1, "infra"),
    "no_new_head": (2, "no_new_head"),
    "critic_cycles_exhausted": (3, "critic_exhausted"),
    "critic_time_exhausted": (3, "critic_exhausted"),
    "integrity_violation": (4, "integrity"),
    "critic_failed": (0, "unclassified"),
    "agent_failed": (0, "unclassified"),
    "other": (0, "unclassified"),
}
PROJECTION_KEYS = {
    "order_id",
    "state",
    "outcome",
    "reason_code",
    "class",
    "class_name",
    "after_failed_run",
}


async def _get(world, task_id: int) -> dict:
    resp = await world.client.get(f"/api/tasks/{task_id}", headers=world.human_auth)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _report(world, oid: int, outcome: str, code: str, host: str = "k8"):
    return await world.client.post(
        f"/api/executor-hosts/{host}/orders/{oid}/report",
        json={"outcome": outcome, "reason_code": code, "reason": "проба"},
        headers=getattr(world, f"{host}_auth"),
    )


async def _failed(world, title: str, code: str = "critic_failed"):
    """Задача open, последний host-заказ закрыт rescued с кодом ``code``."""
    task_id = await _task(world, title)
    lived = await _launch_redeem(world, task_id)
    oid = lived["order"]["id"]
    resp = await _report(world, oid, "rescued", code)
    assert resp.status_code == 200, (code, resp.text)
    await world.db.commit()
    return task_id, oid


# ---------------------------------------------------------------------------
# AC-1
# ---------------------------------------------------------------------------


async def test_triage_codes_are_accepted_and_classified(world):
    """AC-1: каждый код REASON_CODES принят, класс по таблице; неизвестный вход — 422."""
    for code, (klass, name) in CLASS_TABLE.items():
        task_id, oid = await _failed(world, f"код {code}", code)
        order = (await _get(world, task_id))["last_executor_order"]
        assert order["order_id"] == oid, code
        assert order["outcome"] == "rescued" and order["reason_code"] == code
        assert (order["class"], order["class_name"]) == (klass, name), code
        assert order["state"] == "closed"

    assert set(CLASS_TABLE) == set(launch_feedback.REASON_CODES)

    # Неизвестный входной код — 422, заказ не закрыт, класса нет.
    task_id = await _task(world, "неизвестный код")
    lived = await _launch_redeem(world, task_id)
    oid = lived["order"]["id"]
    refused = await _report(world, oid, "rescued", "no_such_code")
    assert refused.status_code == 422, refused.text
    assert "critic_cycles_exhausted" in refused.text
    assert (await _order_row(world, oid))["state"] != "closed"
    assert (await _report(world, oid, "rescued", "critic_failed")).status_code == 200

    # Неизвестный СОХРАНЁННЫЙ код (старая строка) — «не классифицировано», не ошибка.
    legacy, legacy_oid = await _failed(world, "старая строка")
    await world.db.execute(
        "UPDATE executor_orders SET report_reason_code = 'legacy_x' WHERE id = ?",
        (legacy_oid,),
    )
    await world.db.commit()
    stored = (await _get(world, legacy))["last_executor_order"]
    assert (stored["class"], stored["class_name"]) == (0, "unclassified")
    assert stored["reason_code"] == "legacy_x"


# ---------------------------------------------------------------------------
# AC-2
# ---------------------------------------------------------------------------


async def _cookie(world):
    session = await admin_svc.create_browser_session(world.db, world.human_id)
    await world.db.commit()
    return {"Cookie": f"{config.HUB_COOKIE_NAME}={session}", "Origin": HUB_URL}


async def _surfaces(world, task_id: int, capsys) -> dict:
    """Пять представлений одной карточки: REST, MCP compact, MCP full, CLI, страница."""
    rest = await _get(world, task_id)
    out: dict = {"rest": rest.get("last_executor_order")}
    from tests.test_mcp_server import _mcp_text

    texts = {}
    for full in (False, True):
        with (
            patch.object(mcp_server, "_api_post", AsyncMock(return_value={})),
            patch.object(mcp_server, "_api_get", AsyncMock(return_value=rest)),
        ):
            res = await mcp_server.hub_task_status(task_id, full=full)
        key = "mcp_full" if full else "mcp_compact"
        out[key] = res.structuredContent["task"].get("last_executor_order")
        texts[key] = _mcp_text(res)
    capsys.readouterr()
    with patch.object(cli, "_api", return_value=rest):
        assert cli.cmd_status(argparse.Namespace(task_id=task_id)) == 0
    captured = capsys.readouterr()
    out["cli"] = json.loads(captured.out).get("last_executor_order")
    page = await world.client.get(f"/tasks/{task_id}", headers=await _cookie(world))
    assert page.status_code == 200, page.text
    out["marks"] = {
        "mcp_compact": "After failed run" in texts["mcp_compact"],
        "mcp_full": "After failed run" in texts["mcp_full"],
        "cli": "После неудачного прогона" in captured.err,
        "card": "После неудачного прогона" in page.text,
    }
    return out


def _afr(view: dict | None):
    return None if view is None else view.get("after_failed_run")


async def _expect(world, task_id: int, capsys, expected: bool, why: str) -> dict:
    seen = await _surfaces(world, task_id, capsys)
    for name in ("rest", "mcp_compact", "mcp_full", "cli"):
        assert _afr(seen[name]) is expected, (why, name, seen[name])
    for name, shown in seen["marks"].items():
        assert shown is expected, (why, name)
    return seen


async def _db_fingerprint(world) -> list:
    await world.db.commit()  # свежий снимок: чужие коммиты видны
    out = []
    for table in (
        "executor_orders",
        "executor_runs",
        "tasks",
        "task_updates",
        "chat_pair_sessions",
        "admin_audit_log",
    ):
        out.append(await _rows(world.db, f"SELECT * FROM {table} ORDER BY 1"))
    return out


async def test_after_failed_run_is_derived_from_one_snapshot(
    world, capsys, monkeypatch
):
    """AC-2: after_failed_run=true только после неудачного прогона, без чего-либо живого."""
    task_id, _oid = await _failed(world, "основа")

    # Чтение ничего не пишет: содержимое таблиц до и после семи чтений равно.
    # Прогрев: первое чтение карточки досчитывает readiness (старая запись, не снимок).
    await _surfaces(world, task_id, capsys)
    before = await _db_fingerprint(world)
    from hub.services import executor_triage

    changes = world.db.total_changes
    assert (await executor_triage.last_executor_order_view(world.db, task_id))[
        "after_failed_run"
    ]
    assert world.db.total_changes == changes
    seen = await _expect(world, task_id, capsys, True, "основа")
    assert seen["rest"]["class_name"] == "unclassified"
    after = await _db_fingerprint(world)
    for was, now in zip(before, after, strict=True):
        assert was == now, [
            (k, a.get(k), b.get(k)) for a, b in zip(was, now) for k in a if a[k] != b[k]
        ]

    # 1. Новый слот-заказ на той же задаче (другой хост).
    t1, _ = await _failed(world, "новый заказ")
    assert (await _launch(world, t1, "k9")).status_code == 200
    seen = await _expect(world, t1, capsys, False, "новый заказ")
    assert seen["rest"]["state"] != "closed"

    # 2. Облачная бронь (ещё без агента).
    t2, _ = await _failed(world, "облачная бронь")
    await world.db.execute(
        "INSERT INTO executor_runs (task_id, submission_generation, channel, outcome) "
        "VALUES (?, 1, 'cloud', 'running')",
        (t2,),
    )
    await world.db.commit()
    await _expect(world, t2, capsys, False, "облачная бронь")

    # 3. Захват: держатель при статусе open (отдельно от проверки статуса).
    t3, _ = await _failed(world, "держатель")
    await repo.update_task(world.db, t3, claimed_by="someone")
    await world.db.commit()
    await _expect(world, t3, capsys, False, "держатель")

    # 4. Живая сессия implementer, привязанная к задаче.
    t4, _ = await _failed(world, "живая сессия")
    await world.db.execute(
        "UPDATE chat_pair_sessions SET revoked_at = NULL, kind = 'implementer', "
        "expires_at = datetime('now', '+1 hour') WHERE bound_task_id = ?",
        (t4,),
    )
    await world.db.commit()
    await _expect(world, t4, capsys, False, "живая сессия")

    # 5. Сдача после заказа (статус open оставлен: гасит именно поколение).
    t5, o5 = await _failed(world, "сдача")
    gen5 = (await _order_row(world, o5))["generation"]
    await repo.update_task(world.db, t5, submission_generation=gen5)
    await world.db.commit()
    await _expect(world, t5, capsys, False, "сдача")

    # 6. Сдача принята, report failed пришёл позже: каноническая сдача сильнее.
    t6 = await _task(world, "сдача и поздний failed")
    lived = await _launch_redeem(world, t6)
    await _submit(world, t6, generation=1)
    late = await _report(world, lived["order"]["id"], "failed", "timeout")
    assert late.status_code == 200, late.text
    await world.db.commit()
    await repo.update_task(world.db, t6, status="open")
    await world.db.commit()
    await _expect(world, t6, capsys, False, "сдача и поздний failed")

    # 7. Архив.
    t7, _ = await _failed(world, "архив")
    await world.db.execute("UPDATE tasks SET archived = 1 WHERE id = ?", (t7,))
    await world.db.commit()
    await _expect(world, t7, capsys, False, "архив")

    # 8. Статус не open: то же событие в иной форме.
    t8, _ = await _failed(world, "статус")
    await repo.update_task(world.db, t8, status="running")
    await world.db.commit()
    await _expect(world, t8, capsys, False, "статус")

    # 9. Сбой чтения: поле с причиной, after_failed_run не выставлен (ни true, ни false).
    from hub.services import executor_triage

    async def _boom(*args, **kwargs):
        raise RuntimeError("снимок не прочитан")

    t9, _ = await _failed(world, "сбой чтения")
    monkeypatch.setattr(executor_triage, "_read_snapshot", _boom)
    broken = await _surfaces(world, t9, capsys)
    for name in ("rest", "mcp_compact", "mcp_full", "cli"):
        assert "after_failed_run" not in broken[name], broken[name]
        assert broken[name]["unavailable"], broken[name]
    assert not any(broken["marks"].values())
    monkeypatch.undo()


# ---------------------------------------------------------------------------
# AC-3
# ---------------------------------------------------------------------------


async def test_surfaces_show_the_same_triage_state(world, capsys):
    """AC-3: одна проекция на всех поверхностях, без кодов и шифротекста; бюджеты и слои."""
    task_id, oid = await _failed(world, "поверхности")
    seen = await _expect(world, task_id, capsys, True, "поверхности")
    rest = seen["rest"]
    assert set(rest) == PROJECTION_KEYS
    for name in ("mcp_compact", "mcp_full", "cli"):
        assert seen[name] == rest, name
    dumped = json.dumps(await _get(world, task_id))
    row = await _order_row(world, oid)
    assert row["code_hash"] and row["code_hash"] not in dumped
    assert "code_ciphertext" not in dumped and "order_text" not in dumped
    assert "code_hash" not in json.dumps(rest)

    # Задача без заказа хоста: null, а не false.
    plain = await _task(world, "без заказов")
    assert (await _get(world, plain))["last_executor_order"] is None

    # Контракт: таблица код -> класс записана в contracts.md.
    contracts = (ROOT / "docs/agent-context/contracts.md").read_text(encoding="utf-8")
    for code, (klass, name) in CLASS_TABLE.items():
        lines = [ln for ln in contracts.splitlines() if re.search(rf"`{code}`", ln)]
        assert any(name in ln and f"{klass}" in ln for ln in lines), code

    # Слои и бюджет каталога MCP — настоящие скрипты CI, код возврата.
    for script in ("layer_rules.py", "mcp_catalog_budget.py"):
        done = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=240,
        )
        assert done.returncode == 0, (script, done.stdout[-800:], done.stderr[-400:])
    rules = json.loads(
        (ROOT / "docs/agent-context/layer-rules.json").read_text(encoding="utf-8")
    )
    assert "hub.services" in json.dumps(rules["layers"]["services"])
