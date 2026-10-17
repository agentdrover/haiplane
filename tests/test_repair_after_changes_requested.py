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
import pytest

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
    _code_of,
    _launch,
    _orders,
    _redeem,
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
CLAIMED_AT = "2026-10-16T10:00:00+00:00"


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
        claimed_at=CLAIMED_AT,
        claim_nonce="n-run" if session else None,  # метка — у захвата сессии
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
            "kind, bound_task_id, bound_generation, executor_run_id, claimed_session) "
            f"VALUES (?, ?, {expires}, 'implementer', ?, 1, ?, 'n-run')",  # nosec B608 - literals
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


async def test_closing_a_run_releases_only_its_own_claim(
    world,  # noqa: F811
    db,
    monkeypatch,
):
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
        "INSERT INTO executor_slots (task_id, channel, slot, executor_run_id) "
        "VALUES (?, 'cloud', '', ?)",
        (task_id, old),
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

    # Одобренная задача, вернувшаяся в running после снятия захвата (ждёт доставки),
    # мёртвой сессией прогона в open не уходит: реапер её держателем не считает.
    await _approved_in_running_is_not_yanked(db, old_task=task_id)
    await repo.update_task(db, task_id, status="review")
    await db.commit()

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
    # Держатель закончил тоже: от чужого закрытия захват спасает только доказательство.
    await db.execute(
        "UPDATE executor_runs SET outcome='finished', finished_at=datetime('now') "
        "WHERE id = ?",
        (newest,),
    )
    await db.commit()
    assert await run_claim.release_after_run_closed(db, old, why="late") is False
    assert await run_claim.release_after_run_closed(db, new, why="late") is False
    held = await _row(db, task_id)
    assert held["executor_run_id"] == newest and held["claimed_by"]

    await _phase_slot_submission_releases_the_claim(world)
    await _phase_foreign_slot_is_not_the_runs(db, monkeypatch)
    await _phase_terminal_status_does_not_wait_for_cost(db, monkeypatch)


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
    fresh = await db.execute_fetchall(
        "SELECT claimed_session FROM chat_pair_sessions WHERE executor_run_id = ?",
        (run,),
    )
    assert fresh[0][0] is None, "новая сессия задачу ещё не брала"
    claimed = await client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": config.CHAT_PAIR_AGENT, "session_id": "s-1783"},
        headers=headers,
    )
    assert claimed.status_code == 200, claimed.text
    assert (await _row(db, task_id))["executor_run_id"] == run
    bound = await db.execute_fetchall(
        "SELECT claimed_session FROM chat_pair_sessions WHERE executor_run_id = ?",
        (run,),
    )
    assert bound[0][0] and bound[0][0] == (await _row(db, task_id))["claim_nonce"]

    # done держателя из claimed захват прогона хаба НЕ снимает: его (с сессиями и
    # слотом) снимает закрытие прогона, иначе доказательство ушло бы раньше.
    from hub import models, services

    await services.add_update(
        db,
        task_id,
        models.TaskUpdateCreate(
            agent=config.CHAT_PAIR_AGENT, kind="done", content="готово"
        ),
    )
    kept = await _row(db, task_id)
    assert (kept["claimed_by"], kept["executor_run_id"]) == (
        config.CHAT_PAIR_AGENT,
        run,
    )
    live = await db.execute_fetchall(
        "SELECT 1 FROM chat_pair_sessions WHERE bound_task_id = ? AND revoked_at IS NULL",
        (task_id,),
    )
    assert live
    # Снятие захвата на любом пути (в том числе сырым SQL) уносит доказательство.
    await db.execute("UPDATE tasks SET claimed_by = NULL WHERE id = ?", (task_id,))
    await db.commit()
    assert (await _row(db, task_id))["executor_run_id"] is None

    # pair-start из open без claim — тоже захват: доказательство пишется в нём.
    _, second = await _task_with_findings(db, slug="rc-claim-ps")
    await repo.update_task(db, second, status="open")
    await db.commit()
    run2 = await _new_run(db, second, 2)
    code2, _ = await cp.issue_run_code(
        db, second, 2, issued_by_principal_id=human, executor_run_id=run2
    )
    redeemed2 = await client.post("/api/auth/chat-pair/redeem", json={"code": code2})
    assert redeemed2.status_code == 200, redeemed2.text
    started = await client.post(
        f"/api/tasks/{second}/pair-start",
        json={
            "assigned_agent": config.CHAT_PAIR_AGENT,
            "plan": "Plan: go",
            "session_id": "s-1783-ps",
            "git_mode": "remote",
        },
        headers={"Authorization": f"Bearer {redeemed2.json()['token']}"},
    )
    assert started.status_code == 200, started.text
    assert (await _row(db, second))["executor_run_id"] == run2
    bound2 = await db.execute_fetchall(
        "SELECT claimed_session FROM chat_pair_sessions WHERE executor_run_id = ?",
        (run2,),
    )
    assert bound2[0][0] and bound2[0][0] == (await _row(db, second))["claim_nonce"]


# ---------------------------------------------------------------------------
# AC-2: после «на доработку» кнопка видна сразу, допуск и бронь атомарны
# ---------------------------------------------------------------------------


async def _verdict(client, task_id: int, headers: dict | None = None) -> dict:
    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "findings": [{"id": 1, "severity": "high", "message": "Fix the race"}],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_repair_is_offered_and_atomic_after_changes_requested(
    client,
    db,
    db_dsn,
    monkeypatch,
    world,  # noqa: F811
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

    verdict = await _verdict(client, task_id, world.human_auth)
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

    await _phase_busy_task_blocks_the_repair(db, monkeypatch, human)
    await _phase_booking_blocks_a_manual_claim(db, monkeypatch)
    await _phase_slot_booking_blocks_a_manual_claim(world)
    await _phase_slot_second_executor_is_refused_both_ways(world)
    await _phase_repair_reads_the_report_at_the_same_tip(db, monkeypatch)


async def _phase_busy_task_blocks_the_repair(db, monkeypatch, human):
    """Чужой claim, живая сессия или идущий прогон: кнопки нет, заказа нет,
    задача не тронута, причина читается на карточке (repair_state)."""
    _launch_config(monkeypatch)
    calls = _creator(monkeypatch, [_CREATED])
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

    # Допуск под write-локом сам перечитывает признаки: быстрая проверка вне лока
    # обойдена, а чужой claim и идущий прогон всё равно не дают вернуть задачу.
    async with db_module.write_transaction(db):
        busy = await el._admit_rework(db, await _row(db, task_id), "o")
    assert busy, "идущий прогон (бронь) не даёт вернуть задачу"
    assert (await _row(db, task_id))["status"] == "running"
    await db.execute(
        "UPDATE executor_runs SET outcome='failed', finished_at=datetime('now') "
        "WHERE task_id = ? AND outcome = 'running'",
        (task_id,),
    )
    await db.execute("UPDATE tasks SET claimed_by='someone' WHERE id=?", (task_id,))
    await db.commit()
    async with db_module.write_transaction(db):
        claimed = await el._admit_rework(db, await _row(db, task_id), "o")
    assert "claimed_by" in claimed
    assert (await _row(db, task_id))["status"] == "running"

    # Свежий статус под локом не running и не open (чужой claim) — бронь отказана.
    await repo.update_task(db, task_id, claimed_by=None, status="claimed")
    await db.commit()
    async with db_module.write_transaction(db):
        foreign = await el._admit_rework(db, await _row(db, task_id), "o")
    assert "claimed" in foreign


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
    await db.execute(
        "INSERT INTO executor_slots (task_id, channel, slot, executor_run_id) "
        "VALUES (?, 'cloud', '', ?)",
        (task_id, run_id),
    )
    await db.commit()
    await cp.purge_expired(db)
    # Теперь и в review: доказательство есть, прогон закончен — страховка снимает захват
    # (раньше он висел до вердикта и становился тупиком).
    assert (await _row(db, task_id))["claimed_by"] is None
    # Сам вердикт (b) освободил бы захват; здесь воспроизводим уже зависшее состояние.
    await _hold(db, task_id, run_id, session=False)
    await db.execute(
        "INSERT INTO executor_slots (task_id, channel, slot, executor_run_id) "
        "VALUES (?, 'cloud', '', ?)",
        (task_id, run_id),
    )
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
    assert not await db.execute_fetchall(
        "SELECT 1 FROM executor_slots WHERE task_id = ? AND released_at IS NULL",
        (task_id,),
    ), "слот ушёл вместе с захватом: иначе open_slot блокирует повторный запуск"
    assert freed["status"] == "running"
    notes = [
        u
        for u in await db.execute_fetchall(
            "SELECT content FROM task_updates WHERE task_id = ? AND content LIKE '%Захват снят%'",
            (task_id,),
        )
    ]
    assert len(notes) == 2  # снятие в review и снятие после вердикта

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


async def _phase_slot_submission_releases_the_claim(slot_world):
    """Канал слота: принятая сдача снимает захват её прогона (executor_orders)."""
    from hub.services import executor_orders as orders

    task_id = await _task(slot_world, "захват слота после сдачи")
    alive = await _launch_redeem(slot_world, task_id)
    await _claim_and_start(slot_world, task_id, alive["session"])
    held = (await _rows(slot_world.db, "SELECT * FROM tasks WHERE id = ?", (task_id,)))[
        0
    ]
    assert held["executor_run_id"] is not None and held["claimed_by"]
    await _submit(slot_world, task_id, generation=1)
    await orders.settle(slot_world.db)
    await slot_world.db.commit()
    after = (
        await _rows(slot_world.db, "SELECT * FROM tasks WHERE id = ?", (task_id,))
    )[0]
    assert (after["status"], after["claimed_by"], after["executor_run_id"]) == (
        "review",
        None,
        None,
    )
    sessions = await _rows(
        slot_world.db,
        "SELECT revoked_at FROM chat_pair_sessions WHERE bound_task_id = ?",
        (task_id,),
    )
    assert sessions and all(x["revoked_at"] for x in sessions)


async def _approved_in_running_is_not_yanked(db, old_task: int) -> None:
    await repo.update_task(db, old_task, status="running")
    await db.commit()
    assert await cp.release_expired_implementer_tasks(db) == []
    after = await _row(db, old_task)
    assert after["status"] == "running" and after["implementer_principal_id"]


async def test_the_reaper_does_not_return_a_task_whose_claim_was_just_released(
    db, monkeypatch
):
    """Реапер выбрал задачу со старым захватом, а между выборкой и записью захват
    снял run_claim: задача (running, без захвата и доказательства) в open не уходит."""
    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-reaper-race")
    run_id = await _finished_run_id(db, task_id)
    await _hold(db, task_id, run_id, session=True, status="running")
    await repo.update_task(db, task_id, claimed_by=None, claim_session_id=None)
    await db.commit()
    freed = await cp.return_task_to_open(
        db,
        task_id,
        from_status="running",
        note="x",
        event_kind="chat_pair_implementer_expired",
        holders=frozenset(
            {int((await cp.get_acting_agent(db))["id"])}  # type: ignore[index]
        ),
        session_runs=frozenset({run_id}),
    )
    assert freed is False
    assert (await _row(db, task_id))["status"] == "running"


# ---------------------------------------------------------------------------
# Круг 2 (CHANGES_REQUESTED на поколение 1): три P1
# ---------------------------------------------------------------------------


async def _slot_note(db, task_id: int) -> None:
    """Запись «канал: cloud» в ленте — так record_capture метит захват сессией прогона."""
    await repo.add_task_update(db, task_id, "hub", "status", "канал: cloud")


async def _live_run_session(db, task_id: int, run_id: int, token: str) -> None:
    acting = await cp.get_acting_agent(db)
    await db.execute(
        "INSERT INTO chat_pair_sessions (principal_id, token_hash, expires_at, kind, "
        "bound_task_id, bound_generation, executor_run_id, channel, slot) VALUES "
        "(?, ?, datetime('now', '+1 hour'), 'implementer', ?, 1, ?, 'cloud', '')",
        (int(acting["id"]), token, task_id, run_id),
    )


async def test_p1_first_pass_reaper_leaves_a_human_claim_after_a_manual_session(
    db, monkeypatch
):
    """P1-2 (вариант): истёкшая РУЧНАЯ сессия (без прогона) и свежий захват человека —
    принципал держателя неизвестен, имя не её: реапер задачу не трогает."""
    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-p1-human-manual")
    await _hold(db, task_id, None, session=True, status="running")
    await db.execute(
        "UPDATE tasks SET claimed_by='alice', claim_session_id='s-alice', "
        "claim_nonce = NULL, implementer_principal_id = NULL WHERE id = ?",
        (task_id,),
    )
    await db.commit()
    assert await cp.release_expired_implementer_tasks(db) == []
    assert (await _row(db, task_id))["claimed_by"] == "alice"


async def test_p1_first_pass_reaper_leaves_a_same_identity_retake(db, monkeypatch):
    """P1-2 (вариант): новый захват под тем же именем агента, но без доказательства
    прогона — истёкшая сессия СТАРОГО прогона его не снимает."""
    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-p1-retake-reaper")
    run_id = await _finished_run_id(db, task_id)
    await _hold(db, task_id, None, session=False, status="running")
    acting = await cp.get_acting_agent(db)
    await db.execute(
        "INSERT INTO chat_pair_sessions (principal_id, token_hash, expires_at, kind, "
        "bound_task_id, bound_generation, executor_run_id) VALUES "
        "(?, 'p1-2b', datetime('now', '-1 minute'), 'implementer', ?, 1, ?)",
        (int(acting["id"]), task_id, run_id),
    )
    await db.commit()
    assert await cp.release_expired_implementer_tasks(db) == []
    assert (await _row(db, task_id))["status"] == "running"


async def test_p1_first_pass_reaper_leaves_a_human_claim(db, monkeypatch):
    """P1-2: истёкшая сессия СТАРОГО прогона не снимает текущий захват человека
    (executor_run_id и implementer_principal_id пусты)."""
    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-p1-human")
    run_id = await _finished_run_id(db, task_id)
    await _hold(db, task_id, None, session=False, status="running")
    await db.execute(
        "UPDATE tasks SET claimed_by='alice', claim_session_id='s-alice', "
        "implementer_principal_id = NULL WHERE id = ?",
        (task_id,),
    )
    acting = await cp.get_acting_agent(db)
    await db.execute(
        "INSERT INTO chat_pair_sessions (principal_id, token_hash, expires_at, kind, "
        "bound_task_id, bound_generation, executor_run_id) VALUES "
        "(?, 'p1-2', datetime('now', '-1 minute'), 'implementer', ?, 1, ?)",
        (int(acting["id"]), task_id, run_id),
    )
    await db.commit()
    assert await cp.release_expired_implementer_tasks(db) == []
    after = await _row(db, task_id)
    assert (after["status"], after["claimed_by"]) == ("running", "alice")


async def _phase_foreign_slot_is_not_the_runs(db, monkeypatch):
    """Слот без хозяина или чужого прогона — признак работы: захват не снимается,
    и чужой слот не закрывается вместе с захватом."""
    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-foreign-slot")
    run_id = await _finished_run_id(db, task_id)
    await _hold(db, task_id, run_id, session=False)
    await db.execute(
        "INSERT INTO executor_slots (task_id, channel, slot, executor_run_id) "
        "VALUES (?, 'slot', 'k9', NULL)",
        (task_id,),
    )
    await db.commit()
    assert await run_claim.release_after_run_closed(db, run_id, why="x") is False
    assert (await _row(db, task_id))["claimed_by"]
    await db.execute(
        "UPDATE executor_slots SET executor_run_id = ? WHERE task_id = ?",
        (run_id, task_id),
    )
    await db.commit()
    assert await run_claim.release_after_run_closed(db, run_id, why="x") is True


async def _phase_terminal_status_does_not_wait_for_cost(db, monkeypatch):
    """Провайдер сказал «конец», цены нет: захват снят сразу, строка прогона ждёт цену."""
    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-cost-wait")
    run_id = await _new_run(db, task_id, 1)
    await _hold(db, task_id, run_id, session=True, live=True, status="review")
    await repo.update_task(db, task_id, submission_generation=1)
    await db.commit()
    row = dict(await repo.get_executor_run(db, run_id))  # type: ignore[arg-type]
    await ed._settle(
        db,
        {**row, "cancel_intent": ed.OUTCOME_TAKEN_DOWN, "cents": None},
        {"status": "CANCELLED"},
        None,
        None,
    )
    await db.commit()
    assert dict(await repo.get_executor_run(db, run_id))["outcome"] == "running"  # type: ignore[arg-type]
    after = await _row(db, task_id)
    assert (after["claimed_by"], after["executor_run_id"]) == (None, None)


async def _phase_booking_blocks_a_manual_claim(db, monkeypatch):
    """Бронь круга починки: задача open, прогон «идёт» без агента. Ручные claim и
    pair-start на другом соединении отказаны, пока бронь жива; брошенная бронь не мешает."""
    from fastapi import HTTPException

    from hub import models
    from hub.services import lifecycle

    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-booking")
    await repo.update_task(db, task_id, status="open")
    booking = await repo.create_executor_run(
        db,
        task_id=task_id,
        submission_generation=2,
        agent_id="",
        run_id="",
        model="m",
    )
    await db.commit()
    with pytest.raises(HTTPException) as claim_refused:
        await lifecycle.claim_task(
            db, task_id, models.TaskClaim(agent="alice", session_id="s-a")
        )
    assert claim_refused.value.status_code == 409
    with pytest.raises(HTTPException) as start_refused:
        await lifecycle.pair_start_task(
            db,
            task_id,
            models.TaskPairStart(assigned_agent="alice", plan="Plan: go"),
            caller="alice",
        )
    assert start_refused.value.status_code == 409
    assert (await _row(db, task_id))["status"] == "open"
    await db.execute(
        "UPDATE executor_runs SET outcome='failed', finished_at=datetime('now') "
        "WHERE id = ?",
        (booking,),
    )
    await db.commit()
    claimed = await lifecycle.claim_task(
        db, task_id, models.TaskClaim(agent="alice", session_id="s-a")
    )
    assert claimed.status.value == "claimed"


async def test_p1_reaper_gives_back_only_the_claim_the_session_made(db, monkeypatch):
    """Сессия запоминает claim_session_id, под которым взяла задачу (bind_claim);
    независимый захват тем же принципалом под другим session_id её истечением не
    снимается, а её собственный — снимается."""
    _launch_config(monkeypatch)
    acting = await cp.get_acting_agent(db)
    for tag, nonce, expected in (("own", "n-run", ["task"]), ("other", None, [])):
        _, task_id = await _task_with_findings(db, slug=f"rc-bind-{tag}")
        await _hold(db, task_id, None, session=False, status="running")
        await db.execute(
            "UPDATE tasks SET claim_nonce = ? WHERE id = ?", (nonce, task_id)
        )
        await db.execute(
            "INSERT INTO chat_pair_sessions (principal_id, token_hash, expires_at, "
            "kind, bound_task_id, bound_generation, claimed_session) VALUES "
            "(?, ?, datetime('now', '-1 minute'), 'implementer', ?, 1, 'n-run')",
            (int(acting["id"]), f"bind-{tag}", task_id),
        )
        await db.commit()
        released = await cp.release_expired_implementer_tasks(db)
        assert released == ([task_id] if expected else []), tag
        assert (await _row(db, task_id))["status"] == (
            "open" if expected else "running"
        )


async def test_p1_bind_claim_records_the_declared_session(db, monkeypatch):
    """claim и pair-start сессией implementer записывают в неё объявленный session_id."""
    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-bind-rec")
    acting = await cp.get_acting_agent(db)
    await db.execute(
        "INSERT INTO chat_pair_sessions (principal_id, token_hash, expires_at, kind, "
        "bound_task_id, bound_generation) VALUES (?, ?, datetime('now', '+1 hour'), "
        "'implementer', ?, 1)",
        (int(acting["id"]), cp.hash_pair_code("tok-bind"), task_id),
    )
    await db.commit()
    await cp.bind_claim(db, "tok-bind", task_id, "nonce-x")
    rows = await db.execute_fetchall(
        "SELECT claimed_session FROM chat_pair_sessions WHERE bound_task_id = ?",
        (task_id,),
    )
    assert rows[0][0] == "nonce-x"


async def _phase_slot_booking_blocks_a_manual_claim(slot_world):
    """Невыкупленный заказ слоту — такая же бронь, как облачная: ручной claim отказан;
    после выкупа заказ держит сессия хоста, и задачу можно брать (прежнее поведение)."""
    task_id = await _task(slot_world, "бронь слота")
    assert (await _launch(slot_world, task_id)).status_code == 200
    refused = await slot_world.client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": "bot", "session_id": "s-manual"},
        headers=slot_world.agent_auth,
    )
    assert refused.status_code == 409, refused.text
    assert (await _row(slot_world.db, task_id))["status"] == "open"


async def test_the_write_lock_is_taken_before_the_booking_is_read(db, monkeypatch):
    """claim без session_id write-лок не берёт: проверка брони берёт его сама, иначе
    бронь, закоммиченная на втором соединении, прошла бы между чтением и переходом."""
    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-lock")
    await db.commit()
    assert not db.in_transaction
    await run_claim.refuse_over_live_run(db, task_id, None)
    assert db.in_transaction


async def test_p1_a_session_that_never_claimed_does_not_take_a_foreign_claim(
    db, monkeypatch
):
    """Сессия без claim (claimed_session NULL) истекает, а тот же принципал взял задачу
    своим ключом под другим session_id: реапер её не трогает."""
    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-never-claimed")
    await _hold(db, task_id, None, session=False, status="running")
    acting = await cp.get_acting_agent(db)
    await db.execute(
        "INSERT INTO chat_pair_sessions (principal_id, token_hash, expires_at, kind, "
        "bound_task_id, bound_generation, claimed_session) VALUES "
        "(?, 'nc', datetime('now', '-1 minute'), 'implementer', ?, 1, NULL)",
        (int(acting["id"]), task_id),
    )
    await db.commit()
    assert await cp.release_expired_implementer_tasks(db) == []
    assert (await _row(db, task_id))["claimed_by"]
    # и когда у захвата session_id не объявлен: NULL с NULL не совпадение
    await db.execute(
        "UPDATE tasks SET claim_session_id = NULL WHERE id = ?", (task_id,)
    )
    await db.commit()
    assert await cp.release_expired_implementer_tasks(db) == []


async def test_a_token_of_a_finished_run_cannot_take_the_task(client, db, monkeypatch):
    """Задержанный claim зомби-агента: прогон закончен, токен ещё жив — захват отказан,
    иначе он стал бы ручным и его никто не смог бы снять."""
    _launch_config(monkeypatch)
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(
        config, "HUB_TOKENS", {"tok-zombie": TokenIdentity("bot", "agent")}
    )
    human = await _human(db)
    _, task_id = await _task_with_findings(db, slug="rc-zombie")
    await repo.update_task(db, task_id, status="open")
    run = await _new_run(db, task_id, 2)
    code, _ = await cp.issue_run_code(
        db, task_id, 2, issued_by_principal_id=human, executor_run_id=run
    )
    redeemed = await client.post("/api/auth/chat-pair/redeem", json={"code": code})
    headers = {"Authorization": f"Bearer {redeemed.json()['token']}"}
    await db.execute(
        "UPDATE executor_runs SET outcome='failed', finished_at=datetime('now') "
        "WHERE id = ?",
        (run,),
    )
    await db.commit()
    for path, body in (
        ("claim", {"agent": config.CHAT_PAIR_AGENT, "session_id": "z"}),
        (
            "pair-start",
            {
                "assigned_agent": config.CHAT_PAIR_AGENT,
                "plan": "Plan: x",
                "session_id": "z",
            },
        ),
    ):
        resp = await client.post(
            f"/api/tasks/{task_id}/{path}", json=body, headers=headers
        )
        assert resp.status_code == 409, (path, resp.text)
    assert (await _row(db, task_id))["status"] == "open"


async def test_the_done_guard_reads_the_verdict_from_the_database(db, monkeypatch):
    """Снимок задачи вызывающего устарел (вердикт «на доработку» записан параллельно):
    guard читает вердикт и сдачу из базы, а не из подставленных APPROVAL_FIELDS."""
    from hub.services import lifecycle, resubmission_guard

    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-stale-snapshot")
    await repo.update_task(
        db,
        task_id,
        status="running",
        review_verdict="changes_requested",
        review_verdict_generation=1,
        submission_sha=SHA,
    )
    await db.commit()
    stale = {**await _row(db, task_id), "review_verdict": None}
    stale[lifecycle.PREPARED_CI_SHA] = SHA
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await resubmission_guard.stop_empty_done(db, stale)


async def test_the_1732_chain_end_to_end(
    client,
    db,
    monkeypatch,
    world,  # noqa: F811
):
    """my-home #1732: claim прогона → сдача → прогон снят → (сессия истекает, purge) →
    «на доработку» → кнопка видна сразу → повторный прогон → пустая пересдача отклонена."""
    _launch_config(monkeypatch)
    calls = _creator(monkeypatch, [_CREATED])
    human = await _human(db)
    _, task_id = await _task_with_findings(db, slug="rc-1732")
    await repo.update_task(db, task_id, status="open", submission_generation=0)
    run = await _new_run(db, task_id, 1)
    code, _ = await cp.issue_run_code(
        db, task_id, 1, issued_by_principal_id=human, executor_run_id=run
    )
    redeemed = await client.post("/api/auth/chat-pair/redeem", json={"code": code})
    headers = {"Authorization": f"Bearer {redeemed.json()['token']}"}
    for path, body in (
        ("claim", {"agent": config.CHAT_PAIR_AGENT, "session_id": "s-1732"}),
        (
            "pair-start",
            {
                "assigned_agent": config.CHAT_PAIR_AGENT,
                "session_id": "s-1732",
                "plan": "Plan: go",
                "git_mode": "remote",
            },
        ),
    ):
        resp = await client.post(
            f"/api/tasks/{task_id}/{path}", json=body, headers=headers
        )
        assert resp.status_code == 200, resp.text
    assert (await _row(db, task_id))["executor_run_id"] == run

    # сдача легла, провайдер снимает прогон
    await repo.update_task(
        db, task_id, status="review", submission_generation=1, submission_sha=SHA
    )
    await db.commit()
    row = dict(await repo.get_executor_run(db, run))  # type: ignore[arg-type]
    await ed._settle(
        db,
        {**row, "cancel_intent": ed.OUTCOME_TAKEN_DOWN},
        {"status": "CANCELLED"},
        1,
        1.0,
    )
    await db.commit()
    task = await _row(db, task_id)
    assert (task["status"], task["claimed_by"], task["executor_run_id"]) == (
        "review",
        None,
        None,
    )
    await cp.purge_expired(db)

    verdict = await _verdict(client, task_id, world.human_auth)
    assert verdict["status"] == "running"
    task = await _row(db, task_id)
    await db.execute(
        "INSERT OR REPLACE INTO submissions (task_id, generation, sha) VALUES (?, 1, ?)",
        (task_id, SHA),
    )
    await db.commit()
    assert await el.repair_offered(db, task), "кнопка видна сразу, не через TTL"
    assert (
        await el.repair_executor(db, task_id, issuer_principal_id=human, issuer="o")
    ).launched
    assert len(calls) == 1


async def test_a_revoked_run_session_cannot_take_the_task_as_a_manual_one(
    client, db, monkeypatch
):
    """Запрос прошёл авторизацию, а сессию прогона тем временем отозвали: claim отказан,
    а не записан как ручной (его никто не смог бы снять)."""
    from hub.services import run_claim as rc

    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-revoked-claim")
    run = await _new_run(db, task_id, 2)
    await _live_run_session(db, task_id, run, "rev-1")
    await db.execute(
        "UPDATE chat_pair_sessions SET revoked_at = datetime('now') WHERE token_hash = ?",
        ("rev-1",),
    )
    await db.commit()
    assert await cp.ended_run_session(db, "rev-1", task_id) is False  # хэш не токена
    await db.execute(
        "UPDATE chat_pair_sessions SET token_hash = ? WHERE token_hash = 'rev-1'",
        (cp.hash_pair_code("rev-token"),),
    )
    await db.commit()
    assert await cp.ended_run_session(db, "rev-token", task_id) is True
    # истёкшая (не отозванная) сессия живого прогона — тоже не ручная
    await db.execute(
        "UPDATE chat_pair_sessions SET revoked_at = NULL, "
        "expires_at = datetime('now', '-1 minute') WHERE token_hash = ?",
        (cp.hash_pair_code("rev-token"),),
    )
    await db.commit()
    assert await cp.ended_run_session(db, "rev-token", task_id) is True
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await rc.refuse_over_live_run(db, task_id, None, "rev-token")


async def test_a_slot_is_not_recorded_for_a_claim_already_released(db, monkeypatch):
    """record_capture по устаревшему снимку (захват прогона уже снят) слот не пишет."""
    from hub.services import executor_slots

    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-slot-race")
    run = await _new_run(db, task_id, 2)
    await _live_run_session(db, task_id, run, cp.hash_pair_code("slot-race"))
    await _hold(db, task_id, None, session=False, status="running")
    await db.commit()
    assert (
        await executor_slots.record_capture(db, task_id, cp.hash_pair_code("slot-race"))
        == ""
    )
    await repo.update_task(db, task_id, executor_run_id=run)
    await db.commit()
    assert (
        await executor_slots.record_capture(db, task_id, cp.hash_pair_code("slot-race"))
        != ""
    )


async def test_a_silent_usage_does_not_delay_releasing_the_claim(db, monkeypatch):
    """Провайдер вернул FINISHED, /usage молчит: опрос доходит до снятия захвата."""
    from hub.integrations import cursor_cloud

    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-usage-silent")
    run = await repo.create_executor_run(
        db,
        task_id=task_id,
        submission_generation=1,
        agent_id="a",
        run_id="r",
        model="m",
    )
    await _hold(db, task_id, run, session=True, live=True, status="review")
    await repo.update_task(db, task_id, submission_generation=1)
    await db.commit()

    async def _get_run(agent, run_id):
        return {"status": "FINISHED"}

    async def _get_usage(agent, run_id):
        return None

    monkeypatch.setattr(cursor_cloud, "get_run", _get_run)
    monkeypatch.setattr(cursor_cloud, "get_usage", _get_usage)
    await ed._poll_one(db, dict(await repo.get_executor_run(db, run)))  # type: ignore[arg-type]
    await db.commit()
    after = await _row(db, task_id)
    assert (after["claimed_by"], after["executor_run_id"]) == (None, None)


async def test_the_done_guard_looks_in_the_worktree_of_a_pair_task(
    db, monkeypatch, tmp_path
):
    """Pair-задача с worktree: недокоммиченное ищется там, куда смотрит git-хвост."""
    from hub.integrations.registry import plugins
    from hub.services import orchestration, resubmission_guard

    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-worktree")
    seen: list = []

    async def _dirty(repo=None):
        seen.append(repo)
        return ["a.py"]

    monkeypatch.setattr(orchestration, "worktree_per_task_enabled", lambda: True)
    monkeypatch.setattr(plugins.git_ops, "worktree_path", lambda tid, ws: str(tmp_path))
    monkeypatch.setattr(plugins.git_ops, "dirty_paths", _dirty)
    await repo.update_task(
        db,
        task_id,
        status="running",
        review_verdict="changes_requested",
        branch="task-x/work",
        auto_review=1,
    )
    await db.commit()
    facts = await resubmission_guard.collect_git_facts(db, await _row(db, task_id))
    assert facts == {resubmission_guard.UNCOMMITTED_KEY: True}
    assert seen == [str(tmp_path)]


# ---------------------------------------------------------------------------
# Круг 3 (CHANGES_REQUESTED на поколение 2)
# ---------------------------------------------------------------------------


async def test_c3_p1_migration_gives_old_sessions_no_claim_they_did_not_make(
    db, monkeypatch
):
    """P1-1: старые сессии implementer остаются с NULL: нынешний захват задачи
    миграция им не приписывает, и истёкшая сессия не снимает новый ручной claim."""
    _launch_config(monkeypatch)
    assert "backfill_chat_pair_sessions_claimed_session" not in dict(
        db_module._MIGRATIONS
    )
    _, task_id = await _task_with_findings(db, slug="rc-c3-legacy")
    await _hold(db, task_id, None, session=False, status="running")
    acting = await cp.get_acting_agent(db)
    await db.execute(
        "INSERT INTO chat_pair_sessions (principal_id, token_hash, expires_at, kind, "
        "bound_task_id, bound_generation) VALUES "
        "(?, 'c3-old', datetime('now', '-1 minute'), 'implementer', ?, 1)",
        (int(acting["id"]), task_id),
    )
    await db.commit()
    assert await cp.release_expired_implementer_tasks(db) == []
    assert (await _row(db, task_id))["claimed_by"]


async def test_c3_p1_every_terminal_status_releases_the_claim_without_usage(
    db, monkeypatch
):
    """P1-2: ERROR, EXPIRED, CANCELLED и FINISHED без сдачи при недоступном /usage:
    захват снят сразу, строка прогона не висит вечно (закрывается по сроку цены)."""
    from hub.integrations import cursor_cloud

    _launch_config(monkeypatch)

    async def _usage(agent, run_id):
        return None

    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)
    for status in ("ERROR", "EXPIRED", "CANCELLED", "FINISHED"):
        _, task_id = await _task_with_findings(db, slug=f"rc-c3-{status.lower()}")
        run = await repo.create_executor_run(
            db,
            task_id=task_id,
            submission_generation=2,
            agent_id="a",
            run_id="r",
            model="m",
        )
        await repo.update_task(db, task_id, status="running", submission_generation=1)
        await _hold(db, task_id, run, session=True, live=True, status="running")

        async def _get_run(agent, run_id, _s=status):
            return {"status": _s}

        monkeypatch.setattr(cursor_cloud, "get_run", _get_run)
        await ed._poll_one(db, dict(await repo.get_executor_run(db, run)))  # type: ignore[arg-type]
        await db.commit()
        after = await _row(db, task_id)
        assert (after["claimed_by"], after["executor_run_id"]) == (None, None), status
        # конец сказан провайдером: строка ждёт только цену и повторному запуску не мешает
        assert await el._live_run(db, task_id) == "", status
        # срок ожидания цены вышел: строка закрывается исходом провайдера
        await db.execute(
            "UPDATE executor_runs SET cost_wait_since = datetime('now', '-3 hours') "
            "WHERE id = ?",
            (run,),
        )
        await db.commit()
        await ed._poll_one(db, dict(await repo.get_executor_run(db, run)))  # type: ignore[arg-type]
        await db.commit()
        assert dict(await repo.get_executor_run(db, run))["outcome"] != "running", (
            status
        )  # type: ignore[arg-type]


async def test_c3_a_foreign_done_does_not_clear_a_claim(db, monkeypatch):
    """done от агента, который задачу не держит, захват (и его прогон) не снимает."""
    from hub import models, services

    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-c3-foreign-done")
    run = await _new_run(db, task_id, 2)
    await _hold(db, task_id, run, session=True, live=True, status="claimed")
    await db.execute("UPDATE tasks SET branch = '' WHERE id = ?", (task_id,))
    await db.commit()
    await services.add_update(
        db,
        task_id,
        models.TaskUpdateCreate(agent="stranger", kind="done", content="чужой отчёт"),
    )
    held = await _row(db, task_id)
    assert (held["claimed_by"], held["executor_run_id"]) == ("cloud", run)
    live = await db.execute_fetchall(
        "SELECT 1 FROM chat_pair_sessions WHERE executor_run_id = ? AND revoked_at IS NULL",
        (run,),
    )
    assert live, "сессии чужого прогона не отозваны"


async def test_c3_the_stale_claim_sweep_leaves_a_retaken_claim(db, monkeypatch):
    """Sweep аренды выбрал захват, а его успели отпустить и взять заново: новый не снимается."""
    from hub import poller

    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-c3-sweep")
    await repo.update_task(db, task_id, status="claimed")
    await db.execute(
        "UPDATE tasks SET claimed_by='new', claimed_at='2026-10-17T11:00:00+00:00' "
        "WHERE id = ?",
        (task_id,),
    )
    await db.commit()
    stale = dict(await _row(db, task_id)) | {"claimed_at": "2026-10-17T08:00:00+00:00"}

    async def _stale_list(_db, _minutes):
        return [stale]

    monkeypatch.setattr(repo, "list_expired_claims", _stale_list)
    await poller._sweep_expired_claims(db)
    after = await _row(db, task_id)
    assert (after["status"], after["claimed_by"]) == ("claimed", "new")


async def test_c3_release_task_takes_the_lock_before_it_looks_at_the_holder(
    db, monkeypatch
):
    """release_task берёт write-лок ДО проверки держателя."""
    from hub import models
    from hub.services import lifecycle

    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-c3-release")
    await repo.update_task(db, task_id, status="claimed", claimed_by="alice")
    await db.commit()
    seen: list[bool] = []
    real = repo.get_task

    async def _spy(conn, tid):
        seen.append(conn.in_transaction)
        return await real(conn, tid)

    monkeypatch.setattr(lifecycle.repo, "get_task", _spy)
    await lifecycle.release_task(db, task_id, models.TaskRelease(agent="alice"))
    assert seen and seen[0] is True


async def test_c3_a_stuck_claim_in_needs_decision_is_released_too(db, monkeypatch):
    """Прогон закончился ошибкой, снятие при сдаче отказало, задача ушла в needs_decision:
    второй проход снимает захват и там (доказательство есть, признаков работы нет)."""
    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-c3-nd")
    run = await _finished_run_id(db, task_id)
    await _hold(db, task_id, run, session=True, status="needs_decision")
    await cp.purge_expired(db)
    freed = await _row(db, task_id)
    assert (freed["status"], freed["claimed_by"]) == ("needs_decision", None)


async def _phase_slot_second_executor_is_refused_both_ways(slot_world):
    """k8: заказ слоту на ту же задачу и второй исполнитель (#1783).

    (а) пока заказ не выкуплен И после выкупа, пока сессия хоста не взяла задачу,
    ручные claim и pair-start отказаны, а своя сессия задачу берёт;
    (б) выкуп кода отказан, если у задачи уже есть другой держатель или живая
    сессия другого исполнителя: сессия не создаётся, заказ остаётся выкупаемым."""
    w = slot_world

    async def _free_host() -> None:
        """Хост занят чужим заказом из соседней фазы: закрыть его, как закрыл бы sweep."""
        await w.db.execute(
            "UPDATE executor_orders SET state = 'closed', close_reason = 'test' "
            "WHERE state <> 'closed'"
        )
        await w.db.execute(
            "UPDATE executor_runs SET outcome = 'failed', finished_at = datetime('now') "
            "WHERE outcome = 'running'"
        )
        await w.db.commit()

    await _free_host()
    # (а) заказ не выкуплен, затем выкуплен, но сессия ещё не брала
    task_id = await _task(w, "k8: порядок 1")
    assert (await _launch(w, task_id)).status_code == 200
    order = (await _orders(w)).json()["orders"][0]

    async def _manual(path: str, tid: int):
        body = {"agent": "bot", "session_id": "s-other"}
        if path == "pair-start":
            body |= {"assigned_agent": "bot", "plan": "Plan: x", "git_mode": "remote"}
        return await w.client.post(
            f"/api/tasks/{tid}/{path}", json=body, headers=w.agent_auth
        )

    for path in ("claim", "pair-start"):
        assert (await _manual(path, task_id)).status_code == 409, path
    redeemed = await _redeem(w, _code_of(w, order))
    assert redeemed.status_code == 200, redeemed.text
    session = {"Authorization": f"Bearer {redeemed.json()['token']}"}
    for path in ("claim", "pair-start"):
        assert (await _manual(path, task_id)).status_code == 409, (
            f"after redeem {path}"
        )  # прошёл облачный срок брони без агента (у слотового прогона агента нет вовсе):
    # защита живого заказа от этого не слабеет
    await w.db.execute(
        "UPDATE executor_runs SET started_at = datetime('now', '-3 hours') "
        "WHERE task_id = ?",
        (task_id,),
    )
    await w.db.commit()
    for path in ("claim", "pair-start"):
        assert (await _manual(path, task_id)).status_code == 409, f"stale {path}"
    # ручной implementer-код поверх УЖЕ выкупленного заказа слоту — тоже отказан
    late_code = await w.client.post(
        "/api/auth/chat-pair/start",
        json={"kind": "implementer", "task_id": task_id},
        headers={**w.human_auth, "x-forwarded-for": "203.0.113.56"},
    )
    assert late_code.status_code == 200, late_code.text
    assert (await _redeem(w, late_code.json()["code"])).status_code >= 400
    await _claim_and_start(w, task_id, session)  # своя сессия берёт
    assert (await _row(w.db, task_id))["status"] == "running"

    # (б) выкуп отказан, когда задачу уже держит другой
    await _free_host()
    held = await _task(w, "k8: порядок 2")
    assert (await _launch(w, held)).status_code == 200
    await repo.update_task(
        w.db, held, status="claimed", claimed_by="alice", claim_session_id="s-alice"
    )
    await w.db.commit()
    pending = [o for o in (await _orders(w)).json()["orders"] if o["task_id"] == held]
    run_of_held = (
        await w.db.execute_fetchall(
            "SELECT id FROM executor_runs WHERE task_id = ? ORDER BY id DESC", (held,)
        )
    )[0][0]
    conflict = await run_claim.redeem_conflict(
        w.db, {"executor_run_id": run_of_held, "bound_task_id": held}
    )
    assert "держит" in conflict, "держатель другого захвата назван причиной"
    # свой же захват этого прогона выкуп не блокирует
    await repo.update_task(w.db, held, executor_run_id=run_of_held)
    await w.db.commit()
    assert not await run_claim.redeem_conflict(
        w.db, {"executor_run_id": run_of_held, "bound_task_id": held}
    )
    await repo.update_task(w.db, held, executor_run_id=None)
    await w.db.commit()
    blocked = await _redeem(w, _code_of(w, pending[0]))
    assert blocked.status_code >= 400, blocked.text
    assert not await w.db.execute_fetchall(
        "SELECT 1 FROM chat_pair_sessions WHERE bound_task_id = ?", (held,)
    )
    state = await w.db.execute_fetchall(
        "SELECT state FROM executor_orders WHERE task_id = ?", (held,)
    )
    assert state[0][0] in ("pending", "delivered")
    # живая сессия чужого исполнителя — то же
    await _free_host()
    third = await _task(w, "k8: порядок 3")
    assert (await _launch(w, third)).status_code == 200
    acting = await cp.get_acting_agent(w.db)
    await w.db.execute(
        "INSERT INTO chat_pair_sessions (principal_id, token_hash, expires_at, kind, "
        "bound_task_id, bound_generation) VALUES "
        "(?, 'k8-foreign', datetime('now', '+1 hour'), 'implementer', ?, 1)",
        (int(acting["id"]), third),
    )
    await w.db.commit()
    order3 = [o for o in (await _orders(w)).json()["orders"] if o["task_id"] == third]
    assert (await _redeem(w, _code_of(w, order3[0]))).status_code >= 400

    # ручной implementer-код поверх выданного заказа слоту: выкупает первый, второй отказан
    await _free_host()
    fourth = await _task(w, "k8: порядок 4")
    assert (await _launch(w, fourth)).status_code == 200
    manual_code = await w.client.post(
        "/api/auth/chat-pair/start",
        json={"kind": "implementer", "task_id": fourth},
        headers={**w.human_auth, "x-forwarded-for": "203.0.113.55"},
    )
    assert manual_code.status_code == 200, manual_code.text
    manual = await _redeem(w, manual_code.json()["code"])
    assert manual.status_code >= 400, "ручной код не встаёт поверх заказа слоту"
    order4 = [o for o in (await _orders(w)).json()["orders"] if o["task_id"] == fourth]
    assert (await _redeem(w, _code_of(w, order4[0]))).status_code == 200

    # обратный порядок: ручная сессия выкуплена раньше — заказ слоту не выдаётся
    await _free_host()
    fifth = await _task(w, "k8: порядок 5")
    manual5 = await w.client.post(
        "/api/auth/chat-pair/start",
        json={"kind": "implementer", "task_id": fifth},
        headers={**w.human_auth, "x-forwarded-for": "203.0.113.57"},
    )
    assert (await _redeem(w, manual5.json()["code"])).status_code == 200
    assert (await _launch(w, fifth)).status_code == 409


async def test_k8_two_connections_redeem_and_claim_leave_one_executor(
    world,  # noqa: F811
    db_dsn,
):
    """Две стороны гонки: выкуп кода слота и ручной claim. Что бы ни пришло первым,
    на задаче ровно один исполнитель: claim при невыкупленном или ещё не бравшем
    заказе отказан."""
    w = world
    task_id = await _task(w, "k8: гонка")
    assert (await _launch(w, task_id)).status_code == 200
    order = (await _orders(w)).json()["orders"][0]
    code = _code_of(w, order)

    async def _claim():
        return await w.client.post(
            f"/api/tasks/{task_id}/claim",
            json={"agent": "bot", "session_id": "s-race"},
            headers=w.agent_auth,
        )

    claim, redeem = await asyncio.gather(_claim(), _redeem(w, code))
    assert claim.status_code == 409, claim.text
    assert redeem.status_code == 200, redeem.text
    assert (await _row(w.db, task_id))["status"] == "open"
    assert (await _row(w.db, task_id))["claimed_by"] is None


async def test_c3_repair_does_not_evict_a_live_manual_executor_from_review(
    db, monkeypatch
):
    """Кнопка повторного прогона из review не вытесняет ручного исполнителя с ЖИВОЙ
    сессией; без живой сессии возврат идёт как на develop."""
    _launch_config(monkeypatch)
    calls = _creator(monkeypatch, [_CREATED])
    human = await _human(db)
    _, task_id = await _task_with_findings(db, slug="rc-c3-manual-review")
    await repo.update_task(db, task_id, claimed_by="alice", claim_session_id="s-a")
    await _live_run_session(db, task_id, 0, "alive-manual")
    await db.execute(
        "UPDATE chat_pair_sessions SET executor_run_id = NULL WHERE token_hash = ?",
        ("alive-manual",),
    )
    await db.commit()
    result = await el.repair_executor(
        db, task_id, issuer_principal_id=human, issuer="o"
    )
    assert not result.launched and "alice" in result.reason
    assert calls == []
    assert (await _row(db, task_id))["claimed_by"] == "alice"
    # сессия истекла: как на develop, задача возвращается и заказ уходит
    await db.execute(
        "UPDATE chat_pair_sessions SET expires_at = datetime('now', '-1 minute')"
    )
    await db.commit()
    again = await el.repair_executor(db, task_id, issuer_principal_id=human, issuer="o")
    assert again.launched, again
    assert len(calls) == 1


async def test_c4_recorded_outcomes_make_the_same_sha_a_real_resubmission(
    db, monkeypatch
):
    """Исходы находок записаны ОТДЕЛЬНО (false_positive по всем) — тот же коммит дальше
    не «пустая пересдача», хотя в самом запросе исходов нет."""
    from hub.services import resubmission_guard as rg

    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-c4-outcomes")
    task = await _row(db, task_id)
    assert not await rg.findings_answered(db, task)
    report = (await repo.machine_reviews_of_generation(db, task_id, 1))[-1]
    from hub.models import MachineReviewView

    view = MachineReviewView(**dict(report))
    for finding in [*view.findings_confirmed, *view.unresolved]:
        kind = "confirmed" if finding in view.findings_confirmed else "unresolved"
        await db.execute(
            "INSERT INTO finding_outcomes (review_id, task_id, submission_generation, "
            "finding_uid, finding_index, outcome, note, reported_by) "
            "VALUES (?, ?, 1, ?, -1, ?, 'n', 'dev')",
            (
                dict(report)["id"],
                task_id,
                finding.finding_uid,
                "false_positive" if kind == "confirmed" else "not_a_defect",
            ),
        )
    await db.commit()
    assert await rg.findings_answered(db, await _row(db, task_id))

    # и сам шаг submit с пустым телом уже не отказывает (раньше — 422)
    from hub import models
    from hub.services import lifecycle

    await repo.update_task(
        db,
        task_id,
        status="running",
        review_verdict="changes_requested",
        review_verdict_generation=1,
        submission_sha=SHA,
    )
    await db.commit()
    state = lifecycle.SubmitContext(
        db=db,
        task_id=task_id,
        task=await _row(db, task_id),
        body=models.TaskSubmitReview(agent="dev"),
    )
    state.submission_sha = SHA
    await lifecycle._step_empty_resubmission(state)


async def test_c3_a_run_that_claimed_and_died_returns_the_task_to_open(db, monkeypatch):
    """Прогон взял задачу (claimed), pair-start не успел и закончился ошибкой: захват снят
    и задача в open — не висит claimed без держателя и без срока аренды."""
    from hub.integrations import cursor_cloud

    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-c3-claimed-dead")
    run = await _new_run(db, task_id, 1)
    await _hold(db, task_id, run, session=True, live=True, status="claimed")
    await repo.update_task(db, task_id, submission_generation=0)
    await db.commit()

    async def _get_run(agent, run_id):
        return {"status": "ERROR"}

    async def _usage(agent, run_id):
        return None

    monkeypatch.setattr(cursor_cloud, "get_run", _get_run)
    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)
    await ed._poll_one(db, dict(await repo.get_executor_run(db, run)))  # type: ignore[arg-type]
    await db.commit()
    after = await _row(db, task_id)
    assert (after["status"], after["claimed_by"], after["executor_run_id"]) == (
        "open",
        None,
        None,
    )


async def test_c3_finished_without_submission_keeps_its_outcome_when_usage_is_silent(
    db, monkeypatch
):
    """/usage молчит, FINISHED без сдачи: по выходу срока цены строка закрывается тем же
    _settle — отдельным исходом «закончил без сдачи», а не обычным finished."""
    from hub.integrations import cursor_cloud

    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-c3-no-submission")
    run = await repo.create_executor_run(
        db,
        task_id=task_id,
        submission_generation=2,
        agent_id="a",
        run_id="r",
        model="m",
    )
    await db.commit()

    async def _get_run(agent, run_id):
        return {"status": "FINISHED"}

    async def _usage(agent, run_id):
        return None

    monkeypatch.setattr(cursor_cloud, "get_run", _get_run)
    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)
    await ed._poll_one(db, dict(await repo.get_executor_run(db, run)))  # type: ignore[arg-type]
    await db.execute(
        "UPDATE executor_runs SET cost_wait_since = datetime('now', '-3 hours') "
        "WHERE id = ?",
        (run,),
    )
    await db.commit()
    await ed._poll_one(db, dict(await repo.get_executor_run(db, run)))  # type: ignore[arg-type]
    await db.commit()
    assert (
        dict(await repo.get_executor_run(db, run))["outcome"]  # type: ignore[arg-type]
        == ed.OUTCOME_FINISHED_WITHOUT_SUBMISSION
    )


async def test_c3_a_claim_held_after_done_is_released_in_ci_check(db, monkeypatch):
    """Доказанный держатель сдал через done (ci_check), прогон кончился: захват снят и там."""
    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-c3-ci-check")
    run = await _finished_run_id(db, task_id)
    await _hold(db, task_id, run, session=False, status="ci_check")
    await cp.purge_expired(db)
    after = await _row(db, task_id)
    assert (after["status"], after["claimed_by"]) == ("ci_check", None)


async def test_c3_a_run_that_providers_called_over_cannot_take_the_task(
    db, monkeypatch
):
    """Провайдер сказал «конец» (строка ждёт цену, cost_wait_since): её сессия задачу уже
    не берёт — ни как прогон (run_of_session), ни как ручную (ended_run_session)."""
    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-c3-over")
    run = await _new_run(db, task_id, 2)
    await _live_run_session(db, task_id, run, cp.hash_pair_code("over-token"))
    await db.commit()
    assert await cp.run_of_session(db, "over-token", task_id) == run
    assert await cp.ended_run_session(db, "over-token", task_id) is False
    await db.execute(
        "UPDATE executor_runs SET cost_wait_since = datetime('now') WHERE id = ?",
        (run,),
    )
    await db.commit()
    assert await cp.run_of_session(db, "over-token", task_id) is None
    assert await cp.ended_run_session(db, "over-token", task_id) is True


async def test_c4_a_manual_session_claim_is_cleared_by_its_done(
    client,
    db,
    monkeypatch,
    world,  # noqa: F811
):
    """Регрессия круга 3: ручной implementer-код (выдан человеком, без прогона). claim ->
    done -> сессия истекла и purge -> changes_requested: захват снят сдачей, как на
    develop, тупика running + claimed_by + 0 сессий нет."""
    from hub import models, services

    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-c4-manual")
    await repo.update_task(db, task_id, status="open", branch="task-x/work")
    await db.commit()
    issued = await client.post(
        "/api/auth/chat-pair/start",
        json={"kind": "implementer", "task_id": task_id},
        headers={**world.human_auth, "x-forwarded-for": "203.0.113.70"},
    )
    assert issued.status_code == 200, issued.text
    redeemed = await _redeem(world, issued.json()["code"])
    assert redeemed.status_code == 200, redeemed.text
    headers = {"Authorization": f"Bearer {redeemed.json()['token']}"}
    claimed = await client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": config.CHAT_PAIR_AGENT, "session_id": "s-manual-c4"},
        headers=headers,
    )
    assert claimed.status_code == 200, claimed.text
    assert (await _row(db, task_id))["executor_run_id"] is None
    await services.add_update(
        db,
        task_id,
        models.TaskUpdateCreate(
            agent=config.CHAT_PAIR_AGENT, kind="done", content="сдано"
        ),
    )
    after = await _row(db, task_id)
    assert after["claimed_by"] is None and after["claim_session_id"] is None
    await db.execute(
        "UPDATE chat_pair_sessions SET expires_at = datetime('now', '-1 minute')"
    )
    await db.commit()
    await cp.purge_expired(db)
    assert not await db.execute_fetchall(
        "SELECT 1 FROM chat_pair_sessions WHERE bound_task_id = ?", (task_id,)
    )
    final = await _row(db, task_id)
    assert final["claimed_by"] is None, "после purge захвата нет: тупика нет"


async def _manual_token(client, world_, task_id: int, ip: str) -> dict:
    issued = await client.post(
        "/api/auth/chat-pair/start",
        json={"kind": "implementer", "task_id": task_id},
        headers={**world_.human_auth, "x-forwarded-for": ip},
    )
    assert issued.status_code == 200, issued.text
    redeemed = await _redeem(world_, issued.json()["code"])
    assert redeemed.status_code == 200, redeemed.text
    return {"Authorization": f"Bearer {redeemed.json()['token']}"}


async def test_c4_a_changed_token_adopts_the_claim_and_is_not_a_dead_end(
    client,
    db,
    monkeypatch,
    world,  # noqa: F811
):
    """Регрессия-проверка: захват идёт под тем же agent/session_id, но другим токеном (два
    ручных кода; либо claim токеном среды и pair-start токеном сессии). Сессия, продолжившая
    захват, принимает его метку, и после истечения и purge задача возвращается в open."""
    _launch_config(monkeypatch)
    agent = config.CHAT_PAIR_AGENT
    # (1) два ручных кода одного человека, claim через A, повтор через B, pair-start через B
    _, first = await _task_with_findings(db, slug="rc-c4-two-tokens")
    await repo.update_task(db, first, status="open", branch="task-x/work")
    await db.commit()
    a = await _manual_token(client, world, first, "203.0.113.81")
    b = await _manual_token(client, world, first, "203.0.113.82")
    body = {"agent": agent, "session_id": "s-sw"}
    assert (
        await client.post(f"/api/tasks/{first}/claim", json=body, headers=a)
    ).status_code == 200
    assert (
        await client.post(f"/api/tasks/{first}/claim", json=body, headers=b)
    ).status_code == 200
    started = await client.post(
        f"/api/tasks/{first}/pair-start",
        json={
            "assigned_agent": agent,
            "session_id": "s-sw",
            "plan": "Plan: x",
            "git_mode": "remote",
        },
        headers=b,
    )
    assert started.status_code == 200, started.text
    # (2) claim токеном среды, pair-start токеном сессии
    _, second = await _task_with_findings(db, slug="rc-c4-env-then-session")
    await repo.update_task(db, second, status="open", branch="task-y/work")
    await db.commit()
    s2 = await _manual_token(client, world, second, "203.0.113.83")
    env_body = {"agent": agent, "session_id": "s-env"}
    assert (
        await client.post(
            f"/api/tasks/{second}/claim", json=env_body, headers=world.agent_auth
        )
    ).status_code == 200
    started2 = await client.post(
        f"/api/tasks/{second}/pair-start",
        json={
            "assigned_agent": agent,
            "session_id": "s-env",
            "plan": "Plan: y",
            "git_mode": "remote",
        },
        headers=s2,
    )
    assert started2.status_code == 200, started2.text
    # (3) повторный claim другим токеном без pair-start: метку принимает сам claim
    _, third = await _task_with_findings(db, slug="rc-c4-claim-only")
    await repo.update_task(db, third, status="open", branch="task-z/work")
    await db.commit()
    a3 = await _manual_token(client, world, third, "203.0.113.84")
    b3 = await _manual_token(client, world, third, "203.0.113.85")
    body3 = {"agent": agent, "session_id": "s-sw3"}
    for headers in (a3, b3):
        resp = await client.post(
            f"/api/tasks/{third}/claim", json=body3, headers=headers
        )
        assert resp.status_code == 200, resp.text
    # сессия A давно удалена purge, жива только B
    await db.execute(
        "DELETE FROM chat_pair_sessions WHERE bound_task_id = ? AND token_hash = "
        "(SELECT token_hash FROM chat_pair_sessions WHERE bound_task_id = ? "
        "ORDER BY id LIMIT 1)",
        (third, third),
    )
    await db.execute(
        "UPDATE chat_pair_sessions SET expires_at = datetime('now', '-1 minute')"
    )
    await db.commit()
    await cp.purge_expired(db)
    await cp.purge_expired(db)
    for task_id in (first, second, third):
        after = await _row(db, task_id)
        assert after["status"] == "open" and after["claimed_by"] is None, task_id


async def test_c4_a_late_cost_closing_of_an_old_run_does_not_stop_new_work(
    db, monkeypatch
):
    """Старый прогон снят провайдером без цены, захват снят, задачу взяли заново; запоздалое
    закрытие учёта (finished_at позже входа задачи в running) новую работу в needs_decision
    не переводит: время конца — по cost_wait_since."""
    from hub.services import executor_dispatch as dispatch

    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-c4-late-close")
    run = await _new_run(db, task_id, 2)
    await repo.update_task(db, task_id, status="running", submission_generation=1)
    await db.execute(
        "UPDATE tasks SET status_entered_at = datetime('now') WHERE id = ?", (task_id,)
    )
    await db.execute(
        "UPDATE executor_runs SET outcome='cancelled', finished_at=datetime('now'), "
        "cost_wait_since=datetime('now', '-30 minutes') WHERE id = ?",
        (run,),
    )
    await db.commit()
    assert not await repo.stopped_executor_runs_of_running_tasks(
        db, dispatch.STOPPED_OUTCOMES
    )
    await db.execute(
        "UPDATE executor_runs SET cost_wait_since = NULL WHERE id = ?", (run,)
    )
    await db.commit()
    assert await repo.stopped_executor_runs_of_running_tasks(
        db, dispatch.STOPPED_OUTCOMES
    )


async def test_c4_only_a_claim_made_through_a_session_is_given_back(db, monkeypatch):
    """Метка claim_nonce есть только у захвата, сделанного через implementer-сессию.
    Тот же агент и тот же session_id, но обычным токеном (метки нет), истёкшая сессия
    первого захвата не снимает; захват сессии без единой живой сессии — снимается и
    после purge (задача-кандидат, а не сессия)."""
    from hub import models
    from hub.services import lifecycle

    _launch_config(monkeypatch)
    acting = await cp.get_acting_agent(db)
    agent, sid = config.CHAT_PAIR_AGENT, "s-same"
    _, retaken = await _task_with_findings(db, slug="rc-c4-api-retake")
    await repo.update_task(db, retaken, status="open")
    await db.commit()
    await lifecycle.claim_task(
        db, retaken, models.TaskClaim(agent=agent, session_id=sid)
    )
    assert (await _row(db, retaken))["claim_nonce"] is None, "обычный токен — без метки"
    await db.execute(
        "INSERT INTO chat_pair_sessions (principal_id, token_hash, expires_at, kind, "
        "bound_task_id, bound_generation) VALUES "
        "(?, 'c4-old', datetime('now', '-1 minute'), 'implementer', ?, 1)",
        (int(acting["id"]), retaken),
    )
    await db.commit()
    assert await cp.release_expired_implementer_tasks(db) == []
    assert (await _row(db, retaken))["status"] == "claimed"

    # захват сессии: сессии purge удалил раньше, чем проход его освободил
    _, orphan = await _task_with_findings(db, slug="rc-c4-orphan")
    await _hold(db, orphan, None, session=False, status="running")
    await db.execute("UPDATE tasks SET claim_nonce = 'nz' WHERE id = ?", (orphan,))
    await db.commit()
    assert await cp.release_expired_implementer_tasks(db) == [orphan]
    assert (await _row(db, orphan))["status"] == "open"


async def test_c4_a_dead_cloud_runs_session_does_not_block_a_slot_order(
    world,  # noqa: F811
):
    """Облачный агент выкупил код и умер до claim: прогон закрыт, сессия не отозвана. Заказ
    слоту такой задачи не блокируется её сессией (прогон кончен)."""
    w = world
    task_id = await _task(w, "k8: умерший облачный")
    run = await repo.create_executor_run(
        w.db,
        task_id=task_id,
        submission_generation=1,
        agent_id="a",
        run_id="r",
        model="m",
    )
    acting = await cp.get_acting_agent(w.db)
    await w.db.execute(
        "INSERT INTO chat_pair_sessions (principal_id, token_hash, expires_at, kind, "
        "bound_task_id, bound_generation, executor_run_id) VALUES "
        "(?, 'dead-cloud', datetime('now', '+1 hour'), 'implementer', ?, 1, ?)",
        (int(acting["id"]), task_id, run),
    )
    await w.db.commit()
    assert (await _launch(w, task_id)).status_code == 409  # прогон ещё идёт
    await w.db.execute(
        "UPDATE executor_runs SET outcome='failed', finished_at=datetime('now') "
        "WHERE id = ?",
        (run,),
    )
    await w.db.commit()
    assert (await _launch(w, task_id)).status_code == 200


async def test_c4_a_session_claim_orphaned_in_review_is_given_back_after_the_verdict(
    db, monkeypatch
):
    """Ручной код: claim -> pair-start -> review -> сессия истекла и purge -> changes_requested.
    Захват с меткой сессии без единой живой сессии снимается проходом по задачам."""
    _launch_config(monkeypatch)
    _, task_id = await _task_with_findings(db, slug="rc-c4-review-orphan")
    await _hold(db, task_id, None, session=False, status="review")
    await db.execute("UPDATE tasks SET claim_nonce = 'nr' WHERE id = ?", (task_id,))
    await db.commit()
    await cp.purge_expired(db)  # review: проход его не трогает
    assert (await _row(db, task_id))["status"] == "review"
    await repo.update_task(db, task_id, status="running")
    await db.commit()
    await cp.purge_expired(db)
    after = await _row(db, task_id)
    assert (after["status"], after["claimed_by"]) == ("open", None)
