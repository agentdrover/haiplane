"""Разбор в тени, второй вход (#1947): задача вернулась с ревью на доработку.

AC-1: после return-to-work ровно одна рекомендация rework_on_host на задачу, сданную
с k8; для остальных случаев - никакой, и ни заказа, ни кода слоту.
AC-2: новый host-заказ после рекомендации даёт followup=new_order и совпадение.
"""

from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest

from hub import db as hub_db
from hub import repository as repo
from hub.services import executor_orders as orders
from hub.services import executor_triage as triage
from tests import test_executor_orders as _eo
from tests.test_executor_orders import _launch_redeem, _rows, _task

world = _eo.world
keys = _eo.keys
_clean_limiter = _eo._clean_limiter
_no_cloud = _eo._no_cloud

REWORK = "rework_on_host"
FINDINGS = [
    {"id": n, "severity": "medium", "message": f"находка {n}"} for n in (1, 2, 3)
]


async def _pin_old_activation(world) -> None:
    await world.db.execute(
        "INSERT OR REPLACE INTO executor_triage_state (id, activated_at) "
        "VALUES (1, datetime('now', '-1 day'))"
    )
    await world.db.commit()


async def _recs(world, task_id: int) -> list[dict]:
    await world.db.commit()
    return await _rows(
        world.db,
        "SELECT * FROM executor_triage_recommendations WHERE task_id = ? ORDER BY id",
        (task_id,),
    )


async def _count(world, sql: str, params: tuple) -> int:
    await world.db.commit()
    return (await _rows(world.db, sql, params))[0]["n"]


async def _feed(world, task_id: int) -> int:
    return await _count(
        world,
        "SELECT COUNT(*) AS n FROM task_updates WHERE task_id = ? "
        "AND content LIKE 'Разбор прогона k8 (тень, #1947)%'",
        (task_id,),
    )


async def _events(world, task_id: int) -> int:
    return await _count(
        world,
        "SELECT COUNT(*) AS n FROM events WHERE task_id = ? "
        "AND kind = 'executor_triage_recommended'",
        (task_id,),
    )


async def _k8_review(
    world,
    title: str,
    *,
    reason: str = orders.CLOSE_SUBMITTED,
    verdict: str = "changes_requested",
    findings: list | None = None,
    reopen: bool = False,
) -> tuple[int, int]:
    """Задача сдана прогоном k8 на поколении 1 и разобрана ревью.

    ``reopen`` - состояние «open после return-to-work» без самого return-to-work
    (путь свипа); иначе задача остаётся в review.
    """
    tid = await _task(world, title)
    oid = (await _launch_redeem(world, tid))["order"]["id"]
    row = (await _rows(world.db, "SELECT * FROM executor_orders WHERE id = ?", (oid,)))[
        0
    ]
    assert await orders.close_order(world.db, row, reason, orders.RUN_OUTCOME_FINISHED)
    await repo.update_task(
        world.db,
        tid,
        status="open" if reopen else "review",
        claimed_by=None,
        claim_session_id=None,
        submission_generation=1,
        review_verdict=verdict,
        review_verdict_generation=1,
        review_verdict_revision=2,
        review_findings=json.dumps(FINDINGS if findings is None else findings),
    )
    await world.db.commit()
    return tid, oid


async def _return_to_work(world, tid: int) -> None:
    resp = await world.client.post(
        f"/api/tasks/{tid}/return-to-work",
        json={"reason": "доработка по находкам"},
        headers=world.human_auth,
    )
    assert resp.status_code == 200, resp.text
    await world.db.commit()


async def _orders_and_codes(world, tid: int) -> tuple[int, int]:
    orders_n = await _count(
        world, "SELECT COUNT(*) AS n FROM executor_orders WHERE task_id = ?", (tid,)
    )
    codes_n = await _count(world, "SELECT COUNT(*) AS n FROM chat_pair_codes", ())
    return orders_n, codes_n


# ---------------------------------------------------------------------------
# AC-1
# ---------------------------------------------------------------------------


async def test_a_k8_task_back_from_review_gets_a_rework_recommendation(
    world, _no_cloud
):
    """AC-1: return-to-work и свип; k8, не k8, взята человеком, новый заказ."""
    await _pin_old_activation(world)

    # Живой путь: return-to-work пишет рекомендацию в своей транзакции.
    ta, oa = await _k8_review(world, "с k8, return-to-work")
    assert await _recs(world, ta) == []
    await _return_to_work(world, ta)
    recs = await _recs(world, ta)
    assert len(recs) == 1
    assert recs[0]["action"] == REWORK
    assert recs[0]["order_id"] == oa
    assert recs[0]["verdict_revision"] == 2
    assert recs[0]["findings_count"] == 3
    assert await _feed(world, ta) == 1
    assert await _events(world, ta) == 1
    before = await _orders_and_codes(world, ta)
    # Повтор свипа и рестарт не рождают второй записи, события и строки ленты.
    await triage.sweep(world.db)
    await triage.sweep(world.db)
    assert len(await _recs(world, ta)) == 1
    assert (await _feed(world, ta), await _events(world, ta)) == (1, 1)
    # Ни заказа, ни кода слоту: только рекомендация.
    assert await _orders_and_codes(world, ta) == before

    # Путь свипа: возврат уже произошёл, рекомендацию добирает свип.
    tb, ob = await _k8_review(world, "с k8, свип", reopen=True, findings=FINDINGS[:2])
    assert await _recs(world, tb) == []
    await triage.sweep(world.db)
    swept = await _recs(world, tb)
    assert [(r["action"], r["order_id"], r["findings_count"]) for r in swept] == [
        (REWORK, ob, 2)
    ]

    # Сдана не с k8: заказ закрыт не сдачей, сдачу сделал кто-то другой.
    tn, _ = await _k8_review(world, "сдана не с k8", reason=orders.CLOSE_EXPIRED)
    await _return_to_work(world, tn)
    # Host-заказов у задачи нет вовсе (облако, локально).
    tc = await _task(world, "облако")
    await repo.update_task(
        world.db,
        tc,
        status="open",
        submission_generation=1,
        review_verdict="changes_requested",
        review_verdict_generation=1,
        review_verdict_revision=1,
        review_findings=json.dumps(FINDINGS),
    )
    await world.db.commit()

    # Взята человеком и вердикт не changes_requested.
    th, _ = await _k8_review(world, "взята человеком", reopen=True)
    await repo.update_task(world.db, th, claimed_by="denis")
    await world.db.commit()
    tv, _ = await _k8_review(world, "одобрена", reopen=True, verdict="approved")

    # Новый заказ уже есть: рекомендации «разбудить» нет.
    td, _ = await _k8_review(world, "новый заказ", reopen=True)
    await _launch_redeem(world, td)
    await triage.sweep(world.db)
    for other in (tn, tc, th, tv, td):
        assert await _recs(world, other) == [], other
    assert await _feed(world, tn) == 0


async def test_rework_recommendation_respects_boundary_and_current_verdict(
    world, _no_cloud
):
    """Граница активации, неактуальный вердикт и архив рекомендации не дают."""
    # Граница: заказ закрыт ДО активации - история, а не свежий возврат.
    old, _ = await _k8_review(world, "до границы", reopen=True)
    await world.db.execute(
        "INSERT OR REPLACE INTO executor_triage_state (id, activated_at) "
        "VALUES (1, datetime('now', '+1 hour'))"
    )
    await world.db.commit()
    await triage.sweep(world.db)
    assert await _recs(world, old) == []
    # То же на живом пути: возврат при границе в будущем рекомендации не даёт.
    live_old, _ = await _k8_review(world, "возврат до границы")
    await _return_to_work(world, live_old)
    assert await _recs(world, live_old) == []

    await _pin_old_activation(world)
    # Вердикт одобрен: возвращать на доработку по находкам нечего.
    appr, _ = await _k8_review(world, "одобрена и возвращена", verdict="approved")
    await _return_to_work(world, appr)
    # Сдачу следующего поколения сделал не заказ: заказ закрыт на поколении 1.
    other, _ = await _k8_review(world, "сдача не заказа", reopen=True)
    await repo.update_task(
        world.db, other, submission_generation=2, review_verdict_generation=2
    )
    await world.db.commit()
    # Вердикт на более раннем поколении: задачу сдали заново, вердикта на ней нет.
    stale, _ = await _k8_review(world, "вердикт прошлого поколения", reopen=True)
    await repo.update_task(world.db, stale, review_verdict_generation=0)
    # Архивная задача.
    arch, _ = await _k8_review(world, "в архиве", reopen=True)
    await repo.update_task(world.db, arch, archived=1)
    await world.db.commit()
    await triage.sweep(world.db)
    assert await _recs(world, stale) == []
    assert await _recs(world, arch) == []
    assert await _recs(world, appr) == []
    assert await _recs(world, other) == []
    # Граница ушла назад: тот же заказ теперь после неё и получает рекомендацию.
    assert len(await _recs(world, old)) == 1


async def test_the_rework_recommendation_is_measured(world, _no_cloud):
    """AC-2: рекомендация, затем новый host-заказ -> new_order и совпадение."""
    await _pin_old_activation(world)
    tid, oid = await _k8_review(world, "замер")
    await world.db.execute(
        "INSERT INTO submissions (task_id, generation, sha, base_branch, submitted_at) "
        "VALUES (?, 1, 'abc', 'develop', datetime('now', '-2 hours'))",
        (tid,),
    )
    await world.db.commit()
    await _return_to_work(world, tid)
    assert [r["action"] for r in await _recs(world, tid)] == [REWORK]

    # Сдача, из-за которой пришёл вердикт, - не исход рекомендации.
    await asyncio.sleep(1.1)
    await triage.sweep(world.db)
    assert (await _recs(world, tid))[0]["followup"] is None

    # Новый заказ после рекомендации; постановка при этом могла подрасти.
    await repo.update_task(world.db, tid, statement_generation=3)
    await world.db.commit()
    new_oid = (await _launch_redeem(world, tid))["order"]["id"]
    assert new_oid != oid
    await asyncio.sleep(1.1)
    await triage.sweep(world.db)
    rec = (await _recs(world, tid))[0]
    assert rec["followup"] == "new_order"
    stats = await triage.agreement(world.db)
    assert (stats["total"], stats["matched"], stats["rate"]) == (1, 1, 1.0)
    # После замера задача не рекомендуется второй раз.
    await triage.sweep(world.db)
    assert len(await _recs(world, tid)) == 1


async def test_rework_followup_is_submitted_or_none_at_deadline(world, _no_cloud):
    """Сдача следующего поколения - submitted; срок без действия - none_72h."""
    await _pin_old_activation(world)
    done, _ = await _k8_review(world, "сдана дальше")
    quiet, _ = await _k8_review(world, "тишина")
    for tid in (done, quiet):
        await _return_to_work(world, tid)
    await world.db.execute(
        "INSERT INTO submissions (task_id, generation, sha, base_branch, submitted_at) "
        "VALUES (?, 2, 'def', 'develop', datetime('now', '-2 hours'))",
        (done,),
    )
    await repo.update_task(world.db, done, status="review", submission_generation=2)
    await world.db.execute(
        "UPDATE executor_triage_recommendations SET created_at = "
        "datetime('now', '-73 hours') WHERE task_id IN (?, ?)",
        (done, quiet),
    )
    await world.db.commit()
    await triage.sweep(world.db)
    assert (await _recs(world, done))[0]["followup"] == "submitted"
    assert (await _recs(world, quiet))[0]["followup"] == "none_72h"


async def test_rework_action_is_accepted_by_old_and_fresh_schema(world):
    """Миграция: старая таблица пересобрана с rework_on_host, строки целы."""
    import hub.db as db_module
    from tests.test_db_migrations import _SCHEMA

    step = "rework_on_host"
    names = {n for n, _sql in db_module._MIGRATIONS if step in n or "1947" in n}
    assert names, "шаг миграции #1947 не найден"
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    saved = db_module._MIGRATIONS
    try:
        await conn.executescript(_SCHEMA)
        db_module._MIGRATIONS = [m for m in saved if m[0] not in names]
        await db_module._migrate(conn)
        await conn.execute(
            "INSERT INTO executor_triage_recommendations (order_id, task_id, "
            "triage_class, class_name, action) VALUES (7, 5, 1, 'infra', 'to_owner')"
        )
        await conn.commit()
        old = await conn.execute_fetchall(
            "SELECT sql FROM sqlite_master WHERE name = "
            "'executor_triage_recommendations'"
        )
        assert REWORK not in old[0][0]
        db_module._MIGRATIONS = saved
        await db_module._migrate(conn)
        rows = await conn.execute_fetchall(
            "SELECT id, order_id, action, verdict_revision, findings_count "
            "FROM executor_triage_recommendations"
        )
        assert [tuple(r) for r in rows] == [(1, 7, "to_owner", None, None)]
        await conn.execute(
            "INSERT INTO executor_triage_recommendations (order_id, task_id, "
            "triage_class, class_name, action, verdict_revision, findings_count) "
            "VALUES (8, 5, 0, '', 'rework_on_host', 2, 3)"
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO executor_triage_recommendations (order_id, task_id, "
                "triage_class, class_name, action) VALUES (9, 5, 0, '', 'nope')"
            )
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO executor_triage_recommendations (order_id, task_id, "
                "triage_class, class_name, action) VALUES (8, 5, 0, '', 'to_owner')"
            )
        idx = await conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_executor_triage_recommendations_task'"
        )
        assert len(idx) == 1
    finally:
        db_module._MIGRATIONS = saved
        await conn.close()
    # Свежая схема того же вида: подключение теста поднимает все шаги.
    fresh = await _rows(
        world.db,
        "SELECT sql FROM sqlite_master WHERE name = 'executor_triage_recommendations'",
    )
    assert REWORK in fresh[0]["sql"]
    assert hub_db  # модуль подключён теми же шагами, что и прод
