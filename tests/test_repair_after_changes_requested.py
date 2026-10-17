"""Круг починки задачи, запущенной хабом (#1783).

16–17.10 (my-home #1732): после сдачи прогон снят, а захват и сессия остались до
TTL; вердикт «на доработку» вернул задачу в running со старым захватом; кнопка
повторного прогона в running была скрыта, reaper сессию пропустил, purge её
удалил — тупик, снятый ручной правкой базы. Пустая пересдача того же коммита
к тому же засчиталась новым поколением.

Принцип: захват снимает только тот, кто доказал, что его держит ЭТОТ прогон
хаба (``tasks.executor_run_id``). Пустой ``claimed_by`` доказательством не служит.
"""

from __future__ import annotations

import asyncio

import aiosqlite

from hub import config
from hub import db as db_module
from hub import repository as repo
from hub.config import TokenIdentity
from hub.services import chat_pair as cp
from hub.services import executor_dispatch as ed
from hub.services import executor_launch as el
from hub.services import run_claim
from tests.test_executor_orders import (  # noqa: F401 - фикстуры и помощники слота
    _claim_and_start,
    _launch_redeem,
    _rows,
    _submit,
    _task,
    keys,
    world,
)
from tests.test_executor_dispatch import (
    _CREATED,
    _creator,
    _human,
    _launch_config,
    _task_with_findings,
)

SHA = "a" * 40


async def _row(db: aiosqlite.Connection, task_id: int) -> dict:
    await db.commit()
    return dict(await repo.get_task(db, task_id))  # type: ignore[arg-type]


async def _run_row(db: aiosqlite.Connection, task_id: int, **fields) -> dict:
    await db.commit()
    runs = [dict(r) for r in await repo.list_executor_runs(db, task_id)]
    return runs[-1] | fields


async def _hold(
    db: aiosqlite.Connection,
    task_id: int,
    run_id: int | None,
    *,
    session: bool = True,
    live: bool = False,
    status: str | None = None,
) -> None:
    """Захват агентом chat-pair; ``run_id`` — доказательство, что его держит прогон."""
    acting = await cp.get_acting_agent(db)
    assert acting is not None
    await repo.update_task(
        db,
        task_id,
        claimed_by=acting["username"],
        claim_session_id="s-run",
        claimed_at="2026-10-16T10:00:00+00:00",
        executor_run_id=run_id,
        implementer_principal_id=int(acting["id"]),
        **({"status": status} if status else {}),
    )
    if session:
        expires = (
            "datetime('now', '+2 hours')" if live else "datetime('now', '-1 minute')"
        )
        await db.execute(
            "INSERT INTO chat_pair_sessions (principal_id, token_hash, expires_at, "
            "kind, bound_task_id, bound_generation, executor_run_id) "
            f"VALUES (?, ?, {expires}, 'implementer', ?, 1, ?)",  # nosec B608 - literals
            (int(acting["id"]), f"h-{task_id}-{run_id}-{live}", task_id, run_id),
        )
    await db.commit()


async def _new_run(db: aiosqlite.Connection, task_id: int, generation: int = 2) -> int:
    row = await repo.create_executor_run(
        db,
        task_id=task_id,
        submission_generation=generation,
        agent_id="bc-new",
        run_id="run-new",
        model="m",
    )
    await db.commit()
    return int(row)


async def _finished_run_id(db: aiosqlite.Connection, task_id: int) -> int:
    return int((await _run_row(db, task_id))["id"])


# ---------------------------------------------------------------------------
# AC-1: исполнение закрыто сдачей — снят захват ЭТОГО прогона и только он
# ---------------------------------------------------------------------------


async def test_closing_a_run_releases_only_its_own_claim(world, db, monkeypatch):
    """Прогон taken_down после сдачи: сессии отозваны, слот закрыт, захват снят
    по executor_run_id, статус review. Позднее закрытие СТАРОГО прогона захват
    нового держателя не трогает."""
    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-ac1")
    await repo.update_task(db, task_id, status="running", submission_generation=0)
    old = await repo.create_executor_run(
        db,
        task_id=task_id,
        submission_generation=1,
        agent_id="a1",
        run_id="r1",
        model="m",
    )
    await _hold(db, task_id, old, live=True)
    await db.execute(
        "INSERT INTO executor_slots (task_id, channel, slot) VALUES (?, 'cloud', '')",
        (task_id,),
    )
    await repo.update_task(db, task_id, status="review", submission_generation=1)
    await db.commit()

    row = dict(await repo.get_executor_run(db, old))  # type: ignore[arg-type]
    await ed._settle(
        db,
        {**row, "cancel_intent": ed.OUTCOME_TAKEN_DOWN},
        {"status": "CANCELLED"},
        1,
        1.0,
    )
    await db.commit()

    task = await _row(db, task_id)
    assert task["status"] == "review"
    assert (task["claimed_by"], task["claim_session_id"], task["executor_run_id"]) == (
        None,
        None,
        None,
    )
    sessions = await db.execute_fetchall(
        "SELECT revoked_at FROM chat_pair_sessions WHERE executor_run_id = ?", (old,)
    )
    assert all(s[0] for s in sessions) and sessions
    slot = await db.execute_fetchall(
        "SELECT released_at FROM executor_slots WHERE task_id = ?", (task_id,)
    )
    assert slot[0][0]

    # Новый держатель; закрытие старого прогона (повторное, позднее) его не трогает.
    new = await _new_run(db, task_id, 2)
    await _hold(db, task_id, new, live=True, status="running")
    await db.execute(
        "UPDATE executor_runs SET outcome='taken_down', finished_at=datetime('now') "
        "WHERE id = ?",
        (new,),
    )
    # новый прогон «закончился» сам, но держит захват другой, более поздний прогон
    newest = await _new_run(db, task_id, 3)
    await repo.update_task(db, task_id, executor_run_id=newest)
    await db.commit()
    assert await run_claim.release_after_run_closed(db, old, why="late") is False
    assert await run_claim.release_after_run_closed(db, new, why="late") is False
    held = await _row(db, task_id)
    assert held["executor_run_id"] == newest and held["claimed_by"]

    await _phase_slot_submission_releases_the_claim(world)


async def test_a_manual_claim_is_not_released_by_a_run_closing(db, monkeypatch):
    """Пустой executor_run_id (ручной захват) — не доказательство: прогон закрыт, захват цел."""
    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-manual")
    run_id = await _finished_run_id(db, task_id)
    await _hold(db, task_id, None)
    assert await run_claim.release_after_run_closed(db, run_id, why="x") is False
    assert (await _row(db, task_id))["claimed_by"]


async def test_a_claim_by_a_run_session_records_the_run(client, db, monkeypatch):
    """claim и pair-start сессией с заказом пишут executor_run_id в той же транзакции;
    ручной захват — NULL, и чужой прежний хвост у нового держателя стирается."""
    _launch_config(monkeypatch)
    monkeypatch.setattr(
        config, "HUB_TOKENS", {"tok-1783": TokenIdentity("bot", "agent")}
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    human = await _human(db)
    _, task_id = await _task_with_findings(db, slug="rc-claim")
    await repo.update_task(db, task_id, status="open", executor_run_id=999)
    await db.commit()
    run = await _new_run(db, task_id, 2)
    code, _ = await cp.issue_run_code(
        db, task_id, 2, issued_by_principal_id=human, executor_run_id=run
    )
    redeemed = await client.post("/api/auth/chat-pair/redeem", json={"code": code})
    assert redeemed.status_code == 200, redeemed.text
    headers = {"Authorization": f"Bearer {redeemed.json()['token']}"}
    claimed = await client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": config.CHAT_PAIR_AGENT, "session_id": "s-1783"},
        headers=headers,
    )
    assert claimed.status_code == 200, claimed.text
    assert (await _row(db, task_id))["executor_run_id"] == run


# ---------------------------------------------------------------------------
# AC-2: после «на доработку» кнопка видна сразу, допуск и бронь атомарны
# ---------------------------------------------------------------------------


async def _verdict(client, task_id: int) -> dict:
    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "findings": [{"id": 1, "severity": "high", "message": "Fix the race"}],
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_repair_is_offered_and_atomic_after_changes_requested(
    client, db, db_dsn, monkeypatch
):
    """Вердикт «на доработку» по задаче завершённого прогона хаба: захват снят,
    кнопка видна сразу и без TTL, повторный прогон запускается; два нажатия и чужой
    claim в гонке — ровно один держатель, проигравшему исполнитель не покупается;
    живая работа — кнопки нет, причина названа."""
    _launch_config(monkeypatch)
    calls = _creator(monkeypatch, [_CREATED, _CREATED])
    human = await _human(db)
    _, task_id = await _task_with_findings(db, slug="rc-ac2")
    run_id = await _finished_run_id(db, task_id)
    await _hold(db, task_id, run_id, session=False)

    verdict = await _verdict(client, task_id)
    assert verdict["status"] == "running"
    task = await _row(db, task_id)
    assert (task["claimed_by"], task["executor_run_id"]) == (None, None)
    assert await el.repair_offered(db, task), "кнопка видна сразу, без ожидания TTL"

    # Гонка двух нажатий с двух соединений: бронь ровно одна, платный заказ один.
    other = await db_module.connect(db_dsn)
    try:
        results = await asyncio.gather(
            el.repair_executor(db, task_id, issuer_principal_id=human, issuer="a"),
            el.repair_executor(other, task_id, issuer_principal_id=human, issuer="b"),
        )
    finally:
        await other.close()
    assert sorted(r.launched for r in results) == [False, True], results
    assert len(calls) == 1, "проигравшему гонку исполнитель не куплен"
    after = await _row(db, task_id)
    assert after["status"] == "open"
    running = [
        r
        for r in await repo.list_executor_runs(db, task_id)
        if r["outcome"] == "running"
    ]
    assert len(running) == 1


async def _phase_busy_task_blocks_the_repair(client, db, monkeypatch):
    """Чужой claim, живая сессия или идущий прогон: кнопки нет, заказа нет,
    задача не тронута, причина читается на карточке (repair_state)."""
    _launch_config(monkeypatch)
    calls = _creator(monkeypatch, [_CREATED])
    human = await _human(db)
    _, task_id = await _task_with_findings(db, slug="rc-ac2-busy")
    await repo.update_task(
        db,
        task_id,
        status="running",
        review_verdict="changes_requested",
        review_verdict_generation=1,
    )
    await db.execute(
        "UPDATE tasks SET claimed_by='someone', claim_session_id='s-x' WHERE id=?",
        (task_id,),
    )
    await db.commit()
    task = await _row(db, task_id)
    offered, why = await el.repair_state(db, task)
    assert not offered and "claimed_by" in why
    refused = await el.repair_executor(
        db, task_id, issuer_principal_id=human, issuer="o"
    )
    assert not refused.launched and "claimed_by" in refused.reason
    assert calls == []
    assert (await _row(db, task_id))["status"] == "running"

    # Бронь, появившаяся после быстрой проверки, видна под write-локом.
    await repo.update_task(db, task_id, claimed_by=None, claim_session_id=None)
    await _new_run(db, task_id, 2)
    refused = await el.repair_executor(
        db, task_id, issuer_principal_id=human, issuer="o"
    )
    assert not refused.launched and calls == []
    assert (await _row(db, task_id))["status"] == "running"


async def _phase_repair_reads_the_report_at_the_same_tip(db, monkeypatch):
    """Нет отчёта по текущему поколению — находки последнего отчёта при той же
    вершине; чистый свежий отчёт наследование прекращает; иная вершина — нет."""
    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-inherit")
    for gen in (1, 2):
        await db.execute(
            "INSERT OR REPLACE INTO submissions (task_id, generation, sha) VALUES (?, ?, ?)",
            (task_id, gen, SHA),
        )
    await repo.update_task(db, task_id, status="running", submission_generation=2)
    await db.commit()
    task = await _row(db, task_id)
    assert await el._current_findings(db, task), "наследуются находки поколения 1"

    # другая вершина — кода уже не те
    await db.execute("UPDATE submissions SET sha = ? WHERE generation = 2", ("b" * 40,))
    await db.commit()
    assert await el._current_findings(db, await _row(db, task_id)) == []

    # чистый свежий отчёт текущего поколения наследование прекращает
    await db.execute("UPDATE submissions SET sha = ? WHERE generation = 2", (SHA,))
    await db.execute(
        "INSERT INTO machine_reviews (task_id, submission_generation, harness_skill, "
        "findings_confirmed, unresolved) VALUES (?, 2, 'multi-agent-review', '[]', '[]')",
        (task_id,),
    )
    await db.commit()
    assert await el._current_findings(db, await _row(db, task_id)) == []


# ---------------------------------------------------------------------------
# AC-3: тупик «захват без сессий» — reaper от задач
# ---------------------------------------------------------------------------


async def test_stuck_claim_without_sessions_is_released(client, db, monkeypatch):
    """review → сессия истекла → purge → changes_requested: задача running с
    захватом завершённого прогона и без сессий снимается reaper'ом одной строкой
    в ленте; захват человека и живая работа не трогаются."""
    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-ac3")
    run_id = await _finished_run_id(db, task_id)
    # Инцидент: сессия прогона истекла, пока задача была в review; purge её убрал.
    await _hold(db, task_id, run_id, session=True)
    await cp.purge_expired(db)
    assert not await db.execute_fetchall(
        "SELECT 1 FROM chat_pair_sessions WHERE bound_task_id = ?", (task_id,)
    )
    # Сам вердикт (b) освободил бы захват; здесь воспроизводим уже зависшее состояние.
    await repo.update_task(
        db,
        task_id,
        status="running",
        review_verdict="changes_requested",
        review_verdict_generation=1,
    )
    await db.commit()
    stuck = await _row(db, task_id)
    assert stuck["claimed_by"] and stuck["executor_run_id"] == run_id

    await cp.purge_expired(db)

    freed = await _row(db, task_id)
    assert (freed["claimed_by"], freed["executor_run_id"]) == (None, None)
    assert freed["status"] == "running"
    notes = [
        u
        for u in await db.execute_fetchall(
            "SELECT content FROM task_updates WHERE task_id = ? AND content LIKE '%Захват снят%'",
            (task_id,),
        )
    ]
    assert len(notes) == 1

    await _phase_reaper_leaves_what_is_not_provable(db, monkeypatch)
    await _phase_manual_session_released_by_ttl(db, monkeypatch)
    await _phase_purge_keeps_sessions_a_claim_points_to(db, monkeypatch)


async def _phase_reaper_leaves_what_is_not_provable(db, monkeypatch):
    """Захват человека (executor_run_id NULL), живой прогон, живая сессия, бронь
    между выборкой и освобождением, чужой прогон в указателе — не освобождаются."""
    _launch_config(monkeypatch)
    # 1. человек
    _, human_task = await _task_with_findings(db, slug="rc-ac3-human")
    await _hold(db, human_task, None, session=False, status="running")
    # 2. прогон идёт
    _, live_run_task = await _task_with_findings(db, slug="rc-ac3-live")
    running = await _new_run(db, live_run_task, 2)
    await _hold(db, live_run_task, running, session=False, status="running")
    # 3. прогон закончен, но сессия жива
    _, session_task = await _task_with_findings(db, slug="rc-ac3-session")
    done = await _finished_run_id(db, session_task)
    await _hold(db, session_task, done, session=True, live=True, status="running")
    # 4. указатель на прогон ЧУЖОЙ задачи
    _, foreign_task = await _task_with_findings(db, slug="rc-ac3-foreign")
    await _hold(db, foreign_task, done, session=False, status="running")
    # 5. бронь после выборки
    _, booked_task = await _task_with_findings(db, slug="rc-ac3-booked")
    booked_done = await _finished_run_id(db, booked_task)
    await _hold(db, booked_task, booked_done, session=False, status="running")
    candidates = await run_claim.stuck_candidates(db)
    assert booked_task in candidates
    await _new_run(db, booked_task, 2)

    for task_id in (human_task, live_run_task, session_task, foreign_task, booked_task):
        async with db_module.write_transaction(db):
            assert await run_claim.release_stuck_task(db, task_id) is False, task_id
        assert (await _row(db, task_id))["claimed_by"], task_id


async def _phase_manual_session_released_by_ttl(db, monkeypatch):
    """Ручная сессия (без executor_run_id): по TTL задача идёт в open, как до задачи."""
    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-manual-ttl")
    await _hold(db, task_id, None, session=True, status="running")
    await cp.purge_expired(db)
    assert (await _row(db, task_id))["status"] == "open"


async def _phase_purge_keeps_sessions_a_claim_points_to(db, monkeypatch):
    """purge не удаляет сессии, пока захват задачи ссылается на их прогон.

    Первый проход reaper'а отказан идущим облачным прогоном (бронь), поэтому
    задача остаётся running; её мёртвые сессии по-прежнему лежат в базе.
    """
    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-purge")
    done = await _finished_run_id(db, task_id)
    await _hold(db, task_id, done, session=True, status="running")
    await _new_run(db, task_id, 2)
    await cp.purge_expired(db)
    left = await db.execute_fetchall(
        "SELECT 1 FROM chat_pair_sessions WHERE bound_task_id = ?", (task_id,)
    )
    assert left, "сессии прогона, на который ссылается захват, не удалены"
    assert (await _row(db, task_id))["claimed_by"]


async def _phase_slot_submission_releases_the_claim(world):
    """Канал слота: принятая сдача снимает захват её прогона (executor_orders)."""
    from hub.services import executor_orders as orders

    task_id = await _task(world, "захват слота после сдачи")
    alive = await _launch_redeem(world, task_id)
    await _claim_and_start(world, task_id, alive["session"])
    held = (await _rows(world.db, "SELECT * FROM tasks WHERE id = ?", (task_id,)))[0]
    assert held["executor_run_id"] is not None and held["claimed_by"]
    await _submit(world, task_id, generation=1)
    await orders.settle(world.db)
    await world.db.commit()
    after = (await _rows(world.db, "SELECT * FROM tasks WHERE id = ?", (task_id,)))[0]
    assert (after["status"], after["claimed_by"], after["executor_run_id"]) == (
        "review",
        None,
        None,
    )
    sessions = await _rows(
        world.db,
        "SELECT revoked_at FROM chat_pair_sessions WHERE bound_task_id = ?",
        (task_id,),
    )
    assert sessions and all(x["revoked_at"] for x in sessions)
