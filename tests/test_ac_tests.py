"""Tests for running AC-bound tests and recording results per generation (#507)."""

from __future__ import annotations

from hub import repository as repo
from hub.models import AcceptanceCriterion
from hub.services.ac_tests import (
    FAIL,
    NOT_FOUND,
    PASS,
    current_ac_test_results,
    run_ac_tests,
)


async def _task_with_test_acs(db):
    task_id = await repo.create_task(
        db,
        title="t",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="running",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.bump_submission_generation(db, task_id)  # generation 1
    await repo.replace_acceptance_criteria(
        db,
        task_id,
        [
            AcceptanceCriterion(
                id="AC-1",
                given="g",
                when="w",
                then="t",
                verifiable_by="test",
                test_ref="tests/test_x.py::test_a",
            ),
            AcceptanceCriterion(
                id="AC-2",
                given="g",
                when="w",
                then="t",
                verifiable_by="test",
                test_ref="tests/test_x.py::test_b",
            ),
        ],
    )
    await db.commit()
    return task_id


async def test_run_ac_tests_records_on_current_generation(db):
    # AC-1 (#507): pass/fail recorded per AC, stamped with the current generation.
    task_id = await _task_with_test_acs(db)

    async def fake_runner(nodeids, repo_path):
        return {"tests/test_x.py::test_a": True, "tests/test_x.py::test_b": False}

    recorded = await run_ac_tests(db, task_id, runner=fake_runner)
    assert {r["ac_id"]: r["status"] for r in recorded} == {"AC-1": PASS, "AC-2": FAIL}
    rows = [dict(r) for r in await repo.list_ac_test_results(db, task_id)]
    assert {r["ac_id"]: r["submission_generation"] for r in rows} == {
        "AC-1": 1,
        "AC-2": 1,
    }


async def test_ac_results_go_stale_after_resubmission(db):
    # AC-2 (#507): a resubmission bumps the generation; old results are not current.
    task_id = await _task_with_test_acs(db)

    async def fake_runner(nodeids, repo_path):
        return {n: True for n in nodeids}

    await run_ac_tests(db, task_id, runner=fake_runner)
    await repo.bump_submission_generation(db, task_id)  # generation 2
    await db.commit()

    cur = current_ac_test_results(await repo.list_ac_test_results(db, task_id), 2)
    assert cur and all(r["is_current"] is False for r in cur)


async def test_run_ac_tests_not_found_when_runner_unavailable(db):
    # Best-effort: an unavailable runner records not_found, never a false fail.
    task_id = await _task_with_test_acs(db)

    async def none_runner(nodeids, repo_path):
        return None

    recorded = await run_ac_tests(db, task_id, runner=none_runner)
    assert all(r["status"] == NOT_FOUND for r in recorded)


# ---- default_test_runner output parsing (#507 machine-review HIGH) ----


class _FakeProc:
    def __init__(self, out: str, rc: int = 0):
        self._out = out.encode()
        self.returncode = rc

    async def communicate(self):
        return self._out, b""


async def _run_with_output(monkeypatch, nodeids, output):
    from hub.services.ac_tests import default_test_runner

    async def _fake_exec(*_a, **_kw):
        return _FakeProc(output)

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    return await default_test_runner(nodeids, "/repo")


async def test_runner_matches_exact_nodeid_not_prefix(monkeypatch):
    # HIGH (#507): substring matching let "::test_a" absorb the verdict of
    # "::test_a_extra" (and the last line won), flipping pass/fail.
    out = "tests/t.py::test_a PASSED   [ 50%]\ntests/t.py::test_a_extra FAILED [100%]\n"
    res = await _run_with_output(monkeypatch, ["tests/t.py::test_a"], out)
    assert res == {"tests/t.py::test_a": True}


async def test_runner_aggregates_parametrized_any_failure_fails(monkeypatch):
    # A bare locator covers every parametrized case; one red case fails the AC.
    out = "tests/t.py::test_p[c1] PASSED [ 50%]\ntests/t.py::test_p[c2] FAILED [100%]\n"
    res = await _run_with_output(monkeypatch, ["tests/t.py::test_p"], out)
    assert res == {"tests/t.py::test_p": False}


async def test_runner_reports_plain_pass_and_fail(monkeypatch):
    out = "tests/t.py::test_ok PASSED [ 50%]\ntests/t.py::test_bad FAILED [100%]\n"
    res = await _run_with_output(
        monkeypatch, ["tests/t.py::test_ok", "tests/t.py::test_bad"], out
    )
    assert res == {"tests/t.py::test_ok": True, "tests/t.py::test_bad": False}
