"""Запись до сети коммитится до сети, и чужая транзакция не коммитится (#2001).

Два соединения к одному файлу SQLite. Форж (git_ops) подменён и по сигналу
замирает прямо «в сети»; пока он стоит, второе соединение с коротким timeout
берёт BEGIN IMMEDIATE и читает ленту. Целая merge_before_completion не
подменяется: заглушки стоят только на вызовах форжа и git.

Владелец транзакции — тот, кто её открыл (``committed_if_own``): запись при
чистом соединении коммитит её автор; запись внутри чужой транзакции не
коммитит никто, и откат внешней её уносит.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from hub import poller
from hub import repository as repo
from hub import services
from hub.db import committed_if_own, write_transaction
from hub.integrations.protocols import (
    CIProbeOutcome,
    CIProbeResult,
)
from hub.services import lifecycle, orchestration
from tests.delivery_rerun_support import (
    TIP,
    Rig,
    approved_task,
    current,
    rig_after_base_update,
)

NEW_PR = 78
PASSED = CIProbeResult(CIProbeOutcome.passed, "checks_passed")
PENDING = CIProbeResult(CIProbeOutcome.pending, "running")


class _Rollback(Exception):
    """Откат внешней транзакции, которым тест проверяет, что её не коммитили."""


async def _probe(dsn: str, task_id: int) -> dict:
    """Второе соединение: BEGIN IMMEDIATE за 0,5 с и то, что оно видит."""
    conn = await aiosqlite.connect(dsn, timeout=0.5, isolation_level=None)
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA busy_timeout = 500")
        await conn.execute("BEGIN IMMEDIATE")  # «database is locked», если занято
        pr = await conn.execute_fetchall(
            "SELECT pr_number, submission_sha FROM tasks WHERE id=?", (task_id,)
        )
        feed = await conn.execute_fetchall(
            "SELECT content FROM task_updates WHERE task_id=?", (task_id,)
        )
        merges = await conn.execute_fetchall(
            "SELECT pr_number FROM pipeline_merges WHERE task_id=?", (task_id,)
        )
        reruns = await conn.execute_fetchall(
            "SELECT state FROM delivery_reruns WHERE task_id=?", (task_id,)
        )
        await conn.execute("ROLLBACK")
        return {
            "pr": pr[0]["pr_number"],
            "sha": pr[0]["submission_sha"],
            "feed": "\n".join(r["content"] or "" for r in feed),
            "merges": [r["pr_number"] for r in merges],
            "reruns": [r["state"] for r in reruns],
        }
    finally:
        await conn.close()


async def _frozen_in_network(
    attach, run, dsn: str, task_id: int, answer=PENDING
) -> dict:
    """Пустить ``run`` и снять срез базы, пока заглушка форжа стоит в сети."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def stand(*_args, **_kwargs):
        entered.set()
        await asyncio.wait_for(release.wait(), 10)
        return answer

    attach(stand)
    job = asyncio.create_task(run())
    try:
        waiter = asyncio.create_task(entered.wait())
        await asyncio.wait(
            {waiter, job}, timeout=10, return_when=asyncio.FIRST_COMPLETED
        )
        assert entered.is_set(), "проход закончился, не дойдя до сети"
        return await _probe(dsn, task_id)
    finally:
        release.set()
        await asyncio.gather(job, return_exceptions=True)


async def _closed_pr_rig(db, monkeypatch, *, state: str, replacement=None) -> Rig:
    task_id = await approved_task(db)
    await repo.update_task(db, task_id, branch=f"task-{task_id}/x")
    await db.commit()
    rig = Rig(db, monkeypatch, task_id)
    rig.git.head_sha.return_value = (await rig.task())["submission_sha"]
    rig.git.pr_base_freshness = AsyncMock(
        return_value=current((await rig.task())["submission_sha"])
    )
    rig.git.pr_state = AsyncMock(return_value=state)
    rig.git.pr_for_branch = AsyncMock(return_value=replacement)
    return rig


# ---- AC-1: alert при delivery_pr.reason ------------------------------------


async def test_delivery_does_not_hold_the_write_lock_across_the_network(
    db, db_dsn, monkeypatch
):
    # Состояние записанного PR неизвестно -> reason без замены PR: пишется
    # ТОЛЬКО alert, и дальше идёт CI-проба (сеть).
    rig = await _closed_pr_rig(db, monkeypatch, state="")

    def attach(stand):
        rig.git.check_pr_ci = AsyncMock(side_effect=stand)

    seen = await _frozen_in_network(
        attach,
        lambda: lifecycle.deliver_on_disposition(
            db, rig.task_id, "deliver", via="test"
        ),
        db_dsn,
        rig.task_id,
    )
    assert "неизвестно" in seen["feed"], "alert должен быть виден до конца сети"


# ---- AC-2: замена закрытого PR ---------------------------------------------


async def test_pr_replacement_is_committed_before_the_network(db, db_dsn, monkeypatch):
    rig = await _closed_pr_rig(db, monkeypatch, state="closed", replacement=NEW_PR)

    def attach(stand):
        rig.git.check_pr_ci = AsyncMock(side_effect=stand)

    seen = await _frozen_in_network(
        attach, lambda: poller._sweep_pair_delivery(db), db_dsn, rig.task_id
    )
    assert seen["pr"] == NEW_PR


async def test_replacement_by_a_human_decision_is_committed_before_the_network(
    db, db_dsn, monkeypatch
):
    rig = await _closed_pr_rig(db, monkeypatch, state="closed", replacement=NEW_PR)

    def attach(stand):
        rig.git.check_pr_ci = AsyncMock(side_effect=stand)

    seen = await _frozen_in_network(
        attach,
        lambda: lifecycle.deliver_on_disposition(
            db, rig.task_id, "deliver", via="test"
        ),
        db_dsn,
        rig.task_id,
    )
    assert seen["pr"] == NEW_PR
    assert f"#{NEW_PR}" in seen["feed"]


# ---- AC-3: бронь перезапуска до платного POST ------------------------------


async def test_rerun_reservation_is_durable_before_the_paid_post(
    db, db_dsn, monkeypatch
):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.red_first_attempt()
    # голова уже закреплена: re-pin (с его коммитом) не должен маскировать дефект
    await repo.update_task(db, rig.task_id, submission_sha=TIP)
    await db.commit()
    rig.git.pr_state = AsyncMock(return_value="")  # reason -> alert без коммита

    def attach(stand):
        async def post(run_id, repo=None, gh_repo=None, forge=""):
            await stand()
            raise RuntimeError("форж оборвал ответ")  # исход POST неизвестен

        rig.git.rerun_failed_jobs = AsyncMock(side_effect=post)

    seen = await _frozen_in_network(
        attach,
        lambda: lifecycle.deliver_on_disposition(
            db, rig.task_id, "deliver", via="test"
        ),
        db_dsn,
        rig.task_id,
    )
    assert seen["reruns"] == ["reserved"], "бронь видна до POST"
    assert rig.posts == 1
    # сбой и повтор прохода: второго платного запроса нет
    await lifecycle.deliver_on_disposition(db, rig.task_id, "deliver", via="again")
    await poller._sweep_pair_delivery(db)
    assert rig.posts == 1


async def test_rerun_inside_a_foreign_transaction_keeps_the_rollback(
    db, db_dsn, monkeypatch
):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.red_first_attempt()
    await repo.update_task(db, rig.task_id, submission_sha=TIP)
    await db.commit()
    task = dict(await repo.get_task(db, rig.task_id))
    with pytest.raises(_Rollback):
        async with write_transaction(db):  # done-flow: транзакция чужая
            await repo.add_task_update(db, rig.task_id, "dev", "status", "OUTER")
            ok, why = await services.merge_before_completion(db, task)
            assert not ok and rig.posts == 0, "в чужой транзакции платного POST нет"
            raise _Rollback
    assert "OUTER" not in (await _probe(db_dsn, rig.task_id))["feed"]
    assert rig.git.rerun_failed_jobs.await_count == 0


# ---- прочие места «запись -> сеть» -----------------------------------------


async def test_ensure_delivery_pr_leaves_no_open_transaction(db, monkeypatch):
    rig = await _closed_pr_rig(db, monkeypatch, state="open")
    rig.git.push_branch = AsyncMock(return_value=True)
    rig.git.create_pr = AsyncMock(return_value=NEW_PR)
    task = dict(await rig.task())
    got = await orchestration.ensure_delivery_pr(db, task, "task-x/x", ["a.py"])
    assert got == (NEW_PR, "")
    assert not db.in_transaction, "pr_number закоммичен до следующего шага цепочки"


async def test_repin_of_the_approved_head_is_committed_by_its_owner(db, monkeypatch):
    task, tip = await _diverged_task(db, monkeypatch)
    await orchestration._approved_code_check(db, task, task["pr_number"])
    assert not db.in_transaction
    row = dict(await repo.get_task(db, task["id"]))
    assert row["submission_sha"] == tip


async def test_found_pr_is_committed_by_pr_for_delivery(db, db_dsn, monkeypatch):
    rig = await _closed_pr_rig(db, monkeypatch, state="", replacement=NEW_PR)
    await repo.update_task(db, rig.task_id, pr_number=None)
    await db.commit()
    task = dict(await rig.task())
    got = await orchestration.pr_for_delivery(db, task)
    assert got.number == NEW_PR
    assert not db.in_transaction
    assert (await _probe(db_dsn, rig.task_id))["pr"] == NEW_PR


async def test_committed_if_own_rolls_back_only_what_it_opened(db, db_dsn):
    tid = await approved_task(db)
    with pytest.raises(_Rollback):
        async with committed_if_own(db):
            await repo.add_task_update(db, tid, "dev", "status", "OWN")
            raise _Rollback
    assert not db.in_transaction
    assert "OWN" not in (await _probe(db_dsn, tid))["feed"]
    async with write_transaction(db):
        await repo.add_task_update(db, tid, "dev", "status", "OUTER")
        with pytest.raises(_Rollback):
            async with committed_if_own(db):
                raise _Rollback
        assert db.in_transaction, "чужую транзакцию вложенный блок не откатывает"


# ---- чужая транзакция никогда не коммитится --------------------------------


async def _diverged_task(db, monkeypatch):
    """Вершина ушла вперёд только слиянием базы: гейт перезакрепляет её."""
    from hub.services import base_merge

    rig = await _closed_pr_rig(db, monkeypatch, state="open")
    tip = "tip0moved0by0base0merge"
    rig.git.head_sha = AsyncMock(return_value=tip)
    monkeypatch.setattr(
        base_merge, "author_edit_same", lambda before, after: (True, "та же правка")
    )
    return dict(await rig.task()), tip


async def _never_commits_foreign(db, db_dsn, task_id, call, *, marker: str) -> None:
    with pytest.raises(_Rollback):
        async with write_transaction(db):
            await repo.add_task_update(db, task_id, "dev", "status", "OUTER")
            await call()
            assert db.in_transaction, "чужая транзакция не закрыта вложенной записью"
            raise _Rollback
    seen = await _probe(db_dsn, task_id)
    assert "OUTER" not in seen["feed"]
    assert marker not in seen["feed"] + str(seen["pr"]) + str(seen["sha"])


async def test_foreign_transaction_is_never_committed_by_the_repin(
    db, db_dsn, monkeypatch
):
    task, tip = await _diverged_task(db, monkeypatch)
    await _never_commits_foreign(
        db,
        db_dsn,
        task["id"],
        lambda: orchestration._approved_code_check(db, task, task["pr_number"]),
        marker=tip,
    )


async def test_foreign_transaction_is_never_committed_by_pr_replacement(
    db, db_dsn, monkeypatch
):
    rig = await _closed_pr_rig(db, monkeypatch, state="closed", replacement=NEW_PR)
    task = dict(await rig.task())
    await _never_commits_foreign(
        db,
        db_dsn,
        rig.task_id,
        lambda: orchestration.resolve_delivery_pr(db, task),
        marker=str(NEW_PR),
    )


async def test_foreign_transaction_is_never_committed_by_a_found_pr(
    db, db_dsn, monkeypatch
):
    rig = await _closed_pr_rig(db, monkeypatch, state="", replacement=NEW_PR)
    await repo.update_task(db, rig.task_id, pr_number=None)
    await db.commit()
    task = dict(await rig.task())
    await _never_commits_foreign(
        db,
        db_dsn,
        rig.task_id,
        lambda: orchestration.pr_for_delivery(db, task),
        marker=str(NEW_PR),
    )


async def test_foreign_transaction_is_never_committed_by_ensure_delivery_pr(
    db, db_dsn, monkeypatch
):
    rig = await _closed_pr_rig(db, monkeypatch, state="open")
    rig.git.push_branch = AsyncMock(return_value=True)
    rig.git.create_pr = AsyncMock(return_value=NEW_PR)
    task = dict(await rig.task())
    await _never_commits_foreign(
        db,
        db_dsn,
        rig.task_id,
        lambda: orchestration.ensure_delivery_pr(db, task, "task-x/x", ["a.py"]),
        marker=str(NEW_PR),
    )
