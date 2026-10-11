"""#1242: paid → 429-заглушка → paid не должен тратить две ступени лестницы."""

from __future__ import annotations

import aiosqlite

from hub import repository as repo


async def _task(db: aiosqlite.Connection, title: str) -> int:
    return await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="a",
        rationale="",
        status="review",
        auto_review=False,
        task_type="task",
        parent_id=None,
        priority="medium",
    )


async def _dispatch(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    agent_id: str,
    run_id: str = "",
    replaces_dispatch_id: int | None = None,
) -> int:
    return await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=1,
        agent_id=agent_id,
        run_id=run_id,
        model="grok-4.6",
        profile="lite",
        channel="cloud",
        replaces_dispatch_id=replaces_dispatch_id,
    )


async def test_paid_stub_paid_is_one_ladder_step(db: aiosqlite.Connection) -> None:
    """Смешанная цепочка #1242: ERROR/оплата, синхронный 429, затем успех.

    ``count_review_dispatches`` смотрит только непосредственного предка.
    У успеха родитель — заглушка с пустым ``agent_id``, поэтому
    ``NOT EXISTS (родитель с agent_id)`` истинен и строка считается второй
    ступенью. ``maybe_top_up_incomplete`` при ``steps >= 2`` отказывает в
    DEEP-доборе. Прямые пути paid→paid-replace и stub→paid остаются 1.
    """
    paid_then_replace = await _task(db, "paid-replace")
    first_paid = await _dispatch(db, paid_then_replace, agent_id="bc-paid")
    await _dispatch(
        db,
        paid_then_replace,
        agent_id="bc-replace",
        run_id="r-replace",
        replaces_dispatch_id=first_paid,
    )

    stub_then_paid = await _task(db, "stub-paid")
    stub_only = await _dispatch(db, stub_then_paid, agent_id="")
    await _dispatch(
        db,
        stub_then_paid,
        agent_id="bc-after-stub",
        run_id="r-after-stub",
        replaces_dispatch_id=stub_only,
    )

    mixed = await _task(db, "paid-stub-paid")
    paid = await _dispatch(db, mixed, agent_id="bc-cloud", run_id="r-cloud")
    stub = await _dispatch(
        db, mixed, agent_id="", run_id="", replaces_dispatch_id=paid
    )
    await _dispatch(
        db,
        mixed,
        agent_id="bc-again",
        run_id="r-again",
        replaces_dispatch_id=stub,
    )
    await db.commit()

    assert await repo.count_review_dispatches(db, paid_then_replace, 1) == 1
    assert await repo.count_review_dispatches(db, stub_then_paid, 1) == 1
    assert await repo.count_review_dispatches(db, mixed, 1) == 1, (
        "заглушка 429 ничего не стоила: успех продолжает ту же ступень, "
        "что и первый оплаченный прогон"
    )
