"""Один автоперезапуск упавших job канонического CI на свежей базе (#1956).

Гейт слил базу в ветку, CI на новой голове красный: при политике
``delivery_fresh_base_rerun=on`` хаб один раз просит форж перезапустить упавшие
job и мержит только по доверенному отчёту ЭТОЙ попытки; иначе - к человеку с
готовой инструкцией. Настоящие: база, поллер, гейт, проверка доверия к отчёту.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from hub import services
from hub.integrations.protocols import (
    CIProbeOutcome,
    CIProbeResult,
    CIRunRequestOutcome,
    CIRunRequestResult,
)
from hub.services import ci_contract
from tests.ci_trust_support import SAT_CONTRACT, SAT_PATH, satellite_task
from tests.delivery_rerun_support import (
    POLICY_KEY,
    RUN,
    TIP,
    Rig,
    rig_after_base_update,
    set_policy,
)

PENDING = CIProbeResult(CIProbeOutcome.pending, "checks_running")
PASSED = CIProbeResult(CIProbeOutcome.passed, "checks_passed")
FAILED = CIProbeResult(CIProbeOutcome.failed, "checks_failed", details=f"run {RUN}")


async def _first_red_pass(rig: Rig) -> None:
    rig.red_first_attempt()
    await rig.sweep()


async def test_a_fresh_base_failure_is_rerun_once_and_delivered(
    db, db_dsn, monkeypatch
):
    rig = await rig_after_base_update(db, monkeypatch)
    armed = await rig.row()
    assert armed["state"] == "armed", "происхождение базы записано до красного CI"

    seen_by_other_connection: list[str] = []

    async def post(run_id, repo=None, gh_repo=None, forge=""):
        from hub.db import connect

        other = await connect(db_dsn)
        try:
            got = await other.execute_fetchall("SELECT state FROM delivery_reruns")
            seen_by_other_connection.extend(dict(r)["state"] for r in got)
        finally:
            await other.close()
        rig.start_attempt("2")
        return CIRunRequestResult(CIRunRequestOutcome.requested, "rerun_requested")

    rig.git.rerun_failed_jobs = AsyncMock(side_effect=post)
    await _first_red_pass(rig)

    assert rig.posts == 1
    assert seen_by_other_connection == ["reserved"], "бронь закоммичена до POST"
    row = await rig.row()
    assert (row["state"], row["run_id"], row["attempt_before"]) == (
        "requested",
        RUN,
        1,
    )
    assert row["head_sha"] == TIP and row["workflow"] == ".github/workflows/ci.yml"
    assert (await rig.task())["status"] == "running"
    assert "fresh_base_ci_rerun" in await rig.feed()
    assert not await rig.needs_decision()

    rig.git.check_pr_ci = AsyncMock(return_value=PENDING)
    await rig.sweep()
    await rig.sweep()
    assert rig.posts == 1, "второго POST нет, пока ждём"
    rig.git.merge_pr.assert_not_awaited()

    rig.start_attempt("2", status="completed", conclusion="success")
    rig.git.check_pr_ci = AsyncMock(return_value=PASSED)
    await rig.sweep()
    rig.git.merge_pr.assert_not_awaited()  # отчёта попытки 2 ещё нет

    await rig.report_attempt("2")
    await rig.sweep()

    assert rig.posts == 1
    assert rig.git.merge_pr.await_count == 1
    assert (await rig.task())["status"] == "completed"
    assert (await rig.row())["outcome"] == "rerun_green"


@pytest.mark.parametrize("probe", [PENDING, PASSED, FAILED], ids=lambda p: p.reason)
@pytest.mark.parametrize(
    "scenario",
    ["green_without_report", "next_attempt_started", "contract_changed", "old_report"],
)
async def test_only_the_current_rerun_attempt_report_allows_merge(
    db, monkeypatch, scenario, probe
):
    rig = await rig_after_base_update(db, monkeypatch)
    await _first_red_pass(rig)
    assert rig.posts == 1

    if scenario == "green_without_report":
        rig.start_attempt("2", status="completed", conclusion="success")
    elif scenario == "old_report":
        await rig.report_attempt("1", conclusion="failure")
        rig.start_attempt("2", status="completed", conclusion="success")
    else:
        rig.start_attempt("2", status="completed", conclusion="success")
        await rig.report_attempt("2")
    if scenario == "next_attempt_started":
        rig.start_attempt("3", status="in_progress")
    if scenario == "contract_changed":
        other = ci_contract.contract_from_raw(SAT_CONTRACT, "sat")
        monkeypatch.setattr(
            ci_contract, "contract_for_task", AsyncMock(return_value=other)
        )
        assert other.workflow_path == SAT_PATH

    rig.git.check_pr_ci = AsyncMock(return_value=probe)
    for _ in range(3):
        await rig.sweep()
        await services.merge_before_completion(db, await rig.task())

    rig.git.merge_pr.assert_not_awaited()
    assert rig.posts == 1
    assert (await rig.task())["status"] != "completed"


async def test_a_persisting_failure_reaches_the_owner_with_an_instruction(
    db, monkeypatch
):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.jobs[(RUN, "2")] = [
        {"name": "tests (3.12)", "conclusion": "failure"},
        {"name": "lint", "conclusion": "success"},
    ]
    rig.jobs[(RUN, "1")] = [{"name": "старая попытка", "conclusion": "failure"}]
    rig.next_attempt_status = "completed"
    await _first_red_pass(rig)
    rig.start_attempt("2", status="completed", conclusion="failure")
    rig.git.check_pr_ci = AsyncMock(return_value=FAILED)
    await rig.sweep()

    assert await rig.needs_decision()
    feed = await rig.feed()
    assert "fresh_base_ci_failed" in feed
    assert "tests (3.12)" in feed and "lint" not in feed.split("fresh_base")[-1]
    assert "старая попытка" not in feed, "имена - из новой попытки"
    assert "develop" in feed, "фактическая база проекта в образце"
    assert "decide" in feed and "rework" in feed
    assert "submit-for-review" in feed
    assert "перезапуск" in feed.lower(), "история перезапуска"
    for _ in range(3):
        ok, why = await services.merge_before_completion(db, await rig.task())
        assert not ok and why.startswith("fresh_base_ci_failed")
    assert rig.posts == 1, "второго POST нет никогда"


@pytest.mark.parametrize(
    "refusal",
    [
        CIRunRequestResult(CIRunRequestOutcome.declined, "forbidden", details="403"),
        CIRunRequestResult(CIRunRequestOutcome.declined, "not_rerunnable", "422"),
        CIRunRequestResult(CIRunRequestOutcome.unavailable, "network"),
    ],
    ids=["403", "422", "network"],
)
async def test_a_refused_post_names_jobs_of_the_original_attempt(
    db, monkeypatch, refusal
):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.post_results = [refusal]
    rig.jobs[(RUN, "1")] = [{"name": "types", "conclusion": "failure"}]
    await _first_red_pass(rig)

    assert await rig.needs_decision()
    feed = await rig.feed()
    assert "fresh_base_ci_failed" in feed and "types" in feed
    assert rig.posts == 1
    await rig.sweep()
    assert rig.posts == 1


async def test_an_unreadable_job_list_keeps_the_main_reason(db, monkeypatch):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.post_results = [CIRunRequestResult(CIRunRequestOutcome.declined, "no", "403")]
    rig.git.ci_attempt_jobs = AsyncMock(return_value=None)
    await _first_red_pass(rig)

    feed = await rig.feed()
    assert await rig.needs_decision()
    assert "fresh_base_ci_failed" in feed and "список не прочитан" in feed


async def _ineligible_off(rig: Rig) -> None:
    await set_policy(rig.db, **{POLICY_KEY: "off"})


async def _ineligible_forge(rig: Rig) -> None:
    await rig.db.execute("UPDATE projects SET forge='gitverse' WHERE slug='default'")
    await rig.db.commit()


async def _other_workflow_red(rig: Rig) -> None:
    rig.start_attempt("1", status="completed", conclusion="success")


async def _no_canonical_run(rig: Rig) -> None:
    rig.fake.runs.clear()


async def _read_failed(rig: Rig) -> None:
    rig.start_attempt("1", status="completed", conclusion="failure")
    rig.fake.down = True


async def _still_running(rig: Rig) -> None:
    rig.start_attempt("1", status="in_progress")


@pytest.mark.parametrize(
    "case",
    [
        _ineligible_off,
        _ineligible_forge,
        _other_workflow_red,
        _no_canonical_run,
        _read_failed,
        _still_running,
    ],
    ids=lambda f: f.__name__.strip("_"),
)
async def test_ineligible_or_unknown_cases_go_to_the_human(db, monkeypatch, case):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.red_first_attempt()
    await case(rig)
    await rig.sweep()

    assert rig.posts == 0
    assert await rig.needs_decision()
    assert "fresh_base_ci_failed" in await rig.feed()
    rig.git.merge_pr.assert_not_awaited()


async def test_off_keeps_the_text_and_the_path_as_before(db, monkeypatch):
    rig = await rig_after_base_update(db, monkeypatch, policy="off")
    assert await rig.rows() == [], "при off долговечной записи нет"
    rig.red_first_attempt()
    await rig.sweep()

    feed = await rig.feed()
    assert "fresh_base_ci_failed: CI красный на свежей базе — гейт слил базу" in feed
    assert "Авторская правка не менялась" in feed
    assert "Влить свежую" not in feed, "готовой инструкции при off нет"
    assert rig.posts == 0 and await rig.needs_decision()


async def test_a_satellite_needs_an_explicit_contract(db, monkeypatch):
    from hub import repository as repo
    from hub.services import delivery_rerun

    rig = await rig_after_base_update(db, monkeypatch)
    ci = FAILED
    for contract, expected_posts in ((None, 0), (SAT_CONTRACT, 1)):
        pid, tid = await satellite_task(
            db, slug=f"sat{expected_posts}", contract=contract
        )
        await repo.update_task(
            db,
            tid,
            pr_number=90 + expected_posts,
            review_verdict="approved",
            review_verdict_generation=1,
            branch=f"task-{tid}/x",
        )
        await repo.insert_event(
            db,
            kind="gate_branch_update",
            task_id=tid,
            actor="hub",
            payload={
                "pr": 90 + expected_posts,
                "from_sha": "sha-satellite",
                "generation": 1,
            },
        )
        await set_policy_of(db, pid, contract)
        rig.fake.attempt(
            tid,
            "head-sat",
            run_id="777",
            path=SAT_PATH,
            status="completed",
            conclusion="failure",
            event="pull_request",
        )
        rig.git.pr_head_sha = AsyncMock(return_value="head-sat")
        before = rig.posts
        task = dict(await repo.get_task(db, tid))
        await delivery_rerun.gate_step(db, task, {"gh_repo": None}, ci)
        assert rig.posts - before == expected_posts, contract


async def set_policy_of(db, project_id: int, contract: dict | None) -> None:
    policy = {POLICY_KEY: "on"}
    if contract is not None:
        policy["ci_contract"] = contract
    await db.execute(
        "UPDATE projects SET gate_policy=? WHERE id=?", (json.dumps(policy), project_id)
    )
    await db.commit()


async def test_a_rerun_that_never_lands_goes_to_the_human_after_the_wait(
    db, monkeypatch
):
    rig = await rig_after_base_update(db, monkeypatch)
    await _first_red_pass(rig)
    rig.git.check_pr_ci = AsyncMock(return_value=PENDING)
    await rig.sweep()
    assert not await rig.needs_decision()

    await rig.age_request(50)
    await rig.sweep()

    assert await rig.needs_decision()
    assert (await rig.row())["outcome"] == "timeout"
    assert rig.posts == 1


async def test_after_a_restart_an_unanswered_reservation_goes_to_the_human(
    db, monkeypatch
):
    rig = await rig_after_base_update(db, monkeypatch)
    await _first_red_pass(rig)
    from hub.services import delivery_rerun

    await db.execute("UPDATE delivery_reruns SET state='reserved'")
    await db.commit()
    rig.fake.runs.pop((RUN, "2"), None)
    delivery_rerun.forget_in_flight()  # рестарт: памяти о идущем POST нет

    await rig.sweep()

    assert await rig.needs_decision()
    assert rig.posts == 1, "второго POST после рестарта нет"
    assert (await rig.row())["outcome"] == "stalled_after_restart"


async def test_a_caller_inside_a_transaction_never_posts(db, monkeypatch):
    rig = await rig_after_base_update(db, monkeypatch)
    rig.red_first_attempt()
    await db.execute(
        "UPDATE tasks SET updated_at=updated_at WHERE id=?", (rig.task_id,)
    )
    assert db.in_transaction
    ok, why = await services.merge_before_completion(db, await rig.task())

    assert not ok and why.startswith("fresh_base_ci_failed")
    assert rig.posts == 0, "чужую транзакцию не коммитим, платного вызова нет"
