"""Вахта stale отличает ожидание от смерти (#957).

Старый детектор спрашивал одно: «давно ли писали?» — и обязан был ошибаться
в обе стороны. Задача #927, честно отчитывавшаяся о суточном наблюдении,
получила по тревоге на каждый из четырёх отчётов (запись → ровно через 30
минут тревога). Задачи #443 и #880 — одобренная и никем не доставленная
работа — получили по одной тревоге и лежали в тишине неделю и двое суток:
любая запись открывала окно дедупа заново, а её отсутствие закрывало навсегда.

Новое определение: зависла — это «молчит и не объявила, чего ждёт», либо
«объявила и просрочила». Объявленное ожидание обязано иметь срок (бессрочное
«жду» неотличимо от брошенной задачи), до срока снимает тревогу, после —
эскалирует громче простого молчания. Молчание без объявления поднимается по
монотонной лестнице рубежей: конфигный порог, сутки, трое, неделя — каждый
рубеж один раз, и честная запись не открывает первый рубеж заново.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from fastapi import HTTPException

from hub import repository as repo
from hub import services
from hub.models import TaskCreate, TaskDeclareWait
from hub.poller import _sweep_stale_running

pytestmark = pytest.mark.usefixtures("db")


async def _running_task(db: aiosqlite.Connection, title: str = "Ждущая") -> int:
    tv = await services.create_task(db, TaskCreate(title=title))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: наблюдать")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")
    return tv.id


async def _age_feed(db: aiosqlite.Connection, task_id: int, minutes: int) -> None:
    """Состарить и задачу, и её ленту: тишина длится ``minutes`` минут."""
    stamp = (datetime.now(UTC) - timedelta(minutes=minutes)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    await db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (stamp, task_id))
    await db.execute(
        "UPDATE task_updates SET created_at=? WHERE task_id=?", (stamp, task_id)
    )
    await db.commit()


async def _stale_alerts(db: aiosqlite.Connection, task_id: int) -> list[str]:
    rows = await repo.get_task_updates(db, task_id)
    return [
        str(dict(r)["content"])
        for r in rows
        if dict(r)["kind"] == "alert" and "stale in " in str(dict(r)["content"])
    ]


def _until(minutes_from_now: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes_from_now)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


# ---------------------------------------------------------------------------
# AC-1 — объявленное и не истёкшее ожидание снимает тревогу
# ---------------------------------------------------------------------------


async def test_a_declared_current_wait_silences_the_watchdog(db):
    task_id = await _running_task(db)
    await services.declare_task_wait(
        db,
        task_id,
        TaskDeclareWait(
            waiting_for="сутки без ручных мержей (AC-5 наблюдения)",
            waiting_until=_until(24 * 60),
            agent="dev",
        ),
    )
    await _age_feed(db, task_id, minutes=90)

    await _sweep_stale_running(db)

    assert await _stale_alerts(db, task_id) == [], (
        "задача сказала, чего ждёт и до когда — тревога до срока не поднимается"
    )
    stale_ids = [dict(r)["id"] for r in await repo.list_stale_running(db, 30)]
    assert task_id not in stale_ids, "и в секцию Stale Tasks она не попадает"


# ---------------------------------------------------------------------------
# AC-2 — истёкшее ожидание поднимает тревогу о просрочке
# ---------------------------------------------------------------------------


async def test_a_lapsed_wait_alerts_and_names_the_lapse(db):
    task_id = await _running_task(db)
    await services.declare_task_wait(
        db,
        task_id,
        TaskDeclareWait(
            waiting_for="вердикт внешнего ревьюера",
            waiting_until=_until(5),
            agent="dev",
        ),
    )
    # Срок прошёл: сдвигаем дедлайн в прошлое, ленту старим за порог.
    lapsed = (datetime.now(UTC) - timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute("UPDATE tasks SET waiting_until=? WHERE id=?", (lapsed, task_id))
    await db.commit()
    await _age_feed(db, task_id, minutes=120)

    await _sweep_stale_running(db)

    alerts = await _stale_alerts(db, task_id)
    assert len(alerts) == 1
    assert "Ожидание просрочено" in alerts[0]
    assert "вердикт внешнего ревьюера" in alerts[0], "чего ждали — названо"
    assert lapsed in alerts[0], "до какого момента ждали — названо"
    assert "срок вышел" in alerts[0], "и насколько просрочено — тоже"


# ---------------------------------------------------------------------------
# AC-3 — молчание эскалируется по возрасту, а не глохнет после первой тревоги
# ---------------------------------------------------------------------------


async def test_true_silence_climbs_the_ladder_instead_of_one_alert_for_life(db):
    task_id = await _running_task(db)
    await _age_feed(db, task_id, minutes=45)
    await _sweep_stale_running(db)
    assert len(await _stale_alerts(db, task_id)) == 1, "первый рубеж — порог"

    # Кейс #443: тишина продолжается сутки. Старый дедуп молчал бы вечно.
    await _age_feed(db, task_id, minutes=25 * 60)
    await _sweep_stale_running(db)

    alerts = await _stale_alerts(db, task_id)
    assert len(alerts) == 2, "вторая тревога на рубеже суток, а не тишина навсегда"
    assert "24h" in alerts[-1]
    assert "сут" in alerts[-1], "возраст назван фактом, а не константой порога"

    # И дальше по лестнице — семь суток, кейс #443 дословно.
    await _age_feed(db, task_id, minutes=8 * 24 * 60)
    await _sweep_stale_running(db)
    alerts = await _stale_alerts(db, task_id)
    assert any("7d" in a for a in alerts), "недельный рубеж тоже поднимается"


# ---------------------------------------------------------------------------
# AC-4 — честные записи не наказываются тревогой за каждую (кейс #927)
# ---------------------------------------------------------------------------


async def test_honest_reports_do_not_earn_an_alert_each(db):
    task_id = await _running_task(db)
    await _age_feed(db, task_id, minutes=45)
    await _sweep_stale_running(db)
    assert len(await _stale_alerts(db, task_id)) == 1

    # Кейс #927: четыре честных отчёта, после каждого — молчание за порог.
    for note in ("наблюдение 1", "наблюдение 2", "наблюдение 3"):
        await repo.add_task_update(db, task_id, "dev", "status", note)
        await db.commit()
        await _age_feed(db, task_id, minutes=45)
        await _sweep_stale_running(db)

    alerts = await _stale_alerts(db, task_id)
    assert len(alerts) == 1, (
        "регрессия #927: было «запись → тревога» четыре раза подряд; "
        "рубеж порога поднимается один раз, отчётность больше не наказуема"
    )


# ---------------------------------------------------------------------------
# Объявление ожидания: срок обязателен и объявление видно в ленте
# ---------------------------------------------------------------------------


async def test_a_wait_without_a_deadline_is_refused(db):
    task_id = await _running_task(db)

    with pytest.raises(HTTPException) as exc:
        await services.declare_task_wait(
            db,
            task_id,
            TaskDeclareWait(waiting_for="когда-нибудь", waiting_until="", agent="dev"),
        )

    assert exc.value.status_code == 422
    detail = exc.value.detail
    assert (detail.get("reason") if isinstance(detail, dict) else "") == (
        "wait_needs_deadline"
    ), "бессрочное «жду» — это способ замолчать, а не ожидание"


async def test_a_deadline_in_the_past_is_refused(db):
    task_id = await _running_task(db)

    with pytest.raises(HTTPException) as exc:
        await services.declare_task_wait(
            db,
            task_id,
            TaskDeclareWait(
                waiting_for="вчерашний поезд",
                waiting_until=(datetime.now(UTC) - timedelta(hours=1)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                agent="dev",
            ),
        )

    assert exc.value.status_code == 422


async def test_the_declaration_is_a_visible_act_with_a_name_on_it(db):
    """Риск №1 постановки: «объявил ожидание и исчез» должен быть виден."""
    task_id = await _running_task(db)

    await services.declare_task_wait(
        db,
        task_id,
        TaskDeclareWait(
            waiting_for="ответ заказчика", waiting_until=_until(60), agent="ivan"
        ),
    )

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    line = next(u for u in updates if "Объявлено ожидание" in (u["content"] or ""))
    assert "ivan" in line["content"], "кто объявил — в ленте"
    assert "ответ заказчика" in line["content"]
    task = dict(await repo.get_task(db, task_id))
    assert task["waiting_declared_by"] == "ivan"

    # Снятие — тоже видимый акт.
    await services.declare_task_wait(
        db, task_id, TaskDeclareWait(waiting_for="", waiting_until="", agent="ivan")
    )
    task = dict(await repo.get_task(db, task_id))
    assert task["waiting_for"] == ""
    assert task["waiting_until"] in ("", None)


# ---------------------------------------------------------------------------
# AC-5 — строка инбокса называет факт, а не константу
# ---------------------------------------------------------------------------


async def test_the_inbox_row_names_the_fact_not_the_constant(db):
    from hub.services.dashboard import get_inbox_data

    lapsed_id = await _running_task(db, "Просрочила ожидание")
    await services.declare_task_wait(
        db,
        lapsed_id,
        TaskDeclareWait(
            waiting_for="суточное наблюдение", waiting_until=_until(5), agent="dev"
        ),
    )
    past = (datetime.now(UTC) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute("UPDATE tasks SET waiting_until=? WHERE id=?", (past, lapsed_id))
    silent_id = await _running_task(db, "Молчит седьмой день")
    await db.commit()
    await _age_feed(db, lapsed_id, minutes=180)
    await _age_feed(db, silent_id, minutes=7 * 24 * 60)

    data = await get_inbox_data(db)

    meta = data["stale_meta"]
    assert "суточное наблюдение" in meta[lapsed_id]["line"], (
        "у просрочившей — чего ждали"
    )
    assert past in meta[lapsed_id]["line"], "и до какого момента"
    assert "Тишина с" in meta[silent_id]["line"], (
        "у молчащей — фактический момент последней записи, не «30+ minutes»"
    )
    assert "30+ minutes" not in str(meta), "константа умерла"
