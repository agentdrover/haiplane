"""Двойная чистота: гонки между ранним и транзакционным этапами (#1986).

Ранний этап решает по фактам, прочитанным до записи вердикта. Транзакция
записи читает базу ещё раз ТЕМ ЖЕ решением: исключение выдаётся, только если
перечитанное совпало с решённым. Второе соединение здесь - тот, кто пишет в
базу между двумя этапами (отчёт, пересдача, CI, политика).
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from hub import db as db_module
from hub import repository as repo
from hub.models import ReviewVerdict, TaskReviewVerdict
from hub.services.lifecycle import record_review_verdict
from hub.services.steward_apply import apply_refusals, ceiling_scope
from hub.services.steward_applied import (
    APPLIED,
    ESCALATED_TO_HUMAN,
    apply_self_approval,
)
from tests.steward_double_clean_support import (
    CEILING,
    CLEAN_CEILING,
    FINDING,
    LISTED,
    UNRESOLVED,
    OTHER,
    REVIEWERS,
    THIRD,
    add_report,
    build_case,
    ceiling_detail,
    judge_and_advise,
    policy_of,
    seed_ci,
    set_policy,
    verdict_events,
    verdict_of,
)

SECOND_SHA = "b" * 40


async def _write(db, task_id: int) -> None:
    await record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(agent="steward", verdict=ReviewVerdict.approved),
        expected_generation=1,
        applied_by_steward=True,
    )


async def _between(db, case, other_write):
    """Ранний этап прошёл; ``other_write`` пишет второе соединение; запись вердикта."""
    async with ceiling_scope():
        early = await apply_refusals(db, case.task_id, 1)
        assert early == [], f"ранний этап обязан пропустить: {early}"
        await other_write()
        try:
            await _write(db, case.task_id)
        except HTTPException as err:
            return err
    return None


# метка -> что делает второе соединение между этапами
async def _third_report_with_a_finding(other, case):
    await add_report(other, case.task_id, THIRD, confirmed=(FINDING,))


async def _third_report_incomplete(other, case):
    await add_report(other, case.task_id, THIRD, incomplete=True)


async def _third_report_unclaimed(other, case):
    await add_report(other, case.task_id, THIRD, incomplete=None)


async def _third_report_unreadable_findings(other, case):
    review_id = await add_report(other, case.task_id, THIRD)
    await other.execute(
        "UPDATE machine_reviews SET findings_confirmed='' WHERE id=?", (review_id,)
    )
    await other.commit()


async def _third_report_lost_dimension(other, case):
    await add_report(other, case.task_id, THIRD, lost=("security",))


async def _third_report_unresolved_unpinned(other, case):
    await add_report(other, case.task_id, THIRD, pinned=False, unresolved=(UNRESOLVED,))


async def _a_pair_report_turns_dirty(other, case):
    await other.execute(
        "UPDATE machine_reviews SET findings_confirmed=? WHERE id=?",
        (json.dumps([FINDING]), case.extra),
    )
    await other.commit()


async def _new_submission_sha(other, case):
    await other.execute(
        "UPDATE tasks SET submission_sha=? WHERE id=?", (SECOND_SHA, case.task_id)
    )
    await other.commit()
    await _green_for(other, case, SECOND_SHA)


async def _new_ci_attempt(other, case):
    await seed_ci(other, case.task_id, attempt="ci:1:2")


async def _reviewers_list_changed(other, case):
    await set_policy(other, case.project_id, policy_of(reviewers=(999,)))


async def _exception_switched_off(other, case):
    await set_policy(other, case.project_id, policy_of(clean=None))


async def _ordinary_ceiling_removed_then_class_raised(other, case):
    await other.execute("UPDATE tasks SET risk_class='R4' WHERE id=?", (case.task_id,))
    await other.commit()


async def _green_for(other, case, sha: str) -> None:
    from hub.services.ci_report import VALIDATION_PASS
    from tests.ci_trust_support import (
        VERIFIED_ROW,
        seed_clean_integrity,
        upsert_hub_report,
    )

    await upsert_hub_report(
        other,
        task_id=case.task_id,
        head_sha=sha,
        ac_results="[]",
        validation_status=VALIDATION_PASS,
        validation_log="",
        reason="",
        reported_by="ci",
        **VERIFIED_ROW,
    )
    await seed_clean_integrity(other, case.task_id, sha)
    await other.commit()


_CANCELLING = {
    "third_report_with_a_finding": (_third_report_with_a_finding, "confirmed:"),
    "third_report_incomplete": (_third_report_incomplete, "incomplete:"),
    "third_report_unclaimed": (_third_report_unclaimed, "incomplete:"),
    "third_report_unreadable_findings": (
        _third_report_unreadable_findings,
        "confirmed:",
    ),
    "third_report_lost_dimension": (_third_report_lost_dimension, "lost_dimensions:"),
    "third_report_unresolved_unpinned": (
        _third_report_unresolved_unpinned,
        "unresolved:",
    ),
    "a_pair_report_turns_dirty": (_a_pair_report_turns_dirty, "confirmed:"),
    # коммит сдачи: ранняя проверка привязана к (поколение, sha) и отказывает сама
    "new_submission_sha": (_new_submission_sha, "коммит"),
    "new_ci_attempt": (_new_ci_attempt, "ci:"),
    "reviewers_list_changed": (_reviewers_list_changed, "policy:"),
    "exception_switched_off": (_exception_switched_off, None),
    "class_raised": (_ordinary_ceiling_removed_then_class_raised, "class:"),
}


@pytest.mark.parametrize("label", sorted(_CANCELLING))
async def test_new_negative_evidence_cancels_the_exception(
    db, db_dsn, monkeypatch, label
):
    write, token = _CANCELLING[label]
    case = await build_case(db, monkeypatch, f"dc-race-{label}")
    other = await db_module.connect(db_dsn)
    try:

        async def other_write():
            await write(other, case)

        err = await _between(db, case, other_write)
    finally:
        await other.close()

    assert err is not None, f"{label}: вердикт не должен быть записан"
    assert "class_above_policy_ceiling" in str(err.detail), (label, err.detail)
    if token is not None:
        assert token in str(err.detail), (label, err.detail)
    assert await verdict_of(db, case.task_id) == ""
    assert await verdict_events(db, case.task_id) == []


async def test_a_new_generation_cancels_the_decision_made_for_the_old_one(
    db, db_dsn, monkeypatch
):
    case = await build_case(db, monkeypatch, "dc-race-generation")
    other = await db_module.connect(db_dsn)
    try:

        async def bump():
            await other.execute(
                "UPDATE tasks SET submission_generation=2 WHERE id=?", (case.task_id,)
            )
            await other.commit()

        err = await _between(db, case, bump)
    finally:
        await other.close()

    assert err is not None
    assert await verdict_of(db, case.task_id) == ""


async def test_a_new_clean_report_does_not_replace_the_pair(db, db_dsn, monkeypatch):
    """Новый чистый отчёт пару не меняет: вердикт пишется по прежней паре."""
    case = await build_case(db, monkeypatch, "dc-race-clean-third")
    other = await db_module.connect(db_dsn)
    try:

        async def clean_third():
            await add_report(other, case.task_id, LISTED + 50)
            await add_report(other, case.task_id, THIRD)

        err = await _between(db, case, clean_third)
    finally:
        await other.close()

    assert err is None
    audit = (await verdict_events(db, case.task_id))[0]["payload"]["double_clean"]
    assert audit["main"]["review_id"] == case.main
    assert audit["extra"]["review_id"] == case.extra


async def test_the_record_and_the_audit_commit_or_roll_back_together(db, monkeypatch):
    """Сбой ПОСЛЕ записи вердикта откатывает вердикт, строку ленты и аудит вместе."""
    from hub.services import lifecycle

    case = await build_case(db, monkeypatch, "dc-race-atomic")
    real = lifecycle.repo.insert_event

    async def failing(conn, *args, **kwargs):
        if kwargs.get("kind") == "review_verdict_recorded":
            raise RuntimeError("сбой записи события")
        return await real(conn, *args, **kwargs)

    monkeypatch.setattr(lifecycle.repo, "insert_event", failing)
    with pytest.raises(RuntimeError):
        await apply_self_approval(db, case.task_id, 1)
    monkeypatch.setattr(lifecycle.repo, "insert_event", real)

    assert await verdict_of(db, case.task_id) == ""
    assert await verdict_events(db, case.task_id) == []
    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? "
        "AND lower(content) LIKE '%двойной чистоте%'",
        (case.task_id,),
    )
    assert list(rows) == []
    # Положительный контроль: без сбоя те же три следа появляются вместе.
    outcome, detail = await apply_self_approval(db, case.task_id, 1)
    assert outcome == APPLIED, detail
    assert (await verdict_events(db, case.task_id))[0]["payload"]["double_clean"]


async def test_a_stale_audit_never_rides_on_a_verdict_that_did_not_need_it(
    db, monkeypatch
):
    """Аудит прошлого прохода шлюза не попадает в запись, которой исключение не нужно."""
    from hub.services.steward_apply import applied_double_clean, ceiling_gate

    case = await build_case(db, monkeypatch, "dc-race-stale-audit")

    async with ceiling_scope():
        assert await apply_refusals(db, case.task_id, 1) == []
        assert await ceiling_gate(db, case.task_id, "steward_approve_max_class") is None
        assert applied_double_clean(case.task_id) is not None
        await set_policy(db, case.project_id, {"verdict": "steward"})  # потолка нет
        await _write(db, case.task_id)

    assert await verdict_of(db, case.task_id) == "approved"
    payload = (await verdict_events(db, case.task_id))[0]["payload"]
    assert "double_clean" not in payload
    line = "\n".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, case.task_id)
    )
    assert "двойной чистоте" not in line.lower()


async def test_a_ci_attempt_swapped_during_the_check_is_not_trusted(db, monkeypatch):
    """Попытка CI меняется между «зелёным» и чтением ключа: исключения нет."""
    from hub.services import steward_double_clean as dc

    case = await build_case(db, monkeypatch, "dc-race-ci-swap")
    real = dc.ci_trust.pass_gap

    async def swapping(conn, task_id, sha):
        gap = await real(conn, task_id, sha)
        await conn.execute(
            "UPDATE ci_run_reports SET attempt_key='ci:9:9' WHERE task_id=?",
            (task_id,),
        )
        await conn.commit()
        return gap

    monkeypatch.setattr(dc.ci_trust, "pass_gap", swapping)

    detail = ceiling_detail(await apply_refusals(db, case.task_id, 1))

    assert "ci:" in detail and "сменилась" in detail, detail


# ---------------------------------------------------------------------------
# AC-4 - второй отчёт после заказа или после вердикта режим не включает
# ---------------------------------------------------------------------------


async def _frozen(db, task_id: int, generation: int = 1) -> str:
    rows = await db.execute_fetchall(
        "SELECT double_clean_pair FROM steward_runs WHERE task_id=? "
        "AND generation=? AND kind='verdict'",
        (task_id, generation),
    )
    return str(dict(rows[0])["double_clean_pair"])


async def test_a_late_second_report_does_not_switch_the_mode(db, monkeypatch):
    async def second_after_the_order(case):
        case.extra = await add_report(db, case.task_id, LISTED)

    case = await build_case(
        db,
        monkeypatch,
        "dc-late-second",
        pair=False,
        order=False,
    )
    case.main = await add_report(db, case.task_id, OTHER)
    await judge_and_advise(
        db,
        case.task_id,
        after_order=lambda: second_after_the_order(case),
    )

    assert await _frozen(db, case.task_id) == "", "на заказе пары не было"
    detail = ceiling_detail(await apply_refusals(db, case.task_id, 1))
    assert detail and "pair:" in detail
    outcome, _ = await apply_self_approval(db, case.task_id, 1)
    assert outcome == ESCALATED_TO_HUMAN
    assert await verdict_of(db, case.task_id) == ""


async def test_a_late_report_after_the_verdict_does_not_replace_it(db, monkeypatch):
    case = await build_case(db, monkeypatch, "dc-late-after-verdict")
    assert await _frozen(db, case.task_id) != ""
    outcome, detail = await apply_self_approval(db, case.task_id, 1)
    assert outcome == APPLIED, detail
    before = await verdict_events(db, case.task_id)

    await add_report(db, case.task_id, THIRD, confirmed=(FINDING,))
    again = await apply_self_approval(db, case.task_id, 1)

    after = await verdict_events(db, case.task_id)
    assert [e["id"] for e in after] == [e["id"] for e in before], again
    assert await verdict_of(db, case.task_id) == "approved"


async def test_the_next_submission_gets_its_own_pair(db, monkeypatch):
    """«Следующая попытка - на новой сдаче»: пара фиксируется на заказе нового поколения."""
    case = await build_case(db, monkeypatch, "dc-next-attempt", pair=False, order=False)
    await add_report(db, case.task_id, OTHER)
    await judge_and_advise(db, case.task_id)
    assert await _frozen(db, case.task_id) == ""
    assert (await apply_self_approval(db, case.task_id, 1))[0] == ESCALATED_TO_HUMAN

    await db.execute(
        "UPDATE tasks SET submission_generation=2, submission_sha=? WHERE id=?",
        (SECOND_SHA, case.task_id),
    )
    await db.execute(
        "INSERT INTO review_dispatches (task_id, submission_generation, agent_id, "
        "model, status) VALUES (?, 2, 'rev-agent', 'grok-4.6', 'done')",
        (case.task_id,),
    )
    await db.commit()
    await _green_for(db, case, SECOND_SHA)
    for principal in (OTHER, LISTED):
        await add_report(db, case.task_id, principal, sha=SECOND_SHA, generation=2)
    await judge_and_advise(db, case.task_id, generation=2)

    assert await _frozen(db, case.task_id, 2) != ""
    outcome, detail = await apply_self_approval(db, case.task_id, 2)
    assert outcome == APPLIED, detail
    audit = (await verdict_events(db, case.task_id))[0]["payload"]["double_clean"]
    assert audit["generation"] == 2 and audit["sha"] == SECOND_SHA
    assert policy_of()[CLEAN_CEILING] == audit["policy"][CLEAN_CEILING]
    assert CEILING in audit["policy"] and REVIEWERS in audit["policy"]
