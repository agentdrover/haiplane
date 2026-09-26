"""Мерж гейта прошёл — сбой записи после него не делает его недоставкой (#1428).

26.09.2026 02:17–02:18 на проде два done-отчёта ушли почти одновременно.
Done-flow держит ``write_transaction`` (BEGIN IMMEDIATE) на всё время своего
git-хвоста, включая мерж форжем. Поллер в это окно влил PR #501 (#1411), а
строку ``pipeline_merges`` записать не смог: busy_timeout 5 с истёк, SQLite
ответил «database is locked», и ``except`` шага гейта превратил это в
``merge_gate_error``. Задача с влитым кодом ушла к человеку, строка реестра
потерялась.

Здесь всё на ФАЙЛОВОЙ базе и на соединениях ``hub.db.connect`` — тех же
режимов, что на проде (IMMEDIATE, WAL, busy_timeout). Держатель лока —
отдельное соединение, как чужой done-flow.
"""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from hub import db as db_module
from hub import repository as repo
from hub import services
from hub.integrations.noop import NoopDispatch, NoopGitOps
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
from hub.integrations.registry import plugins
from hub.models import TaskCreate, TaskReviewVerdict
from hub.services import orchestration

MERGE_SHA = "gate0merge0sha"


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch):
    # Прод ждёт секунды; тесту хватит долей — механизм тот же.
    monkeypatch.setattr(db_module, "BUSY_TIMEOUT_MS", 100)
    monkeypatch.setattr(orchestration, "GATE_RECORD_WAIT_SECONDS", 3.0)
    monkeypatch.setattr(orchestration, "GATE_RECORD_RETRY_PAUSE_SECONDS", 0.05)
    yield


async def _approved_pair_task(db: aiosqlite.Connection, *, pr_number: int = 501) -> int:
    tv = await services.create_task(db, TaskCreate(title="Deliver me"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: build")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")
    await repo.update_task(db, tv.id, pr_number=pr_number)
    await db.commit()
    await services.submit_for_review(db, tv.id)
    await services.record_review_verdict(
        db, tv.id, TaskReviewVerdict(verdict="approved", agent="reviewer")
    )
    return tv.id


def _git(merge_pr=None) -> NoopGitOps:
    g = NoopGitOps()
    g.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.passed, "checks_passed")
    )
    g.merge_pr = merge_pr or AsyncMock(return_value=True)
    g.merge_commit_sha = AsyncMock(return_value=MERGE_SHA)
    g.pull_main = AsyncMock(return_value=True)
    g.delete_branch = AsyncMock(return_value=True)
    plugins.git_ops = g
    plugins.dispatch = NoopDispatch()
    return g


async def _rows(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    return [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT pr_number, merge_sha FROM pipeline_merges WHERE task_id = ?",
            (task_id,),
        )
    ]


async def test_a_second_delivery_survives_a_merge_held_under_the_write_lock(db, db_dsn):
    """AC-1: чужая транзакция держит лок, пока форж вливает наш PR."""
    task_id = await _approved_pair_task(db)
    holder = await db_module.connect(db_dsn)
    victim = await db_module.connect(db_dsn)
    released = asyncio.Event()

    async def _release_later() -> None:
        # Чужой done-flow дольше busy_timeout занят своим мержем форжем.
        await asyncio.sleep(0.6)
        await holder.commit()
        released.set()

    lock_taken: list[bool] = []

    async def _merge_while_someone_holds_the_lock(*_a, **_kw) -> bool:
        # Лок чужой транзакции берётся, пока идёт «сеть». Если его держит
        # сама жертва (запись до мержа, #1428), взять его нельзя — и это
        # тоже провал теста, а не повод ждать.
        try:
            await holder.execute("BEGIN IMMEDIATE")
            await holder.execute(
                "UPDATE tasks SET updated_at = updated_at WHERE id = ?", (task_id,)
            )
        except sqlite3.OperationalError:
            lock_taken.append(False)
            released.set()
            return True
        lock_taken.append(True)
        asyncio.get_running_loop().create_task(_release_later())
        return True

    g = _git(AsyncMock(side_effect=_merge_while_someone_holds_the_lock))
    try:
        task = dict(await repo.get_task(victim, task_id))
        ok, detail = await services.merge_before_completion(victim, task)
        await asyncio.wait_for(released.wait(), 5)
    finally:
        # Закрыть оба всегда: незакрытое соединение aiosqlite держит поток,
        # и процесс pytest после падения не завершается.
        await holder.close()
        await victim.close()

    assert lock_taken == [True], "жертва держала write-лок во время мержа форжем"
    assert (ok, detail) == (True, MERGE_SHA), detail
    assert g.merge_pr.await_count == 1
    assert await _rows(db, task_id) == [{"pr_number": 501, "merge_sha": MERGE_SHA}]


def _locked_record(monkeypatch, *, failures: int) -> dict:
    """``record_pipeline_merge``, что падает на локе ``failures`` раз.

    Сверка коммита подменена на «сверено»: иначе заметка «без сверки» берёт
    лок раньше записи, а держатель лока «locked» не получает — подделка
    изображала бы невозможное.
    """
    monkeypatch.setattr(
        orchestration, "_approved_code_check", AsyncMock(return_value=("", ""))
    )
    real = repo.record_pipeline_merge
    state = {"calls": 0}

    async def flaky(db, **kw):
        state["calls"] += 1
        # Держатель лока «locked» не получает: только вне своей транзакции.
        if state["calls"] <= failures and not db.in_transaction:
            raise sqlite3.OperationalError("database is locked")
        return await real(db, **kw)

    monkeypatch.setattr(repo, "record_pipeline_merge", flaky)
    return state


async def test_a_locked_write_after_a_merged_pr_is_not_a_failed_delivery(
    db, db_dsn, monkeypatch
):
    """AC-2: один сбой записи — повтор; сбой до конца ожидания — не решение."""
    task_id = await _approved_pair_task(db)
    _git()

    # Один сбой — повтор записи, доставка как обычно.
    state = _locked_record(monkeypatch, failures=1)
    conn = await db_module.connect(db_dsn)
    try:
        ok, detail = await services.merge_before_completion(
            conn, dict(await repo.get_task(conn, task_id))
        )
    finally:
        await conn.close()
    assert (ok, detail) == (True, MERGE_SHA), detail
    assert state["calls"] == 2
    assert len(await _rows(db, task_id)) == 1


async def test_a_record_that_never_lands_waits_and_the_next_pass_writes_it(
    db, monkeypatch
):
    """AC-2: запись не легла за всё ожидание — задача ждёт, а не идёт к человеку."""
    from hub import poller

    monkeypatch.setattr(orchestration, "GATE_RECORD_WAIT_SECONDS", 0.2)
    task_id = await _approved_pair_task(db)
    g = _git()
    _locked_record(monkeypatch, failures=10_000)

    await poller._sweep_pair_delivery(db)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", "влитый PR не уходит к человеку"
    notes = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert any("gate_record_pending" in n and "влит" in n for n in notes), notes
    assert not any("merge_gate_error" in n for n in notes), notes
    assert g.merge_pr.await_count == 1

    # Лок отпустили: следующий проход дописывает строку, второго мержа нет.
    monkeypatch.undo()
    monkeypatch.setattr(orchestration, "GATE_RECORD_WAIT_SECONDS", 3.0)
    await poller._sweep_pair_delivery(db)

    assert dict(await repo.get_task(db, task_id))["status"] == "completed"
    assert g.merge_pr.await_count == 1, "PR не вливается второй раз"
    assert await _rows(db, task_id) == [{"pr_number": 501, "merge_sha": MERGE_SHA}]


async def test_the_forge_merge_is_not_called_under_a_write_transaction(db):
    """AC-3: путь поллера зовёт форж, не держа write-транзакцию.

    Именно этот путь 26.09 стал жертвой: он не должен сам становиться
    держателем лока на время сети. Done-flow держит транзакцию по построению
    (SAVEPOINT done_flow, #364) — эта граница названа в сдаче #1428.
    """
    from hub import poller

    task_id = await _approved_pair_task(db)
    seen: list[bool] = []

    async def _merge(*_a, **_kw) -> bool:
        seen.append(db.in_transaction)
        return True

    _git(AsyncMock(side_effect=_merge))
    await poller._sweep_pair_delivery(db)

    assert seen == [False], seen
    assert dict(await repo.get_task(db, task_id))["status"] == "completed"


async def test_a_human_delivery_with_an_unwritten_record_is_named_merged(
    db, monkeypatch
):
    """Находка ревью #1428: deliver по решению человека — PR влит, не «открыт»."""
    from hub.services.lifecycle import deliver_on_disposition

    monkeypatch.setattr(orchestration, "GATE_RECORD_WAIT_SECONDS", 0.2)
    task_id = await _approved_pair_task(db)
    g = _git()
    # PR читается открытым, как на проде: иначе resolve_delivery_pr пишет
    # «состояние неизвестно» до мержа, и соединение уже держит лок.
    g.pr_state = AsyncMock(return_value="open")
    _locked_record(monkeypatch, failures=10_000)
    await db.commit()  # decide закоммитил задачу до доставки (lifecycle)

    ok, reason, _ = await deliver_on_disposition(
        db, task_id, "deliver", via="decide_accept"
    )

    notes = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert ok is True, reason
    assert reason.startswith(orchestration.GATE_RECORD_PENDING_PREFIX), reason
    assert not any("PR остался открытым" in n for n in notes), notes
    assert any("реестр — нет" in n and "PR #501 влит" in n for n in notes), notes


# ---- #1430: done-flow не держит write-лок на время сети ----


async def test_a_done_report_does_not_hold_the_write_lock_during_the_forge_merge(
    db, db_dsn
):
    """AC-1: во время merge_pr соединение done-отчёта не в транзакции, и
    второе соединение пишет без ожидания busy_timeout."""
    from hub.models import TaskUpdateCreate

    task_id = await _approved_pair_task(db)
    conn = await db_module.connect(db_dsn)
    other = await db_module.connect(db_dsn)
    seen: list[tuple[bool, bool]] = []

    async def _merge(*_a, **_kw) -> bool:
        try:
            await other.execute("BEGIN IMMEDIATE")
            await other.rollback()
            free = True
        except sqlite3.OperationalError:
            free = False
        seen.append((conn.in_transaction, free))
        return True

    _git(AsyncMock(side_effect=_merge))
    try:
        await services.add_update(
            conn, task_id, TaskUpdateCreate(agent="dev", kind="done", content="готово")
        )
        status = dict(await repo.get_task(conn, task_id))["status"]
    finally:
        await other.close()
        await conn.close()

    assert seen == [(False, True)], seen
    assert status == "completed"
    assert await _rows(db, task_id) == [{"pr_number": 501, "merge_sha": MERGE_SHA}]


async def test_a_second_done_in_the_merge_window_delivers_once(db, db_dsn):
    """AC-3: пока done-отчёт вливает PR уже вне транзакции, поллер проходит
    по той же задаче — PR вливается один раз, строка одна, человека не зовут."""
    from hub import poller
    from hub.models import TaskUpdateCreate

    task_id = await _approved_pair_task(db)
    conn = await db_module.connect(db_dsn)
    other = await db_module.connect(db_dsn)
    sweeps: list[asyncio.Task] = []

    async def _merge(*_a, **_kw) -> bool:
        if not sweeps:
            sweeps.append(asyncio.create_task(poller._sweep_pair_delivery(other)))
            await asyncio.sleep(0.3)
        return True

    g = _git(AsyncMock(side_effect=_merge))
    try:
        await services.add_update(
            conn, task_id, TaskUpdateCreate(agent="dev", kind="done", content="готово")
        )
        await asyncio.wait_for(asyncio.gather(*sweeps), 10)
    finally:
        await other.close()
        await conn.close()

    task = dict(await repo.get_task(db, task_id))
    assert g.merge_pr.await_count == 1, "PR вливается один раз"
    assert task["status"] == "completed", task["status"]
    assert await _rows(db, task_id) == [{"pr_number": 501, "merge_sha": MERGE_SHA}]


async def test_a_deferred_completion_leaves_an_already_delivered_task_alone(db):
    """Поллер доставил задачу между коммитом done-flow и отложенным шагом:
    шаг ничего не делает — ни второго мержа, ни второго task_completed."""
    from hub import poller

    task_id = await _approved_pair_task(db)
    g = _git()
    await poller._sweep_pair_delivery(db)
    assert dict(await repo.get_task(db, task_id))["status"] == "completed"

    await orchestration.run_deferred_completions(
        db, [{"task_id": task_id, "exit_code": None, "result_text": None}]
    )

    completed = await db.execute_fetchall(
        "SELECT id FROM events WHERE task_id = ? AND kind = 'task_completed'",
        (task_id,),
    )
    assert g.merge_pr.await_count == 1, "второго мержа нет"
    assert len(completed) == 1, "второго task_completed нет"
