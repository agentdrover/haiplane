"""CI стал verified: автопилот и повтор применения стюарда (#2054), крайние случаи."""

from __future__ import annotations

import aiosqlite
from fastapi import HTTPException

from tests import ci_wake_support as wake
from tests.steward_double_clean_support import build_case, policy_of, verdict_of


async def _case(db, monkeypatch, slug):
    return await build_case(
        db, monkeypatch, slug, policy=policy_of("r2", None, None), risk="R2"
    )


async def test_a_missing_risk_class_fact_is_retried_when_ci_turns_verified(
    db: aiosqlite.Connection, monkeypatch
):
    """Факт ранней проверки класса не привязан к сдаче: переходящий сбой, как ceiling_check_failed."""
    from hub.services import steward_advisor, steward_apply

    case = await _case(db, monkeypatch, "wake-unbound")

    async def forgets(*_args, **_kwargs):
        return None  # ранняя проверка не оставила факта для этой сдачи

    with monkeypatch.context() as scoped:
        scoped.setattr(steward_apply, "remember_risk_fact", forgets)
        assert await steward_advisor.apply_advisor_outcomes(db) == 1
    row = await wake.judgement_of(db, case.task_id)
    assert row["advisor_outcome"] == "failed"
    assert row["advisor_outcome_detail"].startswith("ceiling_fact_unbound: ")

    await wake.ci_report_arrives(db, monkeypatch, case.task_id)

    assert await verdict_of(db, case.task_id) == "approved"
    assert len(await wake.events_of(db, wake.RETRY_EVENT, case.task_id)) == 1


async def test_an_actual_class_above_the_ceiling_is_not_retried(
    db: aiosqlite.Connection, monkeypatch
):
    """Постоянный отказ (класс выше потолка) остаётся failed и уходит к человеку."""
    case = await _case(db, monkeypatch, "wake-permanent")
    above = HTTPException(
        422, "class_above_policy_ceiling: steward_approve_max_class=r1: выше потолка"
    )
    await wake.fail_the_apply(db, monkeypatch, exc=above)
    before = await wake.judgement_of(db, case.task_id)
    assert before["advisor_outcome"] == "failed"

    await wake.ci_report_arrives(db, monkeypatch, case.task_id)

    after = await wake.judgement_of(db, case.task_id)
    assert after["advisor_outcome"] == "failed"
    assert after["advisor_outcome_detail"] == before["advisor_outcome_detail"]
    assert await wake.events_of(db, wake.RETRY_EVENT, case.task_id) == []
    assert await verdict_of(db, case.task_id) == ""


async def test_the_retry_happens_once_per_generation(
    db: aiosqlite.Connection, monkeypatch
):
    """Повтор тоже отказал переходящей причиной - второго повтора нет, события не множатся."""
    case = await _case(db, monkeypatch, "wake-once")
    await wake.fail_the_apply(db, monkeypatch, gap="нет отчёта CI")
    from hub.services import ci_trust

    async def still_red(*_a, **_k):
        return "нет отчёта CI"

    with monkeypatch.context() as scoped:
        scoped.setattr(ci_trust, "pass_gap", still_red)
        await wake.ci_report_arrives(db, monkeypatch, case.task_id)
    assert len(await wake.events_of(db, wake.RETRY_EVENT, case.task_id)) == 1
    again = await wake.judgement_of(db, case.task_id)
    assert again["advisor_outcome"] == "failed"

    before = await wake.count_events(db, case.task_id)
    await wake.ci_report_arrives(db, monkeypatch, case.task_id)
    assert await wake.count_events(db, case.task_id) == before
    assert len(await wake.events_of(db, wake.RETRY_EVENT, case.task_id)) == 1
    assert await verdict_of(db, case.task_id) == ""


async def test_a_verdict_already_written_is_not_retried(
    db: aiosqlite.Connection, monkeypatch
):
    """Вердикт этой сдачи уже стоит - повтор не нужен и не пишется."""
    from hub import repository as repo

    case = await _case(db, monkeypatch, "wake-written")
    await wake.fail_the_apply(db, monkeypatch, gap="нет отчёта CI")
    await repo.update_task(
        db, case.task_id, review_verdict="approved", review_verdict_generation=1
    )
    await db.commit()

    await wake.ci_report_arrives(db, monkeypatch, case.task_id)

    assert await wake.events_of(db, wake.RETRY_EVENT, case.task_id) == []
    assert (await wake.judgement_of(db, case.task_id))["advisor_outcome"] == "failed"


async def test_a_race_is_not_marked_failed(db: aiosqlite.Connection, monkeypatch):
    """B-2: каждая из гонок 409 - не failed, без ухода к человеку и без повторов."""
    for code in ("verdict_taken", "submission_moved", "generation_moved"):
        case = await _case(db, monkeypatch, f"wake-race-{code}")
        await wake.fail_the_apply(
            db, monkeypatch, exc=HTTPException(409, f"{code}: гонка")
        )
        row = await wake.judgement_of(db, case.task_id)
        assert row["advisor_outcome"] == "skipped", (code, row)
        assert await wake.events_of(db, "steward_apply_failed", case.task_id) == []


async def test_the_retry_itself_is_recorded_and_runs_once(
    db: aiosqlite.Connection, monkeypatch
):
    """Повтор пишет событие и не выполняется второй раз, даже если зовут напрямую."""
    from hub.services import ci_trust
    from hub.services.steward_advisor import retry_transient_apply

    case = await _case(db, monkeypatch, "wake-direct")
    await wake.fail_the_apply(db, monkeypatch, gap="нет отчёта CI")

    async def still_red(*_a, **_k):
        return "нет отчёта CI"

    with monkeypatch.context() as scoped:
        scoped.setattr(ci_trust, "pass_gap", still_red)
        assert await retry_transient_apply(db, case.task_id, 1) is True
        assert await retry_transient_apply(db, case.task_id, 1) is False
    assert len(await wake.events_of(db, wake.RETRY_EVENT, case.task_id)) == 1


async def test_a_resubmission_during_the_check_does_not_consume_the_next_retry(
    db: aiosqlite.Connection, monkeypatch
):
    await _resubmitted_during_the_check(db, monkeypatch, "wake-resubmit", 2, wake.SHA)


async def test_a_repin_during_the_check_wakes_nobody(
    db: aiosqlite.Connection, monkeypatch
):
    await _resubmitted_during_the_check(db, monkeypatch, "wake-repin", 1, "b" * 40)


async def _resubmitted_during_the_check(db, monkeypatch, slug, new_gen, new_sha):
    """P2-1: пересдача, пока шла проверка CI, - старое поколение не съедает повтор нового."""
    from hub import repository as repo
    from hub.services import ci_trust

    case = await _case(db, monkeypatch, slug)
    await wake.fail_the_apply(db, monkeypatch, gap="нет отчёта CI")
    await repo.set_ci_run_report_trust(
        db, case.task_id, wake.SHA, trust="unverified", trust_reason="p", attempt_key=""
    )
    await db.commit()

    async def resubmitted_meanwhile(_db, task_id, head_sha, *_a, **_k):
        # Поколение 2 уже стоит, его failed-суждение ждёт своего CI.
        await db.execute(
            "UPDATE steward_judgements SET generation=? WHERE task_id=?",
            (new_gen, task_id),
        )
        await repo.update_task(
            db, task_id, submission_generation=new_gen, submission_sha=new_sha
        )
        await db.commit()
        return ci_trust.Standing(trust=ci_trust.VERIFIED, reason="")

    monkeypatch.setattr(ci_trust, "_refresh_locked", resubmitted_meanwhile)
    await ci_trust.refresh(db, case.task_id, wake.SHA)

    assert await wake.events_of(db, wake.WAKE_EVENT, case.task_id) == []
    assert await wake.events_of(db, wake.RETRY_EVENT, case.task_id) == []
    row = dict(await repo.get_steward_judgement(db, case.task_id, new_gen, "verdict"))
    assert row["advisor_outcome"] == "failed"
    from hub.services.steward_advisor import retry_transient_apply

    if new_gen != 1:
        assert await retry_transient_apply(db, case.task_id, 1) is False
    assert await wake.events_of(db, wake.RETRY_EVENT, case.task_id) == []


async def test_a_refresh_that_turns_trust_verified_wakes_once(
    db: aiosqlite.Connection, monkeypatch
):
    """P2-2: отчёт принят при недоступном GitHub, проверка позже даёт verified - одно пробуждение."""
    from hub import repository as repo
    from hub.services import ci_trust

    case = await _case(db, monkeypatch, "wake-refresh")
    await wake.fail_the_apply(db, monkeypatch, gap="нет отчёта CI")
    await repo.set_ci_run_report_trust(
        db, case.task_id, wake.SHA, trust="unverified", trust_reason="p", attempt_key=""
    )
    await db.commit()
    await wake.verified_by_github(monkeypatch)

    await ci_trust.refresh(db, case.task_id, wake.SHA)  # путь поллера (sweep)

    assert len(await wake.events_of(db, wake.WAKE_EVENT, case.task_id)) == 1
    assert await verdict_of(db, case.task_id) == "approved"
    before = await wake.count_events(db, case.task_id)
    await ci_trust.refresh(db, case.task_id, wake.SHA)
    assert await wake.count_events(db, case.task_id) == before
