"""Tests for running AC-bound tests and recording results per generation (#507)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import stat
import time
from pathlib import Path

import pytest

from hub import repository as repo
from hub.models import AcceptanceCriterion
from hub.services.ac_tests import default_test_runner as real_default_test_runner
from tests.test_auth import run_routes_hub  # noqa: F401 - pytest fixture
from hub.services.ac_tests import (
    FAIL,
    NOT_FOUND,
    PASS,
    ac_tests_gap,
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


class _Stream:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk


class _FakeProc:
    def __init__(self, out: str, rc: int = 0):
        self.stdout = _Stream(out.encode())
        self.returncode = rc
        self.pid = 4_190_001  # not a real process: killing its group is a no-op

    async def wait(self):
        return self.returncode

    def kill(self):
        raise ProcessLookupError


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


# ---- silence from CI is unknown, never a failure (#546) ----


async def test_missing_ci_report_is_unknown_not_fail(db):
    # AC-3 (#546): when no run was reported for the code under review, the brief
    # says so WITH A CAUSE. It must not say the run failed: a false fail blocks a
    # verdict for a reason that has nothing to do with the work — the same
    # mistake #506 made when it read an unavailable environment as "no problem".
    from hub.services.ci_report import STATE_UNKNOWN, ci_report_state

    task_id = await _task_with_test_acs(db)

    # 1. Nothing pinned yet: there is no commit to compare a report against.
    state, reason = await ci_report_state(db, {"id": task_id, "submission_sha": ""})
    assert state == STATE_UNKNOWN
    assert "не закреплён" in reason and reason.strip(), "unknown must carry a cause"

    # 2. A commit is pinned and nobody reported it — still unknown, and the
    #    reason names the commit so the reader can go look for the run.
    state, reason = await ci_report_state(
        db, {"id": task_id, "submission_sha": "sha-under-review"}
    )
    assert state == STATE_UNKNOWN
    assert "sha-under-review"[:12] in reason

    # 3. CI reported ANOTHER commit. Still unknown, and the reason distinguishes
    #    "no evidence" from "evidence about different code".
    await repo.upsert_ci_run_report(
        db,
        task_id=task_id,
        head_sha="sha-some-other-run",
        ac_results="{}",
        validation_status="fail",
        validation_log="",
        reason="",
        reported_by="github-actions",
    )
    await db.commit()
    state, reason = await ci_report_state(
        db, {"id": task_id, "submission_sha": "sha-under-review"}
    )
    assert state == STATE_UNKNOWN
    assert "sha-some-other-run"[:12] in reason and "sha-under-review"[:12] in reason

    # And in none of the three cases was a fail recorded for the AC.
    rows = [dict(r) for r in await repo.list_ac_test_results(db, task_id)]
    assert rows == [], "absence of a report must never be stored as a result"


# ---- Three outcomes, three sentences (#1203) ----


async def test_gap_distinguishes_unsupported_runner_from_missing_test(db):
    # AC-3. Three different situations used to reach the author as at most two
    # sentences, and the third one — a perfectly good locator for a runner the
    # hub cannot run — was folded into "AC-тесты не зелёные". That sends
    # someone to fix a criterion that has nothing wrong with it (#419).
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
    await repo.bump_submission_generation(db, task_id)
    await repo.replace_acceptance_criteria(
        db,
        task_id,
        [
            # Runnable, but no result recorded for this generation.
            AcceptanceCriterion(
                id="AC-1",
                given="g",
                when="w",
                then="t",
                verifiable_by="test",
                test_ref="tests/test_x.py::test_a",
            ),
            # Not a locator at all.
            AcceptanceCriterion(
                id="AC-2",
                given="g",
                when="w",
                then="t",
                verifiable_by="test",
                test_ref="see the frontend suite",
            ),
            # A good locator this hub cannot run.
            AcceptanceCriterion(
                id="AC-3",
                given="g",
                when="w",
                then="t",
                verifiable_by="test",
                test_ref="src/lib/recent.test.ts::still toggles",
            ),
        ],
    )
    await db.commit()
    task = dict(await repo.get_task(db, task_id))

    gap = await ac_tests_gap(db, task)
    assert gap is not None
    parts = gap.split("; ")
    assert len(parts) == 3, gap

    by_ac = {ac: [p for p in parts if ac in p] for ac in ("AC-1", "AC-2", "AC-3")}
    # Each criterion is named in exactly one sentence: a criterion appearing in
    # two of them is the merge this criterion forbids.
    assert all(len(v) == 1 for v in by_ac.values()), gap

    runner_line = by_ac["AC-3"][0]
    assert "vitest" in runner_line
    # The author must be told there is nothing to fix in the locator itself,
    # which is precisely what the "не разрешается" sentence would imply.
    assert "не разрешается" not in runner_line
    assert "не зелёные" not in runner_line


async def test_unrunnable_locator_gets_no_recorded_result(db):
    # A recorded not_found is read as "the test is not there". A locator the
    # hub never handed to any runner has earned no such row.
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
    await repo.bump_submission_generation(db, task_id)
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
                test_ref="src/lib/recent.test.ts::still toggles",
            )
        ],
    )
    await db.commit()

    handed_to_runner: list[list[str]] = []

    async def _runner(nodeids, repo_path):
        handed_to_runner.append(list(nodeids))
        return {n: True for n in nodeids}

    recorded = await run_ac_tests(db, task_id, runner=_runner)
    assert recorded == []
    # And the runner was never called with it — not called and told "missing"
    # are different facts, and only one of them is true here.
    assert handed_to_runner == []


# ---- #1650: a manual AC run gets a minimal environment and dies as a group ----

_FAKE_UV = """#!/bin/sh
env > "$PWD/child-env.txt"
sleep 300 &
echo $! > "$PWD/child.pid"
wait
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.usefixtures("run_routes_hub")
async def test_manual_ac_run_has_a_minimal_env_and_kills_its_group(
    tmp_path: Path, monkeypatch, request
):
    """#1650 AC-4: no hub secrets reach the child, and the timeout kills the group.

    `uv` here is a stand-in script that records its environment and leaves a
    `sleep` running — the shape of a hung test with a child. A secret is named
    with and without TOKEN in it: the old filter let the second one through.
    """
    from hub.services import ac_tests

    hub = request.getfixturevalue("run_routes_hub")
    # Machines still get 403 (#1646): the route is a human's, so the run below
    # is only ever started by one.
    denied = await hub.client.post(
        f"/api/tasks/{hub.task_id}/run-ac-tests", headers=hub.keys["agent"]
    )
    assert denied.status_code == 403, denied.text

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "uv"
    fake.write_text(_FAKE_UV)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    work = tmp_path / "work"
    work.mkdir()
    real_home = os.environ.get("HOME", "")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("SYNTH_HUB_SIGNING_SALT", "synthetic-plain-secret")
    monkeypatch.setenv("SYNTH_API_TOKEN", "synthetic-token-secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/synthetic/agent.sock")
    monkeypatch.setattr(ac_tests, "_RUN_TIMEOUT", 1)

    try:
        # A runner that does not kill the group blocks on the pipe the child
        # holds: bound the wait so that is a red test, not a hung one.
        result = await asyncio.wait_for(
            real_default_test_runner(["tests/t.py::test_a"], str(work)), timeout=15
        )
        assert result is None, "a hung run is 'could not run', not a verdict"
        child_env = (work / "child-env.txt").read_text()
        for leaked in (
            "SYNTH_HUB_SIGNING_SALT",
            "SYNTH_API_TOKEN",
            "synthetic-plain-secret",
            "synthetic-token-secret",
            "SSH_AUTH_SOCK",
            "/synthetic/agent.sock",
        ):
            assert leaked not in child_env, leaked
        home_line = [ln for ln in child_env.splitlines() if ln.startswith("HOME=")]
        assert home_line and home_line[0] != f"HOME={real_home}"
        assert not Path(home_line[0][5:]).exists(), "the temporary HOME is removed"
        pid = int((work / "child.pid").read_text())
        deadline = time.monotonic() + 5
        while _alive(pid) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert not _alive(pid), "the process group survived the timeout"
    finally:
        pid_file = work / "child.pid"
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


async def test_manual_ac_run_streams_a_long_output(monkeypatch, tmp_path: Path):
    _fake_uv(
        tmp_path,
        "yes 'tests/t.py::test_a PASSED' | head -n 200000\n"
        "echo 'tests/t.py::test_b PASSED'\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")

    res = await real_default_test_runner(
        ["tests/t.py::test_a", "tests/t.py::test_b"], str(tmp_path)
    )

    assert res == {"tests/t.py::test_a": True, "tests/t.py::test_b": True}


# ---- #1650 round 2: a lost FAILED is not a pass; a group outlives its leader ----


def _fake_uv(tmp_path: Path, body: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "uv"
    fake.write_text("#!/bin/sh\n" + body)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)


async def test_a_failure_after_the_log_limit_still_fails_the_ac(
    monkeypatch, tmp_path: Path
):
    from hub.services import validation_run

    monkeypatch.setattr(validation_run, "_MAX_OUTPUT", 1000)
    _fake_uv(
        tmp_path,
        "yes 'noise noise noise noise' | head -c 3000000\n"
        "echo 'tests/t.py::test_a FAILED [100%]'\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")

    res = await real_default_test_runner(["tests/t.py::test_a"], str(tmp_path))

    assert res == {"tests/t.py::test_a": False}


async def test_incomplete_output_gives_no_positive_result(monkeypatch, tmp_path: Path):
    # A line longer than the buffer can hide an outcome: nothing may pass on it.
    _fake_uv(
        tmp_path,
        "echo 'tests/t.py::test_a PASSED'\n"
        "head -c 3000000 /dev/zero | tr '\\0' 'x'\n"
        "echo\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")

    res = await real_default_test_runner(["tests/t.py::test_a"], str(tmp_path))

    assert not res or not any(res.values()), res


_LAUNCHER = """sleep 300 &
echo $! > "$PWD/child.pid"
exit 0
"""


def _kill_quietly(work: Path) -> None:
    pid_file = work / "child.pid"
    if pid_file.exists():
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(pid_file.read_text()), signal.SIGKILL)


async def _alive_after(work: Path, seconds: float = 5) -> bool:
    deadline = time.monotonic() + seconds
    pid = int((work / "child.pid").read_text())
    while _alive(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    return _alive(pid)


async def test_the_group_dies_on_timeout_even_if_the_leader_already_exited(
    monkeypatch, tmp_path: Path
):
    from hub.services import ac_tests

    _fake_uv(tmp_path, _LAUNCHER)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    monkeypatch.setattr(ac_tests, "_RUN_TIMEOUT", 1)
    try:
        res = await asyncio.wait_for(
            real_default_test_runner(["tests/t.py::test_a"], str(tmp_path)), 15
        )
        assert res is None or not any(res.values())
        assert not await _alive_after(tmp_path), "the orphan outlived the run"
    finally:
        _kill_quietly(tmp_path)


async def test_the_group_dies_on_cancel_even_if_the_leader_already_exited(
    monkeypatch, tmp_path: Path
):
    _fake_uv(tmp_path, _LAUNCHER)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    try:
        task = asyncio.create_task(
            real_default_test_runner(["tests/t.py::test_a"], str(tmp_path))
        )
        for _ in range(100):
            if (tmp_path / "child.pid").exists():
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)  # the launcher has exited by now
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert not await _alive_after(tmp_path), "the orphan outlived the cancel"
    finally:
        _kill_quietly(tmp_path)
