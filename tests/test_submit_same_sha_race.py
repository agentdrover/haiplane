"""Параллельная и повторная сдача одного sha не плодит поколения (#2057)."""

from __future__ import annotations

import asyncio


import pytest

from hub import models
from hub import repository as repo
from hub.services import lifecycle
from tests.test_submit_expected_sha import X, _install_git, _running_task


async def test_a_same_sha_retry_returns_the_current_generation_without_a_submission(
    db, monkeypatch
):
    _install_git(monkeypatch, X)
    task = await _running_task(db, "Повтор")
    body = models.TaskSubmitReview(agent="dev")
    first = await lifecycle.submit_for_review(db, task.id, body)
    again = await lifecycle.submit_for_review(db, task.id, body)
    assert (first.submission_generation, again.submission_generation) == (1, 1)
    cur = await db.execute(
        "SELECT COUNT(*) FROM submissions WHERE task_id=?", (task.id,)
    )
    assert (await cur.fetchone())[0] == 1


async def test_a_new_sha_retried_after_a_lost_answer_opens_one_generation(
    db, db_dsn, monkeypatch
):
    """Новый sha из review: запрос, чей ответ потерян, и его повтор (#2057).

    Оба прочитали старый sha до лока; под локом второй видит, что этот sha уже
    сдан, и не открывает ещё одно поколение.
    """
    from hub.db import connect
    from hub.services import ci_trust

    git = _install_git(monkeypatch, X)
    task = await _running_task(db, "Гонка")
    body = models.TaskSubmitReview(agent="dev")
    await lifecycle.submit_for_review(db, task.id, body)
    await db.commit()
    git.tip = "b" * 40

    held, go = asyncio.Event(), asyncio.Event()
    real = ci_trust.refresh_quietly
    first = {"taken": False}

    async def gate(*a, **k):
        if not first["taken"]:
            first["taken"] = True
            held.set()
            await go.wait()  # запрос B застрял после снимка, до лока
        return await real(*a, **k)

    monkeypatch.setattr(ci_trust, "refresh_quietly", gate)
    from hub.services import finding_outcome

    refused: list[int] = []
    real_refuse = finding_outcome.refuse_conflicting

    async def spy(*a, **k):
        refused.append(1)
        return await real_refuse(*a, **k)

    monkeypatch.setattr(finding_outcome, "refuse_conflicting", spy)
    noops: list[int] = []
    real_noop = lifecycle._same_sha_noop_response

    async def noop_spy(state):
        noops.append(state.raced)
        assert state.task["submission_sha"] == "b" * 40, "снимок обновлён"
        return await real_noop(state)

    monkeypatch.setattr(lifecycle, "_same_sha_noop_response", noop_spy)
    other = await connect(db_dsn)
    try:
        racer = asyncio.ensure_future(lifecycle.submit_for_review(other, task.id, body))
        await asyncio.wait_for(held.wait(), 5)
        winner = await lifecycle.submit_for_review(db, task.id, body)
        assert winner.submission_generation == 2
        go.set()
        loser = await asyncio.wait_for(racer, 10)
    finally:
        go.set()
        await other.close()
    assert loser.submission_generation == 2, "повтор вернул текущее поколение"
    assert refused == [1], "гонка сверила исходы с записанными"
    assert noops == [True], "свежее чтение увело проигравшего до гейтов находок"
    await db.commit()
    cur = await db.execute(
        "SELECT COUNT(*), MAX(generation) FROM submissions WHERE task_id=?", (task.id,)
    )
    assert tuple(await cur.fetchone()) == (2, 2), (
        "второго поколения для того же sha нет"
    )


async def test_a_raced_submit_refuses_conflicting_outcomes_but_not_identical(
    db, monkeypatch
):
    from fastapi import HTTPException

    from hub.services import finding_outcome

    async def reports(*_a):
        return [{"id": 7}]

    async def rows(_db, _rid):
        return [{"finding_uid": "f1", "outcome": "fixed"}]

    monkeypatch.setattr(finding_outcome, "reports_owed_an_answer", reports)
    monkeypatch.setattr(repo, "list_finding_outcomes", rows)

    def item(outcome):
        return models.FindingOutcomeItem(
            finding_uid="f1", outcome=outcome, note="n", linked_task_id=None
        )

    await finding_outcome.refuse_conflicting(db, 1, 2, [item("fixed")])
    with pytest.raises(HTTPException) as err:
        await finding_outcome.refuse_conflicting(db, 1, 2, [item("deferred")])
    assert err.value.status_code == 409


async def test_a_stale_snapshot_is_caught_inside_the_write_transaction(db, monkeypatch):
    """Снимок устарел и после гейтов: под локом тот же sha уже текущая сдача."""
    git = _install_git(monkeypatch, X)
    task = await _running_task(db, "Устарел")
    body = models.TaskSubmitReview(agent="dev")
    await lifecycle.submit_for_review(db, task.id, body)
    stale = dict(await repo.get_task(db, task.id))
    git.tip = "b" * 40
    await lifecycle.submit_for_review(db, task.id, body)
    await db.commit()

    state = lifecycle.SubmitContext(db=db, task_id=task.id, task=stale, body=body)
    state.submission_sha = "b" * 40
    with pytest.raises(lifecycle._SameShaRaced):
        await lifecycle._apply_submission(state)
    cur = await db.execute(
        "SELECT COUNT(*) FROM submissions WHERE task_id=?", (task.id,)
    )
    assert (await cur.fetchone())[0] == 2, "третьего поколения нет"


async def test_the_conflict_check_runs_in_the_transaction_that_applies_outcomes(
    db, monkeypatch
):
    from hub.services import finding_outcome

    _install_git(monkeypatch, X)
    task = await _running_task(db, "Один лок")
    body = models.TaskSubmitReview(agent="dev")
    await lifecycle.submit_for_review(db, task.id, body)
    await db.commit()

    seen: list[bool] = []

    async def spy(conn, *_a):
        seen.append(conn.in_transaction)

    monkeypatch.setattr(finding_outcome, "refuse_conflicting", spy)
    state = lifecycle.SubmitContext(
        db=db,
        task_id=task.id,
        task=dict(await repo.get_task(db, task.id)),
        body=body,
    )
    state.raced = True
    await lifecycle._same_sha_noop_response(state)
    assert seen == [True], "сверка и запись исходов - одна транзакция под локом"
