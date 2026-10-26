"""Пустой отчёт в каждом режиме применения стюарда (#2082).

Правило одно на все режимы: признаки исполнения (пин, число агентов) работу
ревьюера не доказывают, доказывает её только биллинг провайдера того запуска,
который породил ИМЕННО этот отчёт.
"""

from __future__ import annotations

import asyncio

import pytest

from hub import repository as repo
from hub.integrations import cursor_cloud
from hub.services.refusal_reason import CodedRefusal
from hub.services.steward_applied import (
    APPLIED,
    ESCALATED_TO_HUMAN,
    apply_judgement,
    apply_self_approval,
)
from tests.steward_double_clean_support import build_case, verdict_of
from tests.steward_empty_report_support import (
    PROVEN,
    add_report,
    empty_case,
    settle_dispatch,
)


async def test_double_clean_pair_with_zero_candidates_is_refused(db, monkeypatch):
    """Пара двойной чистоты из пустых отчётов не заменяет биллинг самодекларацией."""
    case = await build_case(
        db, monkeypatch, "dc-empty-pair", main={"raw": 0}, extra={"raw": 0}
    )

    result = await apply_self_approval(db, case.task_id, 1)

    assert result is not None and result[0] == ESCALATED_TO_HUMAN, result
    assert await verdict_of(db, case.task_id) == ""
    reason = dict(await repo.get_task(db, case.task_id))["verdict_owner_reason"]
    assert "no_data" in reason, reason


async def test_double_clean_pair_with_one_empty_report_is_refused(db, monkeypatch):
    for label, main, extra in (("main", 0, 2), ("extra", 2, 0)):
        case = await build_case(
            db,
            monkeypatch,
            f"dc-one-empty-{label}",
            main={"raw": main},
            extra={"raw": extra},
        )

        result = await apply_self_approval(db, case.task_id, 1)

        assert result is not None and result[0] == ESCALATED_TO_HUMAN, (label, result)
        assert await verdict_of(db, case.task_id) == "", label


async def test_double_clean_pair_with_candidates_still_applies(db, monkeypatch):
    case = await build_case(
        db, monkeypatch, "dc-seen-pair", main={"raw": 1}, extra={"raw": 3}
    )

    result = await apply_self_approval(db, case.task_id, 1)

    assert result is not None and result[0] == APPLIED, result


async def test_a_billing_of_another_report_is_not_the_proof_of_this_one(
    db, monkeypatch
):
    """Отчёт A привязан к запуску с биллингом; отчёт B своего запуска не имеет."""
    from tests.steward_double_clean_support import judge_and_advise, policy_of

    case = await build_case(
        db,
        monkeypatch,
        "ab-billing",
        risk="R1",
        policy=policy_of(ordinary=None, clean=None),
        pair=False,
        order=False,
    )
    first = await add_report(db, case.task_id, raw_count=2)
    calls = await settle_dispatch(
        db, case.task_id, monkeypatch, usage=PROVEN, report_id=first
    )
    await add_report(db, case.task_id, raw_count=0, tokens_spent=PROVEN)
    await judge_and_advise(db, case.task_id)

    result = await apply_self_approval(db, case.task_id, 1)

    assert result is not None and result[0] == ESCALATED_TO_HUMAN, result
    assert await verdict_of(db, case.task_id) == ""
    assert calls == [], "чужой запуск провайдера не спрашивают"
    reason = dict(await repo.get_task(db, case.task_id))["verdict_owner_reason"]
    assert reason.startswith("no_data_report"), reason


async def test_a_child_task_does_not_inherit_the_proof_of_the_application(
    db, monkeypatch
):
    """Доказательство живёт в применении, а не в унаследованном контексте дочерней Task."""
    from hub.services import steward_apply, steward_empty_report

    case, calls = await empty_case(db, monkeypatch, "child-task")
    release = asyncio.Event()

    async def child() -> None:
        await release.wait()
        await apply_judgement(db, case.task_id, 1)

    async with steward_apply.ceiling_scope():
        assert await steward_empty_report.early_refusal(db, case.task_id, 1) is None
        assert len(calls) == 1
        task = asyncio.create_task(child())
        await asyncio.sleep(0)

    async def down(agent_id, run_id=None):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(cursor_cloud, "get_usage", down)
    release.set()
    with pytest.raises(CodedRefusal) as caught:
        await task
    assert caught.value.code == "no_data_report"
    assert await verdict_of(db, case.task_id) == ""
