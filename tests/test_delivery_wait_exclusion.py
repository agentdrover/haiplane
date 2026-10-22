"""Взаимное исключение мержа и эскалации срока ожидания (#1992, CR gen4).

Продолжение ``test_delivery_wait_deadline.py``: те же помощники, отдельный файл
из-за бюджета размера файла тестов.
"""

from __future__ import annotations

from hub import repository as repo
from tests.delivery_rerun_support import set_policy  # noqa: F401
from tests.test_delivery_wait_deadline import (
    _age,
    _needs_decision_events,
    _pass,
    _setup,
)


# --- Круг gen4: взаимное исключение мержа и эскалации -----------------------


async def test_a_merge_entering_the_escalation_window_is_refused(db, monkeypatch):
    """Сосед входит в мерж после последней проверки и до UPDATE перехода."""
    from hub import repository_delivery_wait as journal_module
    from hub.integrations.registry import plugins
    from hub.services import orchestration

    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    real = journal_module.escalate
    seen: dict = {}

    async def neighbour_then_escalate(*args, **kw):
        snap = dict(await repo.get_task(db, task_id))
        ctx = await orchestration.project_git_context(db, task_id)
        seen["step"], _ = await orchestration.merge_if_approval_stands(
            db, snap, ctx, checked_sha=snap["submission_sha"]
        )
        return await real(*args, **kw)

    monkeypatch.setattr(journal_module, "escalate", neighbour_then_escalate)
    await _pass(db, task_id)

    assert seen["step"] is not orchestration.MergeStep.MERGED
    plugins.git_ops.merge_pr.assert_not_awaited()
    assert (await repo.get_task(db, task_id))["status"] == "needs_decision"
    assert await _needs_decision_events(db, task_id) == 1


async def test_the_claim_is_released_after_the_transition(db, monkeypatch):
    from hub.services import delivery_claim

    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    await _pass(db, task_id)

    assert not delivery_claim.held((task_id, 360))


async def test_a_merge_in_flight_wins_over_the_escalation(db, monkeypatch):
    """Обратный порядок: мерж зарегистрирован первым - эскалация уступает."""
    import asyncio

    from hub.integrations.registry import plugins
    from hub.services import orchestration

    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    release = asyncio.Event()

    async def slow_merge(*a, **kw):
        await release.wait()
        return True, ""

    plugins.git_ops.merge_pr_with_detail = slow_merge
    snap = dict(await repo.get_task(db, task_id))
    ctx = await orchestration.project_git_context(db, task_id)
    merging = asyncio.create_task(
        orchestration.merge_if_approval_stands(
            db, snap, ctx, checked_sha=snap["submission_sha"]
        )
    )
    try:
        for _ in range(200):  # дать мержу зарегистрировать Future
            if (task_id, 360) in orchestration._gate_merges:
                break
            await asyncio.sleep(0.01)
        assert (task_id, 360) in orchestration._gate_merges
        await _pass(db, task_id)
        assert (await repo.get_task(db, task_id))["status"] == "running"
        assert await _needs_decision_events(db, task_id) == 0
    finally:
        release.set()
        step, _ = await merging
        orchestration._gate_merges.pop((task_id, 360), None)
    assert step is orchestration.MergeStep.MERGED


async def test_a_pass_started_before_the_escalation_cannot_merge_after_it(
    db, monkeypatch
):
    """Сосед с устаревшим снимком (running) продолжает после ПОЛНОГО перехода."""
    from hub.integrations.registry import plugins
    from hub.services import orchestration

    task_id = await _setup(db, monkeypatch)
    old_snapshot = dict(await repo.get_task(db, task_id))  # status running
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    assert (await _pass(db, task_id))["status"] == "needs_decision"
    ctx = await orchestration.project_git_context(db, task_id)

    step, _ = await orchestration.merge_if_approval_stands(
        db, old_snapshot, ctx, checked_sha=old_snapshot["submission_sha"]
    )

    assert step is orchestration.MergeStep.APPROVAL_GONE
    plugins.git_ops.merge_pr.assert_not_awaited()
    # Свежий снимок автоматики (отложенное завершение заменяет его перечитанной
    # строкой) тоже не мержит, пока задача у человека.
    fresh = dict(await repo.get_task(db, task_id))
    step, _ = await orchestration.merge_if_approval_stands(
        db, fresh, ctx, checked_sha=fresh["submission_sha"]
    )
    assert step is orchestration.MergeStep.APPROVAL_GONE
    plugins.git_ops.merge_pr.assert_not_awaited()
    # Решение человека (decide accept ставит completed до доставки) путь открывает.
    await repo.update_task(db, task_id, status="completed")
    await db.commit()
    fresh = dict(await repo.get_task(db, task_id))
    step, _ = await orchestration.merge_if_approval_stands(
        db, fresh, ctx, checked_sha=fresh["submission_sha"]
    )
    assert step is orchestration.MergeStep.MERGED
    orchestration._gate_merges.pop((task_id, 360), None)


async def test_a_replaced_pr_does_not_bypass_the_owner_hold(db, monkeypatch):
    from hub.integrations.registry import plugins
    from hub.services import orchestration

    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    assert (await _pass(db, task_id))["status"] == "needs_decision"
    await repo.update_task(db, task_id, pr_number=362)  # замена PR того же поколения
    await db.commit()
    fresh = dict(await repo.get_task(db, task_id))
    ctx = await orchestration.project_git_context(db, task_id)

    step, _ = await orchestration.merge_if_approval_stands(
        db, fresh, ctx, checked_sha=fresh["submission_sha"]
    )

    assert step is orchestration.MergeStep.APPROVAL_GONE
    plugins.git_ops.merge_pr.assert_not_awaited()


async def test_a_stale_running_read_does_not_lift_the_owner_hold(db, monkeypatch):
    from hub.services import delivery_claim

    task_id = await _setup(db, monkeypatch)
    await _pass(db, task_id)
    await _age(db, task_id, 30)
    assert (await _pass(db, task_id))["status"] == "needs_decision"
    row = dict(await repo.get_task(db, task_id))
    gen = row["submission_generation"]
    key = (task_id, 360)

    stale = {"status": "running", "submission_generation": gen}
    assert delivery_claim.refuses(key, stale) is False  # могло быть до коммита
    assert delivery_claim.refuses(key, row) is True  # отметка цела
