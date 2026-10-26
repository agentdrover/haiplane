"""Пустой отчёт и режим двойной чистоты (#2082): у режима своё правило о пустоте.

Пара отчётов двойной чистоты обязана нести признаки исполнения и пин (#1986);
правило пустого отчёта автопилота (биллинг, потолок пустого ревью) к ней не
применяется, и это закреплено здесь, чтобы не сломать режим молча.
"""

from __future__ import annotations

from hub import repository as repo
from hub.models import ReviewVerdict
from hub.services.steward_applied import APPLIED, apply_self_approval
from tests.steward_double_clean_support import build_case


async def test_double_clean_pair_with_zero_candidates_keeps_its_own_rule(
    db, monkeypatch
):
    case = await build_case(
        db,
        monkeypatch,
        "dc-empty-pair",
        main={"raw": 0},
        extra={"raw": 0},
    )

    result = await apply_self_approval(db, case.task_id, 1)

    assert result is not None and result[0] == APPLIED, result
    task = dict(await repo.get_task(db, case.task_id))
    assert task["review_verdict"] == ReviewVerdict.approved.value


async def test_a_ceiling_lifted_after_the_early_check_does_not_keep_the_exemption(
    db, db_dsn, monkeypatch
):
    """Двойную чистоту подтверждает потолок, прочитанный в транзакции, а не ранний флаг."""
    import pytest
    from fastapi import HTTPException

    from hub import db as db_module
    from hub.services import steward_applied
    from tests.steward_double_clean_support import policy_of, set_policy, verdict_of

    case = await build_case(
        db, monkeypatch, "dc-empty-lifted", main={"raw": 0}, extra={"raw": 0}
    )
    other = await db_module.connect(db_dsn)
    real_record = steward_applied._record

    async def lift_then_record(*args, **kwargs):
        await set_policy(other, case.project_id, policy_of(ordinary=None, clean=None))
        return await real_record(*args, **kwargs)

    try:
        monkeypatch.setattr(steward_applied, "_record", lift_then_record)
        with pytest.raises(HTTPException) as caught:
            await apply_self_approval(db, case.task_id, 1)
    finally:
        await other.close()

    assert str(caught.value.detail).startswith("no_data_report:"), caught.value.detail
    assert await verdict_of(db, case.task_id) == ""
