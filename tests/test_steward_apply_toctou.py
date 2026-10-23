"""Право стюарда на автономное одобрение перепроверяется в транзакции записи (#2037).

Ранние проверки (делегирование вердикта и cap-handoff советника) читают базу ДО
записи вердикта. Между последней из них и ``_record`` второе соединение может
забрать вердикт себе (``verdict=human``) или передать сдачу человеку из-за
суточного потолка. Транзакция записи обязана спросить тех же читателей ещё раз
и отказать с той же причиной, не записав вердикт.

Сцена настоящая (файловая SQLite, два соединения): ``tests/steward_double_clean_support``.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi import HTTPException

from hub import config
from hub import db as db_module
from hub import repository as repo
from hub.models import ReviewVerdict, TaskReviewVerdict
from hub.services import steward_applied, steward_cap
from hub.services.steward_advisor import advisor_refusal
from hub.services.steward_apply import policy_refusal
from hub.services.steward_applied import APPLIED, apply_self_approval
from hub.services.lifecycle import record_review_verdict
from tests.steward_double_clean_support import (
    build_case,
    policy_of,
    set_policy,
    verdict_events,
    verdict_of,
)


async def _switch_to_human(other, case, _monkeypatch) -> None:
    """Владелец забрал вердикт себе: политика проекта - verdict=human."""
    await set_policy(other, case.project_id, {**policy_of(), "verdict": "human"})


async def _cap_handoff(other, case, monkeypatch) -> None:
    """Сдача передана человеку: отказ ``daily_cap`` по заказу вердикта, потолок исчерпан."""
    later = steward_cap.utcnow() + timedelta(hours=1)
    monkeypatch.setattr(steward_cap, "utcnow", lambda: later)
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 0)
    await steward_cap.record_refusal(
        other, case.task_id, case.project_id, 1, "verdict", "потолок"
    )
    await other.commit()


async def _nothing(other, case, monkeypatch) -> None:
    """Контроль: между проверкой и записью второе соединение ничего не меняет."""


async def _apply_with_a_write_before_record(db, db_dsn, case, monkeypatch, change):
    """Применение стюарда; перед ``_record`` второе соединение делает ``change``.

    ``_record`` стоит сразу за последним ``advisor_refusal`` пути применения
    (``apply_judgement``): это и есть окно из постановки.
    """
    other = await db_module.connect(db_dsn)
    real_record = steward_applied._record

    async def record_after_the_other_writes(*args, **kwargs):
        await change(other, case, monkeypatch)
        return await real_record(*args, **kwargs)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(steward_applied, "_record", record_after_the_other_writes)
            return await apply_self_approval(db, case.task_id, 1)
    finally:
        await other.close()


async def test_control_without_a_change_the_approve_is_recorded(
    db, db_dsn, monkeypatch
):
    case = await build_case(db, monkeypatch, "toctou-control")

    result = await _apply_with_a_write_before_record(
        db, db_dsn, case, monkeypatch, _nothing
    )

    assert result is not None and result[0] == APPLIED, result
    assert await verdict_of(db, case.task_id) == ReviewVerdict.approved.value
    assert len(await verdict_events(db, case.task_id)) == 1


async def test_delegation_and_cap_rechecked_at_write(db, db_dsn, monkeypatch):
    """AC-1: оба случая окна - approve не записан, причина та же, что у ранней проверки."""
    seen: dict[str, str] = {}
    for label, change in (
        ("policy_not_delegated", _switch_to_human),
        ("cap", _cap_handoff),
    ):
        case = await build_case(db, monkeypatch, f"toctou-{label}")
        with pytest.raises(HTTPException) as caught:
            await _apply_with_a_write_before_record(
                db, db_dsn, case, monkeypatch, change
            )

        assert caught.value.status_code == 422, (label, caught.value.detail)
        assert await verdict_of(db, case.task_id) == "", label
        assert await verdict_events(db, case.task_id) == [], label
        # Причина - слово в слово ответ ранней проверки, прочитанный теперь.
        early = (
            await policy_refusal(db, case.task_id)
            if label == "policy_not_delegated"
            else await advisor_refusal(db, case.task_id, 1)
        )
        assert early is not None, label
        assert caught.value.detail == f"{early[0]}: {early[1]}", (
            label,
            caught.value.detail,
        )
        seen[label] = str(caught.value.detail)

    assert "policy_not_delegated" in seen["policy_not_delegated"]
    assert "потолок" in seen["cap"]


async def test_a_steward_return_for_rework_is_not_gated(db, monkeypatch):
    """Возврат правок стюардом права на АВТОНОМНОЕ ОДОБРЕНИЕ не требует: не затронут."""
    case = await build_case(db, monkeypatch, "toctou-changes-requested")
    await set_policy(db, case.project_id, {**policy_of(), "verdict": "human"})

    await record_review_verdict(
        db,
        case.task_id,
        TaskReviewVerdict(
            agent="steward",
            verdict=ReviewVerdict.changes_requested,
            comments="правки",
        ),
        expected_generation=1,
        applied_by_steward=True,
    )

    stored = dict(await repo.get_task(db, case.task_id))
    assert stored["review_verdict"] == ReviewVerdict.changes_requested.value
    assert json.dumps(stored["review_cycle"]) == "1"
