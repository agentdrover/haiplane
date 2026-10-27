"""The hub asks GitHub whether a CI report is true (#1687).

A report is evidence only when the run, the attempt and the artifact it names
exist on GitHub and say what the report says. These tests run against a fake
GitHub (tests/ci_trust_support.py); nothing here touches the network.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
from pathlib import Path

import pytest

from hub import repository as repo
from hub.models import AcceptanceCriterion, TaskSubmitReview
from hub.services import (
    ci_artifact,
    ci_contract,
    ci_green_proof,
    ci_trust,
    steward_evidence,
)
from hub.services.ci_report import accept_ci_run_report, adopt_ci_run_report
from tests.ci_trust_support import (
    GREEN_CHECKS,
    SAT_CHECKS,
    SAT_CONTRACT,
    SAT_CONTRACT_B,
    SAT_PATH,
    SAT_SHA,
    FakeGitHub,
    archive,
    install_fake_github,
    satellite_task,
    set_contract,
)

SHA = "sha-pinned"
GH: FakeGitHub = FakeGitHub()


@pytest.fixture(autouse=True)
def _github(monkeypatch):
    global GH
    GH = install_fake_github(monkeypatch)


async def _task(db, *, generation: int = 1, sha: str = SHA) -> int:
    task_id = await repo.create_task(
        db,
        title="Trust the run",
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
            )
        ],
    )
    for _ in range(generation):
        await repo.bump_submission_generation(db, task_id)
    if generation and sha:
        await repo.update_task(db, task_id, submission_sha=sha)
    await db.commit()
    return task_id


async def _post(db, task_id: int, sent: dict) -> dict:
    """What the intake route does with the body a trusted reporter sent."""
    return await accept_ci_run_report(db, task_id, **sent)


async def _standing(db, task_id: int, sha: str = SHA) -> tuple[str, str, str]:
    row = dict(await repo.get_ci_run_report(db, task_id, sha))
    return row["trust"], row["trust_reason"], row["attempt_key"]


async def _evidence(db, task_id: int) -> tuple[list[tuple[str, str]], object, object]:
    """AC results and the validation fields the gates read, as the task holds them."""
    results = sorted(
        (r["ac_id"], r["status"]) for r in await repo.list_ac_test_results(db, task_id)
    )
    task = dict(await repo.get_task(db, task_id))
    return results, task["validation_status"], task["validation_source"]


async def _gap(db, task_id: int, sha: str = SHA):
    rows = [dict(r) for r in await repo.list_acceptance_criteria(db, task_id)]
    report = await repo.get_ci_run_report(db, task_id, sha)
    contract = await ci_contract.contract_for_task(db, task_id)
    return ci_green_proof.proof_gap(
        dict(report) if report else None, rows, sha, contract
    )


# ---- AC-1: a report whose run does not check out ---------------------------


async def test_a_report_whose_run_does_not_check_out_is_unverified(db):
    broken = {}

    def nonexistent(sent):
        sent["provenance"]["run_id"] = "9999"

    def another_repository(sent):
        GH.runs[("5101", "1")]["repository"] = "other/rep"

    def another_path(sent):
        GH.runs[("5102", "1")]["path"] = ".github/workflows/other.yml"

    def another_commit(sent):
        GH.runs[("5103", "1")]["head_sha"] = "sha-someone-elses"

    def foreign_artifact(sent):
        sent["provenance"]["artifact_id"] = "424242"

    def wrong_digest(sent):
        sent["provenance"]["artifact_digest"] = "sha256:" + "0" * 64

    cases = {
        "run_not_found": ("5100", nonexistent),
        "wrong_repository": ("5101", another_repository),
        "wrong_workflow": ("5102", another_path),
        "wrong_commit": ("5103", another_commit),
        "artifact_mismatch": ("5104", foreign_artifact),
    }
    cases["artifact_mismatch#digest"] = ("5105", wrong_digest)
    for label, (run_id, tamper) in cases.items():
        task_id = await _task(db)
        sent = GH.attempt(task_id, SHA, run_id=run_id)
        tamper(sent)
        out = await _post(db, task_id, sent)
        reason = label.split("#")[0]
        assert out["trust"] == "unverified", label
        assert out["trust_reason"] == reason, (label, out["trust_reason"])
        assert out["applied"] is False, label
        # Nothing was written as proved, and the require gate refuses.
        results, validation, _ = await _evidence(db, task_id)
        assert results == [] and validation in (None, ""), label
        gap = await _gap(db, task_id)
        assert gap is not None and gap.cause == ci_green_proof.UNVERIFIED, label
        assert reason in gap.violations[0] or "не подтверждён" in gap.violations[0]
        broken[label] = out["trust_reason"]
    assert len(broken) == 6


# ---- AC-2: green needs a successful run and every required check ----------


async def test_green_proof_needs_a_successful_run_and_all_checks(db):
    # Control: a successful run with the four checks green IS a proof.
    ok = await _task(db)
    out = await _post(db, ok, GH.attempt(ok, SHA))
    assert out["trust"] == "verified" and out["applied"] is True
    assert await _gap(db, ok) is None

    # A verified run that ended in failure, whatever its checks say.
    failed = await _task(db)
    await _post(db, failed, GH.attempt(failed, SHA, conclusion="failure"))
    assert (await _standing(db, failed))[0] == "verified"
    assert (await _gap(db, failed)).cause == ci_green_proof.NOT_GREEN
    results, validation, _ = await _evidence(db, failed)
    assert ("AC-1", "pass") not in results and validation in (None, "")

    # Success with the `tests` key missing; success with four skips.
    for label, checks in (
        ("missing", {k: v for k, v in GREEN_CHECKS.items() if k != "tests"}),
        ("skipped", dict.fromkeys(GREEN_CHECKS, "skipped")),
    ):
        task_id = await _task(db)
        await _post(db, task_id, GH.attempt(task_id, SHA, checks=checks))
        gap = await _gap(db, task_id)
        assert gap is not None and gap.cause == ci_green_proof.NOT_GREEN, label

    # The artifact says validation failed; the POST says it passed.
    forged = await _task(db)
    sent = GH.attempt(forged, SHA, validation_status="fail")
    sent["validation_status"] = "pass"
    out = await _post(db, forged, sent)
    assert (out["trust"], out["trust_reason"]) == ("unverified", "body_mismatch")
    assert (await _gap(db, forged)).cause == ci_green_proof.UNVERIFIED

    # The steward and the autopilot read the same row: none of them sees a pass.
    for task_id in (failed, forged):
        fact = await steward_evidence._ci_fact(db, task_id, SHA)
        assert not fact.is_present or fact.value["passed"] is not True, fact
        row = await repo.get_ci_run_report(db, task_id, SHA)
        contract = await ci_contract.contract_for_task(db, task_id)
        assert not ci_trust.is_verified(
            dict(row), contract.fingerprint
        ) or ci_trust.green_gap(dict(row), contract)
    assert (await steward_evidence._ci_fact(db, ok, SHA)).value["passed"] is True


# ---- AC-3: the current attempt ---------------------------------------------


async def test_a_late_old_attempt_never_overwrites_a_newer_one(db):
    red = dict(
        conclusion="failure",
        validation_status="fail",
        ac_results={"AC-1": "fail"},
    )

    # 1. attempt 1 green, attempt 2 red, the POST of attempt 1 comes last.
    one = await _task(db)
    first = GH.attempt(one, SHA, run_id="6001", attempt="1")
    second = GH.attempt(one, SHA, run_id="6001", attempt="2", **red)
    await _post(db, one, second)
    out = await _post(db, one, first)
    assert out["trust"] == "verified"
    assert (await _standing(db, one))[2] == "ci:6001:2"
    results, validation, source = await _evidence(db, one)
    assert results == [("AC-1", "fail")] and validation == "fail"
    assert source == "ci:6001:2"
    assert (await _gap(db, one)).cause == ci_green_proof.RED
    kept = await repo.list_ci_report_attempts(db, one, SHA)
    assert {(r["run_id"], r["run_attempt"]) for r in kept} == {
        ("6001", "1"),
        ("6001", "2"),
    }, "the attempts are kept apart"

    # 2. the check of attempt 1 finishes after attempt 2 (GitHub was down).
    two = await _task(db)
    old = GH.attempt(two, SHA, run_id="6002", attempt="1")
    new = GH.attempt(two, SHA, run_id="6002", attempt="2", **red)
    GH.down = True
    await _post(db, two, old)
    GH.down = False
    await _post(db, two, new)
    await db.execute(
        "UPDATE ci_report_attempts SET checked_at = datetime('now', '-9 minutes')"
    )
    await ci_trust.sweep(db)
    states = {
        r["run_attempt"]: r["trust"]
        for r in await repo.list_ci_report_attempts(db, two, SHA)
    }
    assert states == {"1": "verified", "2": "verified"}
    assert (await _standing(db, two))[2] == "ci:6002:2", (
        "the late check changed nothing"
    )
    assert (await _evidence(db, two))[0] == [("AC-1", "fail")]

    # 3. attempt 2 has no artifact of its own.
    three = await _task(db)
    await _post(db, three, GH.attempt(three, SHA, run_id="6003", attempt="1"))
    assert (await _evidence(db, three))[0] == [("AC-1", "pass")]
    out = await _post(
        db, three, GH.attempt(three, SHA, run_id="6003", attempt="2", artifact=False)
    )
    assert (out["trust"], out["trust_reason"]) == (
        "unverified",
        "newer_attempt_without_report",
    )
    results, validation, _ = await _evidence(db, three)
    assert results == [] and validation in (None, ""), "attempt 1's evidence is gone"

    # 4. attempt 2 never sent a POST: only the API knows it.
    four = await _task(db)
    await _post(db, four, GH.attempt(four, SHA, run_id="6004", attempt="1"))
    GH.attempt(four, SHA, run_id="6004", attempt="2", **red)
    await ci_trust.refresh(db, four, SHA)
    assert (await _standing(db, four))[:2] == (
        "unverified",
        "newer_attempt_without_report",
    )
    assert (await _evidence(db, four))[0] == []
    assert (await _gap(db, four)).cause == ci_green_proof.UNVERIFIED

    # 5. run R1 is older than R2; R2 is green; then R1 is re-run (and fails).
    five = await _task(db)
    r1 = GH.attempt(five, SHA, run_id="6005", attempt="1")
    r2 = GH.attempt(five, SHA, run_id="6006", attempt="1")
    await _post(db, five, r1)
    await _post(db, five, r2)
    assert (await _standing(db, five))[2] == "ci:6006:1"
    assert (await _evidence(db, five))[0] == [("AC-1", "pass")]
    rerun = GH.attempt(five, SHA, run_id="6005", attempt="2", **red)
    await _post(db, five, rerun)
    assert (await _standing(db, five))[2] == "ci:6005:2"
    results, validation, source = await _evidence(db, five)
    assert results == [("AC-1", "fail")] and source == "ci:6005:2"
    assert (await _gap(db, five)) is not None, "require refuses after the rerun"
    # ... and the same rerun with no POST and no artifact at all.
    six = await _task(db)
    await _post(db, six, GH.attempt(six, SHA, run_id="6007", attempt="1"))
    await _post(db, six, GH.attempt(six, SHA, run_id="6008", attempt="1"))
    GH.attempt(six, SHA, run_id="6007", attempt="2", artifact=False)
    await ci_trust.refresh(db, six, SHA)
    assert (await _standing(db, six))[:2] == (
        "unverified",
        "newer_attempt_without_report",
    )
    assert (await _evidence(db, six))[0] == []
    assert (await _gap(db, six)) is not None

    # 6. a path with the documented @ref suffix is the canonical workflow.
    seven = await _task(db)
    out = await _post(
        db,
        seven,
        GH.attempt(seven, SHA, path=".github/workflows/ci.yml@refs/heads/task-1/x"),
    )
    assert out["trust"] == "verified", out["trust_reason"]


# ---- AC-4: GitHub unreachable ----------------------------------------------


async def test_an_unreachable_forge_is_retried_and_never_verifies_by_default(db):
    # Down at intake: unverified(forge_unavailable), nothing written.
    task_id = await _task(db)
    sent = GH.attempt(task_id, SHA)
    GH.down = True
    out = await _post(db, task_id, sent)
    assert (out["trust"], out["trust_reason"]) == ("unverified", "forge_unavailable")
    assert out["applied"] is False and (await _evidence(db, task_id))[0] == []

    # The poller does not hurry: inside five minutes nothing is asked again.
    GH.calls.clear()
    GH.down = False
    await ci_trust.sweep(db)
    assert GH.calls == [], "retried before the five minutes passed"

    # Five minutes on, GitHub is back: verified, evidence written.
    await db.execute(
        "UPDATE ci_report_attempts SET checked_at = datetime('now', '-6 minutes')"
    )
    await ci_trust.sweep(db)
    assert (await _standing(db, task_id))[:2] == ("verified", "")
    assert (await _evidence(db, task_id))[0] == [("AC-1", "pass")]

    # Down for good: after six hours the answer is final until a new report.
    late = await _task(db)
    sent = GH.attempt(late, SHA)
    GH.down = True
    await _post(db, late, sent)
    await db.execute(
        "UPDATE ci_report_attempts SET reported_at = datetime('now', '-7 hours'), "
        "checked_at = datetime('now', '-6 minutes') WHERE task_id = ?",
        (late,),
    )
    await ci_trust.sweep(db)
    row = (await repo.list_ci_report_attempts(db, late, SHA))[0]
    assert row["trust_final"] == 1 and row["trust_reason"] == "forge_unavailable"
    GH.down = False
    await db.execute(
        "UPDATE ci_report_attempts SET checked_at = datetime('now', '-6 minutes')"
    )
    await ci_trust.sweep(db)
    assert (await _standing(db, late))[:2] == ("unverified", "forge_unavailable")
    assert (await _evidence(db, late))[0] == []

    # A new report of the same attempt is a new claim, and is checked.
    again = GH.attempt(late, SHA, run_id="7777")
    out = await _post(db, late, again)
    assert out["trust"] == "verified"


# ---- AC-5: before and after the submission ---------------------------------


async def test_trust_is_the_same_before_and_after_submission(db):
    for label, tamper in (
        ("verified", None),
        ("unverified", lambda s: s.__setitem__("validation_status", "pass")),
    ):
        # After: the submission pinned the commit first, the report follows.
        after = await _task(db)
        sent_after = GH.attempt(after, SHA, validation_status="fail")
        if tamper:
            tamper(sent_after)
        await _post(db, after, sent_after)

        # Before: the report comes first, the submission adopts it.
        before = await _task(db, generation=0, sha="")
        sent_before = GH.attempt(before, SHA, validation_status="fail")
        if tamper:
            tamper(sent_before)
        await _post(db, before, sent_before)
        generation = await repo.bump_submission_generation(db, before)
        await repo.update_task(db, before, submission_sha=SHA)
        await ci_trust.refresh(db, before, SHA)
        await adopt_ci_run_report(db, before, SHA, generation)
        await db.commit()

        assert (await _standing(db, before))[:2] == (await _standing(db, after))[:2], (
            label
        )
        assert (await _evidence(db, before))[:2] == (await _evidence(db, after))[:2], (
            label
        )
        expected = [("AC-1", "pass")] if label == "verified" else []
        assert (await _evidence(db, after))[0] == expected, label


# ---- AC-6: as before for a verified report; legacy is not a pass -----------


async def _legacy_database(tmp_path):
    """A database as it stood before this task: reports and results, no trust."""
    from hub import db as db_module

    path = tmp_path / "old.db"
    fresh = await db_module.connect(str(path))
    await db_module.bootstrap(fresh)
    await fresh.execute("INSERT INTO tasks (title) VALUES ('legacy task')")
    await fresh.execute("INSERT INTO tasks (title) VALUES ('manual task')")
    await fresh.execute(
        "INSERT INTO ci_run_reports (task_id, head_sha, reported_by, validation_status) "
        "VALUES (1, 'old-sha', 'ci', 'pass')"
    )
    await fresh.execute(
        "INSERT INTO ac_test_results (task_id, ac_id, submission_generation, status) "
        "VALUES (1, 'AC-1', 1, 'pass'), (2, 'AC-1', 1, 'pass')"
    )
    await fresh.execute(
        "UPDATE tasks SET validation_generation=1, validation_status='pass' "
        "WHERE id IN (1, 2)"
    )
    await fresh.commit()
    await fresh.close()
    raw = sqlite3.connect(path)
    for table, column in (
        ("ci_run_reports", "trust"),
        ("ci_run_reports", "trust_reason"),
        ("ci_run_reports", "attempt_key"),
        ("ac_test_results", "source"),
        ("tasks", "validation_source"),
    ):
        raw.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    raw.execute("DROP TABLE ci_report_attempts")
    raw.execute(
        "DELETE FROM _migrations WHERE name IN ("
        "'add_ci_run_reports_trust', 'add_ci_run_reports_trust_reason', "
        "'add_ci_run_reports_attempt_key', 'create_ci_report_attempts', "
        "'add_ac_test_results_source', 'add_tasks_validation_source', "
        "'legacy_ac_test_results_source', 'legacy_tasks_validation_source')"
    )
    raw.commit()
    raw.close()
    return path


async def test_a_verified_report_behaves_as_before(db, tmp_path):
    from hub import db as db_module
    from hub.services.validation_run import validation_gap

    # A verified green report of an ordinary submission: the gate is satisfied,
    # the AC and the validation are recorded, the steward sees a pass.
    task_id = await _task(db)
    out = await _post(db, task_id, GH.attempt(task_id, SHA))
    assert out["applied"] is True and out["trust"] == "verified"
    assert await _gap(db, task_id) is None
    assert (await _evidence(db, task_id))[:2] == ([("AC-1", "pass")], "pass")
    task = dict(await repo.get_task(db, task_id))
    task["validation_commands"] = '["x"]'
    assert validation_gap(task) is None
    fact = await steward_evidence._ci_fact(db, task_id, SHA)
    assert fact.is_present and fact.value["passed"] is True
    state, reason = await ci_trust_report_state(db, task_id)
    assert (state, reason) == ("current", "")

    # Evidence written before the migration is unverified(legacy), not a pass.
    path = await _legacy_database(tmp_path)
    upgraded = await db_module.connect(str(path))
    try:
        await db_module.bootstrap(upgraded)
        await db_module.bootstrap(upgraded)  # twice: the migration is idempotent
        report = dict(await repo.get_ci_run_report(upgraded, 1, "old-sha"))
        assert (report["trust"], report["trust_reason"]) == ("unverified", "legacy")
        assert (
            ci_green_proof.proof_gap(
                report, [], "old-sha", ci_contract.HUB_CONTRACT
            ).cause
            == "unverified"
        )
        assert [dict(r) for r in await repo.list_ac_test_results(upgraded, 1)] == [], (
            "a legacy AC result is not read as a pass"
        )
        legacy_task = dict(await repo.get_task(upgraded, 1))
        legacy_task["validation_commands"] = '["x"]'
        assert "legacy" in (validation_gap(legacy_task) or "")
        # What a human or the hub itself recorded (no CI report on that task) stays.
        manual = [dict(r) for r in await repo.list_ac_test_results(upgraded, 2)]
        assert [(r["ac_id"], r["status"], r["source"]) for r in manual] == [
            ("AC-1", "pass", "manual")
        ]
        assert dict(await repo.get_task(upgraded, 2))["validation_source"] == "manual"
    finally:
        await upgraded.close()


async def ci_trust_report_state(db, task_id: int) -> tuple[str, str]:
    from hub.services.ci_report import ci_report_state

    task = dict(await repo.get_task(db, task_id))
    return await ci_report_state(db, task)


# ---- the hub reads the artifact the way the reporter does ------------------


def test_the_hubs_artifact_limits_are_the_reporters():
    spec = importlib.util.spec_from_file_location(
        "ci_trusted_report",
        Path(__file__).resolve().parent.parent / "scripts" / "ci_trusted_report.py",
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    for name in (
        "RESULTS_MEMBER",
        "AUDIT_MEMBER",
        "ALLOWED_MEMBERS",
        "MAX_ARCHIVE_BYTES",
        "MAX_MEMBERS",
        "MAX_UNPACKED_BYTES",
        "MAX_RESULTS_BYTES",
        "RESULTS_SCHEMA",
        "AC_STATUSES",
        "CHECK_STATUSES",
        "VALIDATION_STATUSES",
        "RESULT_KEYS",
    ):
        assert getattr(ci_artifact, name) == getattr(script, name), name
    assert ci_artifact.LOG_MAX == script._LOG_MAX
    assert ci_artifact.REASON_MAX == script._REASON_MAX
    assert ci_artifact.BLOCK_MAX_CHARS == script._BLOCK_MAX_CHARS


def test_the_artifact_is_read_as_data_and_never_extracted(tmp_path):
    good = json.dumps({"schema": 1, "task_id": 1, "head_sha": "s"}).encode()
    members = ci_artifact.read_archive(archive({"results.json": good}))
    assert members == {"results.json": good}
    for name in ("../results.json", "/etc/passwd", "sub/results.json", "extra.sh"):
        with pytest.raises(ci_artifact.ArtifactRefused):
            ci_artifact.read_archive(archive({name: b"x"}))
    with pytest.raises(ci_artifact.ArtifactRefused):
        ci_artifact.read_archive(b"not a zip")
    with pytest.raises(ci_artifact.ArtifactRefused):
        ci_artifact.read_archive(b"\0" * (ci_artifact.MAX_ARCHIVE_BYTES + 1))
    with pytest.raises(ci_artifact.ArtifactRefused):
        ci_artifact.expected_body(
            archive({"results.json": good}), head_sha="other", task_id=1, submitted={}
        )
    assert list(tmp_path.iterdir()) == [], "nothing is written to disk"


# ---- the forge adapter ------------------------------------------------------


async def test_the_github_adapter_reads_runs_artifacts_and_the_archive(monkeypatch):
    from hub.integrations import proc
    from hub.integrations.forge import github as gh_module

    seen: list[tuple[str, ...]] = []
    answers: dict[str, tuple[int, str, str]] = {}

    async def fake_gh(*args, repo=None, **kw):
        seen.append(args)
        return answers[args[1].split("?")[0]]

    run = {
        "id": 5001,
        "run_attempt": 2,
        "path": ".github/workflows/ci.yml@refs/heads/x",
        "head_sha": "abc",
        "status": "completed",
        "conclusion": "success",
        "event": "workflow_dispatch",
        "run_started_at": "2026-10-12T10:00:00Z",
        "repository": {"full_name": "own/rep"},
        "head_repository": {"full_name": "own/rep"},
    }
    answers["repos/own/rep/actions/runs/5001/attempts/2"] = (0, json.dumps(run), "")
    answers["repos/own/rep/actions/runs/5002/attempts/1"] = (
        1,
        "",
        "HTTP 404: Not Found",
    )
    answers["repos/own/rep/actions/runs/5003/attempts/1"] = (
        1,
        "",
        "HTTP 502: bad gateway",
    )
    answers["repos/own/rep/actions/workflows/ci.yml/runs"] = (
        0,
        json.dumps({"workflow_runs": [run]}),
        "",
    )
    answers["repos/own/rep/actions/runs/5001/artifacts"] = (
        0,
        json.dumps(
            {
                "artifacts": [
                    {
                        "id": 7,
                        "name": "hub-ci-results-2",
                        "digest": "sha256:ab",
                        "size_in_bytes": 10,
                        "expired": False,
                    }
                ]
            }
        ),
        "",
    )
    monkeypatch.setattr(gh_module, "_gh", fake_gh)
    forge = gh_module.GitHubForge()

    found = await forge.ci_run_attempt("5001", "2", gh_repo="own/rep")
    assert found["path"].startswith(".github/workflows/ci.yml")
    assert found["repository"] == "own/rep" and found["run_attempt"] == "2"
    assert await forge.ci_run_attempt("5002", "1", gh_repo="own/rep") == {}
    assert await forge.ci_run_attempt("5003", "1", gh_repo="own/rep") is None
    assert await forge.ci_run_attempt("not-a-number", "1", gh_repo="own/rep") == {}
    runs = await forge.ci_runs_for_sha("abc", gh_repo="own/rep")
    assert [r["run_id"] for r in runs] == ["5001"]
    listing = await forge.ci_run_artifacts("5001", gh_repo="own/rep")
    assert listing == [
        {
            "id": "7",
            "name": "hub-ci-results-2",
            "digest": "sha256:ab",
            "size": 10,
            "expired": False,
        }
    ]

    calls: list[tuple] = []

    async def fake_capped(*cmd, cwd=None, timeout=60, max_bytes, max_stderr=0):
        calls.append(cmd)
        return {
            "7": (0, b"PK-bytes", ""),
            "8": (-2, b"", "output longer"),
            "9": (1, b"", "HTTP 410: Gone"),
            "10": (1, b"", "connection reset"),
        }[cmd[2].split("/")[-2]]

    monkeypatch.setattr(proc, "run_capped", fake_capped)
    assert (
        await forge.ci_artifact_zip("7", max_bytes=5, gh_repo="own/rep") == b"PK-bytes"
    )
    assert await forge.ci_artifact_zip("8", max_bytes=5, gh_repo="own/rep") == b""
    assert await forge.ci_artifact_zip("9", max_bytes=5, gh_repo="own/rep") == b""
    assert await forge.ci_artifact_zip("10", max_bytes=5, gh_repo="own/rep") is None
    assert calls[0][:2] == (gh_module.GH_BIN, "api")
    assert calls[0][2] == "repos/own/rep/actions/artifacts/7/zip"


async def test_the_github_adapter_asks_for_the_workflow_of_the_contract(monkeypatch):
    from hub.integrations.forge import github as gh_module
    from hub.integrations.noop import NoopGitOps

    seen: list[str] = []

    async def fake_gh(*args, repo=None, **kw):
        seen.append(args[1].split("?")[0])
        return 0, json.dumps({"workflow_runs": []}), ""

    monkeypatch.setattr(gh_module, "_gh", fake_gh)
    forge = gh_module.GitHubForge()
    assert await forge.ci_runs_for_sha("abc", gh_repo="own/rep") == []
    assert (
        await forge.ci_runs_for_sha("abc", gh_repo="own/rep", workflow_path=SAT_PATH)
        == []
    )
    assert seen == [
        "repos/own/rep/actions/workflows/ci.yml/runs",
        "repos/own/rep/actions/workflows/tests.yml/runs",
    ]
    # A path that is not a workflow file is no question for GitHub: unknown, not "no runs".
    for bad in (
        "tests.yml",
        ".github/workflows/../x.yml",
        ".github/workflows/a/b.yml",
        ".github/workflows/x.yml?a=b",
    ):
        assert (
            await forge.ci_runs_for_sha("abc", gh_repo="own/rep", workflow_path=bad)
            is None
        )
    assert len(seen) == 2
    # Without a forge nothing is visible: "not supported" is None, never "no runs".
    assert await NoopGitOps().ci_runs_for_sha("abc", workflow_path=SAT_PATH) is None


async def test_the_noop_and_gitverse_forges_cannot_ask_and_say_so():
    from hub.integrations.forge.gitverse import GitVerseForge
    from hub.integrations.noop import NoopForge, NoopGitOps

    for forge in (NoopForge(), NoopGitOps(), GitVerseForge.__new__(GitVerseForge)):
        assert await forge.ci_run_attempt("1", "1") is None
        assert await forge.ci_runs_for_sha("abc") is None
        assert await forge.ci_run_artifacts("1") is None
        kwargs = {"max_bytes": 5}
        assert await forge.ci_artifact_zip("1", **kwargs) is None


def test_the_mcp_brief_says_when_the_ci_report_is_not_confirmed():
    from hub.mcp_server import _brief_evidence_lines

    shown = _brief_evidence_lines(
        {"ci_run_report": {"trust": "unverified", "trust_reason": "body_mismatch"}}
    )
    assert any("CI-отчёт не подтверждён: body_mismatch" in line for line in shown)
    confirmed = _brief_evidence_lines(
        {"ci_run_report": {"trust": "verified", "trust_reason": ""}}
    )
    assert not any("не подтверждён" in line for line in confirmed)
    assert not any("не подтверждён" in line for line in _brief_evidence_lines({}))


async def test_the_poller_rereads_the_current_attempt_of_tasks_in_review(db):
    """A rerun nobody reported changes the standing with no POST arriving."""
    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="6101"))
    assert (await _evidence(db, task_id))[0] == [("AC-1", "pass")]
    await repo.update_task(db, task_id, status="review")
    await db.commit()

    GH.attempt(task_id, SHA, run_id="6101", attempt="2", artifact=False)
    await ci_trust.sweep(db)
    assert (await _standing(db, task_id))[:2] == (
        "unverified",
        "newer_attempt_without_report",
    )
    assert (await _evidence(db, task_id))[0] == []

    # A task that is not in review is not polled for this.
    other = await _task(db)
    await _post(db, other, GH.attempt(other, SHA, run_id="6102"))
    GH.attempt(other, SHA, run_id="6102", attempt="2", artifact=False)
    await ci_trust.sweep(db)
    assert (await _standing(db, other))[0] == "verified"


# ---- review round: races, outages, readers, pagination, budget -------------


async def test_a_body_replaced_while_it_is_checked_is_checked_again(db, monkeypatch):
    """The verdict belongs to the body it was computed for, not to the row id."""
    from hub.integrations.registry import plugins
    from hub.services.ci_report import store_ci_run_report

    task_id = await _task(db)
    honest = GH.attempt(
        task_id, SHA, validation_status="fail", ac_results={"AC-1": "fail"}
    )
    forged = {**honest, "validation_status": "pass", "ac_results": {"AC-1": "pass"}}
    await store_ci_run_report(db, task_id, **honest)
    await db.commit()

    real_zip = GH.ci_artifact_zip
    replaced: list[int] = []

    async def zip_then_replace(*args, **kwargs):
        blob = await real_zip(*args, **kwargs)
        if not replaced:  # the repeated POST arrives while the archive is read
            replaced.append(1)
            await store_ci_run_report(db, task_id, **forged)
            await db.commit()
        return blob

    monkeypatch.setattr(plugins.git_ops, "ci_artifact_zip", zip_then_replace)
    standing = await ci_trust.refresh(db, task_id, SHA)

    assert replaced == [1]
    assert (standing.trust, standing.reason) == ("unverified", "body_mismatch")
    assert (await _standing(db, task_id))[:2] == ("unverified", "body_mismatch")
    assert ("AC-1", "pass") not in (await _evidence(db, task_id))[0]
    assert (await _gap(db, task_id)).cause == ci_green_proof.UNVERIFIED


async def test_overlapping_refreshes_never_restore_an_older_picture(db, monkeypatch):
    import asyncio

    from hub.integrations.registry import plugins
    from hub.services.ci_report import store_ci_run_report

    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="6201", attempt="1"))
    entered, release = asyncio.Event(), asyncio.Event()
    real_runs = GH.ci_runs_for_sha
    seen: list[int] = []

    async def slow_first_runs(*args, **kwargs):
        picture = await real_runs(*args, **kwargs)  # the world as it is NOW
        seen.append(1)
        if len(seen) == 1:
            entered.set()
            await release.wait()  # ... and the answer is slow to be used
        return picture

    monkeypatch.setattr(plugins.git_ops, "ci_runs_for_sha", slow_first_runs)
    older = asyncio.create_task(ci_trust.refresh(db, task_id, SHA))
    await entered.wait()
    rerun = GH.attempt(
        task_id,
        SHA,
        run_id="6201",
        attempt="2",
        conclusion="failure",
        validation_status="fail",
        ac_results={"AC-1": "fail"},
    )
    await store_ci_run_report(db, task_id, **rerun)
    await db.commit()
    newer = asyncio.create_task(ci_trust.refresh(db, task_id, SHA))
    await asyncio.sleep(0.05)  # the second refresh has reached the first one's lock
    release.set()
    await asyncio.gather(older, newer)

    assert (await _standing(db, task_id))[2] == "ci:6201:2"
    results, validation, source = await _evidence(db, task_id)
    assert results == [("AC-1", "fail")] and source == "ci:6201:2"


async def test_without_a_current_confirmation_nothing_stays_proved(db, monkeypatch):
    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="6301"))
    assert await _gap(db, task_id) is None
    assert (await _evidence(db, task_id))[0] == [("AC-1", "pass")]

    # GitHub cannot be asked: the old green is history, not proof.
    GH.down = True
    out = await ci_trust.refresh(db, task_id, SHA)
    assert out.forge_down is True
    assert (await _standing(db, task_id))[:2] == ("unverified", "forge_unavailable")
    assert (await _evidence(db, task_id))[0] == []
    assert (await _gap(db, task_id)).cause == ci_green_proof.UNVERIFIED
    held = await repo.list_ci_report_attempts(db, task_id, SHA)
    assert held[0]["trust"] == "verified", "the past confirmation is kept as history"

    # It answers again: the history is reused, nothing is downloaded twice.
    GH.down = False
    GH.calls.clear()
    await ci_trust.refresh(db, task_id, SHA)
    assert (await _standing(db, task_id))[:2] == ("verified", "")
    assert (await _evidence(db, task_id))[0] == [("AC-1", "pass")]
    assert not any(c.startswith("zip:") for c in GH.calls)

    # It answers with a list that does not hold the reported run.
    GH.runs.clear()
    await ci_trust.refresh(db, task_id, SHA)
    assert (await _standing(db, task_id))[:2] == (
        "unverified",
        "current_attempt_unknown",
    )
    assert (await _evidence(db, task_id))[0] == []


async def test_a_broken_check_fails_closed(db, monkeypatch):
    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA))
    assert (await _evidence(db, task_id))[0] == [("AC-1", "pass")]

    async def broken(*args, **kwargs):
        raise RuntimeError("the check itself broke")

    monkeypatch.setattr(ci_trust, "_refresh_locked", broken)
    assert await ci_trust.refresh_quietly(db, task_id, SHA) is None
    assert (await _standing(db, task_id))[:2] == ("unverified", "check_failed")
    assert (await _evidence(db, task_id))[0] == []
    assert (await _gap(db, task_id)).cause == ci_green_proof.UNVERIFIED


async def _task_whose_rerun_nobody_reported(db, run_id: str) -> int:
    """Confirmed green is stored; a newer attempt is running and sent nothing."""
    task_id = await _task(db)
    out = await _post(db, task_id, GH.attempt(task_id, SHA, run_id=run_id, attempt="1"))
    assert out["trust"] == "verified"
    GH.attempt(
        task_id, SHA, run_id=run_id, attempt="2", status="in_progress", artifact=False
    )
    return task_id


async def test_readers_ask_which_attempt_is_current_before_they_read(db):
    from hub.services.ac_tests import ac_tests_gap
    from hub.services.review_brief import build_review_brief
    from hub.services.steward_evidence import build_evidence_packet
    from hub.services.validation_run import validation_gap

    # The gates: the AC gap and the validation gap read the task AFTER the ask.
    gate = await _task_whose_rerun_nobody_reported(db, "6401")
    assert await ac_tests_gap(db, dict(await repo.get_task(db, gate))) is None
    task = await ci_trust.refresh_for_gate(db, dict(await repo.get_task(db, gate)))
    assert await ac_tests_gap(db, task)
    task["validation_commands"] = '["x"]'
    assert validation_gap(task)

    # The brief: the pre-pass is not "covered" by the attempt that is gone.
    brief_task = await _task_whose_rerun_nobody_reported(db, "6402")
    brief = await build_review_brief(db, brief_task)
    assert brief.prepass.state != "covered"
    assert brief.ci_run_report.state == "unknown"
    assert brief.ac_test_results == []

    # The steward packet reads the CI fact of the current attempt too.
    packet_task = await _task_whose_rerun_nobody_reported(db, "6403")
    packet = await build_evidence_packet(db, packet_task)
    assert packet.fact("ci_pinned_sha").is_absent


async def test_the_steward_packet_asks_before_it_builds_the_brief(db, monkeypatch):
    from hub.services import review_brief
    from hub.services.steward_evidence import build_evidence_packet

    task_id = await _task_whose_rerun_nobody_reported(db, "6404")
    order: list[str] = []
    real_refresh = ci_trust.refresh_quietly
    real_brief = review_brief.build_review_brief

    async def refresh_spy(*args, **kwargs):
        order.append("refresh")
        return await real_refresh(*args, **kwargs)

    async def brief_spy(*args, **kwargs):
        order.append("brief")
        return await real_brief(*args, **kwargs)

    monkeypatch.setattr(ci_trust, "refresh_quietly", refresh_spy)
    monkeypatch.setattr(review_brief, "build_review_brief", brief_spy)
    await build_evidence_packet(db, task_id)
    assert order[0] == "refresh" and "brief" in order, order


async def test_a_run_that_failed_is_not_a_checked_submission(db):
    from hub.services import review_evidence

    task_id = await _task(db)
    await _post(
        db,
        task_id,
        GH.attempt(task_id, SHA, conclusion="failure", validation_status="pass"),
    )
    task = dict(await repo.get_task(db, task_id))
    prepass = await review_evidence.prepass_state(db, task)
    assert prepass.state == "failed" and "прогон" in prepass.failed
    assert prepass.passed == ["format", "lint", "tests", "types"], "steps stay listed"
    standing = review_evidence.validation_standing(prepass, "")
    assert standing.verified is False
    assert standing.state == review_evidence.VALIDATION_FAILED

    ok = await _task(db)
    await _post(db, ok, GH.attempt(ok, SHA))
    covered = await review_evidence.prepass_state(db, dict(await repo.get_task(db, ok)))
    assert covered.state == "covered"


# ---- the migration: a report that names its run is checked, not buried ----


def _owner_count_query() -> str:
    """The query deploy/CD.md hands the owner for prod, taken from the runbook itself."""
    text = (Path(__file__).resolve().parent.parent / "deploy" / "CD.md").read_text(
        encoding="utf-8"
    )
    match = re.search(r"```\n(SELECT t\.status,.*?;)\n```", text, re.S)
    assert match, "the runbook no longer carries the count query"
    return match.group(1)


async def test_a_report_that_names_its_run_is_queued_by_the_migration(tmp_path):
    from hub import db as db_module

    sent = GH.attempt(1, "prov-sha", run_id="6501", ac_results={"AC-1": "pass"})
    path = tmp_path / "old.db"
    fresh = await db_module.connect(str(path))
    await db_module.bootstrap(fresh)
    for title in ("names its run", "does not", "ran out of run"):
        await fresh.execute("INSERT INTO tasks (title) VALUES (?)", (title,))
    rows = (
        (1, "prov-sha", json.dumps(sent["provenance"])),
        (2, "bare-sha", "{}"),
        (3, "half-sha", json.dumps({"run_id": "1", "run_attempt": "1"})),
    )
    for task_id, sha, provenance in rows:
        await fresh.execute(
            "INSERT INTO ci_run_reports (task_id, head_sha, ac_results, checks, "
            "validation_status, provenance, reported_by) VALUES (?, ?, ?, ?, ?, ?, 'ci')",
            (
                task_id,
                sha,
                json.dumps(sent["ac_results"]),
                json.dumps(sent["checks"]),
                "pass",
                provenance,
            ),
        )
    await fresh.commit()
    await fresh.close()
    raw = sqlite3.connect(path)
    for table, column in (
        ("ci_run_reports", "trust"),
        ("ci_run_reports", "trust_reason"),
        ("ci_run_reports", "attempt_key"),
        ("ac_test_results", "source"),
        ("tasks", "validation_source"),
    ):
        raw.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    raw.execute("DROP TABLE ci_report_attempts")
    raw.execute(
        "DELETE FROM _migrations WHERE name LIKE '%ci_report_attempts%' "
        "OR name IN ('add_ci_run_reports_trust', 'add_ci_run_reports_trust_reason', "
        "'add_ci_run_reports_attempt_key', 'add_ac_test_results_source', "
        "'add_tasks_validation_source', 'legacy_ac_test_results_source', "
        "'legacy_tasks_validation_source', 'queue_provenanced_reports_for_verification')"
    )
    raw.commit()
    raw.close()

    from hub import db as db_module

    upgraded = await db_module.connect(str(path))
    try:
        await db_module.bootstrap(upgraded)
        await db_module.bootstrap(upgraded)  # twice: no second queue entry
        queued = [
            (r["task_id"], r["trust_final"], r["checked_at"])
            for r in await upgraded.execute_fetchall(
                "SELECT task_id, trust_final, checked_at FROM ci_report_attempts"
            )
        ]
        assert queued == [(1, 0, None)], "only the report that names run AND artifact"
        legacy = {
            r["task_id"]: r["trust_reason"]
            for r in await upgraded.execute_fetchall(
                "SELECT task_id, trust_reason FROM ci_run_reports"
            )
        }
        assert legacy == {1: "legacy", 2: "legacy", 3: "legacy"}

        # The query the owner runs on prod: tasks in work whose pinned report
        # names its run, i.e. the ones this migration queues.
        await upgraded.execute(
            "UPDATE tasks SET status='review', submission_sha='prov-sha' WHERE id=1"
        )
        await upgraded.execute(
            "UPDATE tasks SET status='running', submission_sha='bare-sha' WHERE id=2"
        )
        await upgraded.commit()
        counted = sorted(
            tuple(r) for r in await upgraded.execute_fetchall(_owner_count_query())
        )
        assert counted == [("review", 1, 0), ("running", 0, 1)]

        # The poller picks it up (never checked) and GitHub confirms it.
        await ci_trust.sweep(upgraded)
        row = dict(await repo.get_ci_run_report(upgraded, 1, "prov-sha"))
        assert (row["trust"], row["trust_reason"]) == ("verified", "")
        bare = dict(await repo.get_ci_run_report(upgraded, 2, "bare-sha"))
        assert bare["trust_reason"] == "legacy"
    finally:
        await upgraded.close()


# ---- the adapter reads every page, or says it does not know ---------------


async def test_a_list_with_a_hidden_next_page_is_unknown_not_short(monkeypatch):
    from hub.integrations.forge import github as gh_module

    pages: dict[int, tuple[int, str, str]] = {}
    asked: list[int] = []

    async def fake_gh(*args, repo=None, **kw):
        page = int(re.search(r"[&?]page=(\d+)", args[1]).group(1))
        asked.append(page)
        return pages[page]

    def run(n: int) -> dict:
        return {"id": n, "run_attempt": 1, "path": ".github/workflows/ci.yml"}

    def listing(first: int, count: int, total: int) -> tuple[int, str, str]:
        body = {
            "total_count": total,
            "workflow_runs": [run(i) for i in range(first, first + count)],
        }
        return 0, json.dumps(body), ""

    monkeypatch.setattr(gh_module, "_gh", fake_gh)
    forge = gh_module.GitHubForge()

    pages.update({1: listing(0, 100, 130), 2: listing(100, 30, 130)})
    runs = await forge.ci_runs_for_sha("abc", gh_repo="own/rep")
    assert len(runs) == 130 and asked == [1, 2], "the second page is read"

    asked.clear()
    pages[2] = (1, "", "HTTP 502: bad gateway")  # the next page cannot be read
    assert await forge.ci_runs_for_sha("abc", gh_repo="own/rep") is None

    pages[2] = listing(100, 10, 130)  # GitHub counts more than it handed over
    assert await forge.ci_runs_for_sha("abc", gh_repo="own/rep") is None

    asked.clear()  # a list that never ends within the pages we read
    for n in range(1, 12):
        pages[n] = listing((n - 1) * 100, 100, 5000)
    assert await forge.ci_runs_for_sha("abc", gh_repo="own/rep") is None
    assert max(asked) == 10

    art = {
        "total_count": 120,
        "artifacts": [{"id": i, "name": "n"} for i in range(100)],
    }
    pages.clear()
    pages[1] = 0, json.dumps(art), ""
    pages[2] = 1, "", "HTTP 500"
    assert await forge.ci_run_artifacts("7", gh_repo="own/rep") is None


# ---- the poller is not held hostage by GitHub ------------------------------


async def _review_task_with_green(db, run_id: str) -> int:
    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id=run_id))
    await repo.update_task(db, task_id, status="review")
    await db.commit()
    return task_id


async def test_a_github_outage_costs_the_tick_one_failed_call(db):
    first = await _review_task_with_green(db, "6601")
    second = await _review_task_with_green(db, "6602")
    GH.calls.clear()
    GH.down = True
    await ci_trust.sweep(db)
    # The first task found GitHub down; the second was not sent to find out.
    assert ci_trust.forge_recently_down() is True
    asked = [c for c in GH.calls if c.startswith("runs:")]
    assert len(asked) == 1, GH.calls
    for task_id in (first, second):
        assert (await _standing(db, task_id))[:2] == ("unverified", "forge_unavailable")
        assert (await _evidence(db, task_id))[0] == []

    # Inside the backoff nothing is asked at all.
    GH.calls.clear()
    await ci_trust.sweep(db)
    assert GH.calls == []


async def test_a_tick_stops_starting_checks_when_its_budget_is_spent(db, monkeypatch):
    first = await _review_task_with_green(db, "6701")
    second = await _review_task_with_green(db, "6702")
    monkeypatch.setattr(ci_trust, "SWEEP_BUDGET_SECONDS", -1.0)
    GH.calls.clear()
    await ci_trust.sweep(db)
    assert GH.calls == [], "no check was started"
    # Deferred, not downgraded: nothing was learned, nothing is claimed.
    assert (await _standing(db, first))[0] == "verified"
    assert (await _standing(db, second))[0] == "verified"


# ---- the metric does not count what GitHub did not confirm -----------------


def test_the_economy_metric_counts_only_a_confirmed_green_run():
    from hub.services.review_economy import _ci_row

    hub = ci_contract.HUB_CONTRACT
    ok = {
        "trust": "verified",
        "contract_fp": hub.fingerprint,
        "checks": json.dumps(GREEN_CHECKS),
        "provenance": json.dumps({"run_conclusion": "success"}),
    }
    assert _ci_row(True, "pass", ok, hub) == "green"
    assert _ci_row(True, "fail", ok, hub) == "red"
    assert _ci_row(True, "pass", {**ok, "trust": "unverified"}, hub) == "undetermined"
    assert _ci_row(True, "fail", {**ok, "trust": "unverified"}, hub) == "undetermined"
    failed_run = {**ok, "provenance": json.dumps({"run_conclusion": "failure"})}
    assert _ci_row(True, "pass", failed_run, hub) == "undetermined"
    assert _ci_row(False, None, None, hub) == "no_report"
    # A verdict made under another contract is no confirmation (#1848).
    assert _ci_row(True, "pass", {**ok, "contract_fp": "x"}, hub) == "undetermined"


async def test_an_approving_verdict_asks_before_it_reads_the_ac_results(
    db, monkeypatch
):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from hub import config
    from hub.services.lifecycle import _vstep_ac_tests_green

    monkeypatch.setattr(config, "SDD_AC_TESTS", "require")
    task_id = await _task_whose_rerun_nobody_reported(db, "6405")
    task = dict(await repo.get_task(db, task_id))
    state = SimpleNamespace(
        db=db,
        task_id=task_id,
        task=task,
        body=SimpleNamespace(verdict=SimpleNamespace(value="approved")),
    )
    with pytest.raises(HTTPException) as refused:
        await _vstep_ac_tests_green(state)
    assert refused.value.status_code == 422
    assert "ac_tests_not_green" in str(refused.value.detail)


# ---- round 2: the database decides, not the process ------------------------


async def _slow_first_runs(monkeypatch):
    """The first answer about the commit's runs is taken now and used later."""
    import asyncio

    from hub.integrations.registry import plugins

    entered, release = asyncio.Event(), asyncio.Event()
    real_runs = GH.ci_runs_for_sha
    seen: list[int] = []

    async def slow(*args, **kwargs):
        picture = await real_runs(*args, **kwargs)
        seen.append(1)
        if len(seen) == 1:
            entered.set()
            await release.wait()
        return picture

    monkeypatch.setattr(plugins.git_ops, "ci_runs_for_sha", slow)
    return entered, release


def _red_rerun(task_id: int, run_id: str) -> dict:
    return GH.attempt(
        task_id,
        SHA,
        run_id=run_id,
        attempt="2",
        conclusion="failure",
        validation_status="fail",
        ac_results={"AC-1": "fail"},
    )


async def test_a_delayed_refresh_cannot_write_over_a_newer_post(db, monkeypatch):
    """No second refresh, no lock to save it: only the database version."""
    import asyncio

    from hub.services.ci_report import store_ci_run_report

    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="6801", attempt="1"))
    entered, release = await _slow_first_runs(monkeypatch)
    older = asyncio.create_task(ci_trust.refresh(db, task_id, SHA))
    await entered.wait()
    await store_ci_run_report(db, task_id, **_red_rerun(task_id, "6801"))
    await db.commit()
    release.set()
    await older

    assert (await _standing(db, task_id))[2] == "ci:6801:2"
    results, validation, source = await _evidence(db, task_id)
    assert results == [("AC-1", "fail")] and source == "ci:6801:2"


async def test_the_same_race_through_two_independent_instances(db, monkeypatch):
    """Another process has its own lock table: here, a table emptied mid-flight."""
    import asyncio

    from hub.services.ci_report import store_ci_run_report

    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="6802", attempt="1"))
    entered, release = await _slow_first_runs(monkeypatch)
    first = asyncio.create_task(ci_trust.refresh(db, task_id, SHA))
    await entered.wait()
    await store_ci_run_report(db, task_id, **_red_rerun(task_id, "6802"))
    await db.commit()
    ci_trust._locks.clear()  # the other process knows nothing of the first lock
    other = await ci_trust.refresh(db, task_id, SHA)
    assert other.attempt_key == "ci:6802:2"
    release.set()  # ... and only now the first one finishes with its old picture
    await first

    assert (await _standing(db, task_id))[2] == "ci:6802:2"
    assert (await _evidence(db, task_id))[0] == [("AC-1", "fail")]


async def test_a_version_moved_by_another_process_makes_the_refresh_ask_again(
    db, monkeypatch
):
    import asyncio

    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="6803"))
    projected: list[int] = []
    real_project = ci_trust._project

    async def counting(*args, **kwargs):
        projected.append(1)
        return await real_project(*args, **kwargs)

    monkeypatch.setattr(ci_trust, "_project", counting)
    entered, release = await _slow_first_runs(monkeypatch)
    task = asyncio.create_task(ci_trust.refresh(db, task_id, SHA))
    await entered.wait()
    await db.execute(  # what another process does: one row, no Python object shared
        "UPDATE ci_report_state SET version = version + 1 WHERE task_id = ?",
        (task_id,),
    )
    await db.commit()
    release.set()
    standing = await task
    assert standing.trust == "verified"
    assert projected == [1], "the stale picture was not written; the second round was"


async def test_a_check_that_is_always_overtaken_is_superseded_not_guessed(
    db, monkeypatch
):
    from hub.integrations.registry import plugins

    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="6804"))
    real_runs = GH.ci_runs_for_sha

    async def always_overtaken(*args, **kwargs):
        picture = await real_runs(*args, **kwargs)
        await db.execute(
            "UPDATE ci_report_state SET version = version + 1 WHERE task_id = ?",
            (task_id,),
        )
        await db.commit()
        return picture

    monkeypatch.setattr(plugins.git_ops, "ci_runs_for_sha", always_overtaken)
    standing = await ci_trust.refresh(db, task_id, SHA)
    assert (standing.trust, standing.reason) == ("unverified", "check_superseded")
    assert (await _standing(db, task_id))[:2] == ("unverified", "check_superseded")
    assert (await _evidence(db, task_id))[0] == []


async def test_a_post_withdraws_the_standing_until_it_is_settled(db):
    from hub.services.ci_report import store_ci_run_report

    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="6805"))
    assert (await _evidence(db, task_id))[0] == [("AC-1", "pass")]
    await store_ci_run_report(db, task_id, **GH.attempt(task_id, SHA, run_id="6806"))
    await db.commit()
    assert (await _standing(db, task_id))[:2] == ("unverified", "pending")
    assert (await _evidence(db, task_id))[0] == []
    assert (await _gap(db, task_id)).cause == ci_green_proof.UNVERIFIED


# ---- round 2: no network inside a write transaction ------------------------


def _forbid_network_in_transaction(db) -> None:
    """Every call to the fake GitHub asserts that the database is not in a transaction."""
    for name in (
        "ci_run_attempt",
        "ci_runs_for_sha",
        "ci_run_artifacts",
        "ci_artifact_zip",
    ):
        real = getattr(GH, name)

        async def checked(*args, __real=real, __name=name, **kwargs):
            assert not db.in_transaction, f"{__name} called inside a write transaction"
            return await __real(*args, **kwargs)

        setattr(GH, name, checked)
        from hub.integrations.registry import plugins

        setattr(plugins.git_ops, name, checked)


async def test_a_done_report_is_gated_on_the_current_attempt_and_asks_outside_the_lock(
    db, monkeypatch
):
    from fastapi import HTTPException

    from hub import config
    from hub.models import TaskUpdateCreate
    from hub.services import lifecycle

    monkeypatch.setattr(config, "SDD_VALIDATION", "require")
    task_id = await _task_whose_rerun_nobody_reported(db, "6901")
    await repo.update_task(
        db,
        task_id,
        status="pending_report",
        auto_review=0,
        validation_commands='["x"]',
    )
    await db.commit()
    assert (await _evidence(db, task_id))[1] == "pass", "the old green is still stored"
    _forbid_network_in_transaction(db)

    with pytest.raises(HTTPException) as refused:
        await lifecycle.add_update(
            db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="done")
        )
    assert refused.value.status_code == 422
    assert "validation_failed" in str(refused.value.detail)
    assert dict(await repo.get_task(db, task_id))["status"] == "pending_report"
    assert any(c.startswith("runs:") for c in GH.calls), "GitHub WAS asked, outside"


async def test_the_pin_step_does_not_ask_github_from_inside_a_transaction(
    db, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from hub.db import write_transaction
    from hub.services import lifecycle

    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="6902"))
    monkeypatch.setattr(
        lifecycle, "resolve_branch_tip", AsyncMock(return_value=(SHA, ""))
    )

    def state():
        return SimpleNamespace(
            db=db,
            task_id=task_id,
            task={"branch": "b"},
            submission_sha="",
            sha_reason="",
            body=TaskSubmitReview(),
        )

    GH.calls.clear()
    async with write_transaction(db):
        await lifecycle._step_pin_submission_sha(state())
    assert GH.calls == [], "inside a transaction only the stored standing is read"
    await lifecycle._step_pin_submission_sha(state())
    assert any(c.startswith("runs:") for c in GH.calls), "outside it, GitHub is asked"


# ---- round 2: one deadline for a refresh ------------------------------------


async def test_a_refresh_has_one_deadline_for_every_call_to_github(db, monkeypatch):
    import asyncio
    import time

    from hub.integrations.registry import plugins
    from hub.services.ci_report import store_ci_run_report

    task_id = await _task(db)
    await store_ci_run_report(db, task_id, **GH.attempt(task_id, SHA, run_id="7001"))
    await db.commit()

    async def hangs(*args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(plugins.git_ops, "ci_run_attempt", hangs)
    monkeypatch.setattr(ci_trust, "REFRESH_DEADLINE_SECONDS", 0.2)
    started = time.monotonic()
    standing = await ci_trust.refresh(db, task_id, SHA)
    assert time.monotonic() - started < 5
    assert (standing.trust, standing.reason) == ("unverified", "forge_unavailable")
    assert (await _evidence(db, task_id))[0] == []

    # The wait for the per-commit lock counts against the same deadline.
    lock = asyncio.Lock()
    await lock.acquire()
    ci_trust._locks[(task_id, SHA)] = lock
    started = time.monotonic()
    standing = await ci_trust.refresh(db, task_id, SHA)
    assert time.monotonic() - started < 5
    assert standing.trust == "unverified"


async def test_the_first_failure_of_github_ends_the_questions_of_that_refresh(db):
    from hub.services.ci_report import store_ci_run_report

    task_id = await _task(db)
    for n in range(4):  # four attempts, none checked yet
        await store_ci_run_report(
            db, task_id, **GH.attempt(task_id, SHA, run_id=f"710{n}")
        )
    await db.commit()
    GH.down = True
    GH.calls.clear()
    standing = await ci_trust.refresh(db, task_id, SHA)
    assert len(GH.calls) == 1, GH.calls
    assert (standing.trust, standing.reason) == ("unverified", "forge_unavailable")
    assert standing.forge_down is True


async def test_a_tick_that_ran_out_of_budget_goes_on_where_it_stopped(db, monkeypatch):
    from types import SimpleNamespace

    tasks = [await _review_task_with_green(db, f"72{n:02d}") for n in range(3)]
    clock = [0.0]
    monkeypatch.setattr(
        ci_trust, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=None)
    )
    monkeypatch.setattr(ci_trust, "SWEEP_BUDGET_SECONDS", 0.5)
    real = ci_trust.refresh_quietly
    handled: list[int] = []

    async def slow(db_, task_id, sha, **kwargs):
        handled.append(task_id)
        clock[0] += 1.0  # each refresh spends the whole budget
        return await real(db_, task_id, sha, **kwargs)

    monkeypatch.setattr(ci_trust, "refresh_quietly", slow)
    for _ in range(3):
        await ci_trust.sweep(db)
    assert handled == sorted(tasks), "every task got its turn, in order"


# ---- round 3 ---------------------------------------------------------------


async def test_a_retry_round_reserves_its_own_version_and_a_late_answer_loses(
    db, monkeypatch
):
    """A conflicts and retries; B writes; A's retry answer arrives late and stale."""
    import asyncio

    from hub.integrations.registry import plugins

    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="7301", attempt="1"))
    real_runs = GH.ci_runs_for_sha
    stale: list[list] = []
    gates = {n: (asyncio.Event(), asyncio.Event()) for n in (1, 2, 3)}
    calls: list[int] = []

    async def hooked(*args, **kwargs):
        calls.append(1)
        n = len(calls)
        if n == 3:  # A's retry: the answer it got was computed from OLD data
            picture = list(stale[0])
        else:
            picture = await real_runs(*args, **kwargs)
        if n == 1:
            stale.append(list(picture))
        if n in gates:
            entered, release = gates[n]
            entered.set()
            await release.wait()
        return picture

    monkeypatch.setattr(plugins.git_ops, "ci_runs_for_sha", hooked)
    # 1. A asks (call 1) and waits.
    a = asyncio.create_task(ci_trust.refresh(db, task_id, SHA))
    await gates[1][0].wait()
    # 2. A newer attempt exists, nobody reported it; B (another process) starts:
    #    it reserves a version and asks (call 2) ... and waits.
    GH.attempt(task_id, SHA, run_id="7301", attempt="2", artifact=False)
    ci_trust._locks.clear()
    b = asyncio.create_task(ci_trust.refresh(db, task_id, SHA))
    await gates[2][0].wait()
    # 3. A's first round ends in a conflict and it retries (call 3, stale answer).
    gates[1][1].set()
    await gates[3][0].wait()
    # 4. B finishes: it writes newer_attempt_without_report ...
    gates[2][1].set()
    await asyncio.sleep(0.05)
    # 5. ... and only then A's stale retry answer arrives.
    gates[3][1].set()
    await asyncio.gather(a, b)

    assert (await _standing(db, task_id))[:2] == (
        "unverified",
        "newer_attempt_without_report",
    )
    assert (await _evidence(db, task_id))[0] == []


async def test_the_first_submission_asks_about_the_tip_that_will_be_pinned(
    db, monkeypatch
):
    from unittest.mock import AsyncMock

    from hub.services import lifecycle

    task_id = await _task(db, generation=0, sha="")
    await repo.update_task(db, task_id, branch="task-1/x")
    await db.commit()
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="7401", attempt="1"))
    GH.attempt(task_id, SHA, run_id="7401", attempt="2", artifact=False)
    monkeypatch.setattr(
        lifecycle, "resolve_branch_tip", AsyncMock(return_value=(SHA, ""))
    )
    _forbid_network_in_transaction(db)

    task = await lifecycle.prepare_ci_standing(
        db, dict(await repo.get_task(db, task_id))
    )
    assert task[lifecycle.PREPARED_CI_SHA] == SHA
    assert (await _standing(db, task_id))[:2] == (
        "unverified",
        "newer_attempt_without_report",
    )
    # Inside a transaction nothing is asked and the task is returned as it is.
    from hub.db import write_transaction

    GH.calls.clear()
    async with write_transaction(db):
        same = await lifecycle.prepare_ci_standing(db, task)
    assert same is task and GH.calls == []


async def test_the_poller_and_refresh_entries_prepare_before_they_transition(
    db, monkeypatch
):
    """transition_after_agent_done is the door of the poller and of refresh_task."""
    from hub.services import orchestration

    task_id = await _task(db, generation=0, sha="")
    await repo.update_task(db, task_id, branch="task-1/x")
    await db.commit()
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="7501", attempt="1"))
    GH.attempt(task_id, SHA, run_id="7501", attempt="2", artifact=False)
    from unittest.mock import AsyncMock

    from hub.services import lifecycle

    monkeypatch.setattr(
        lifecycle, "resolve_branch_tip", AsyncMock(return_value=(SHA, ""))
    )
    _forbid_network_in_transaction(db)
    await orchestration.transition_after_agent_done(
        db, dict(await repo.get_task(db, task_id)), has_done=True
    )
    assert (await _standing(db, task_id))[:2] == (
        "unverified",
        "newer_attempt_without_report",
    )


async def test_a_different_tip_pinned_inside_the_transaction_is_unconfirmed(
    db, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from hub.db import write_transaction
    from hub.services import lifecycle

    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="7601"))
    assert (await _standing(db, task_id))[0] == "verified"
    monkeypatch.setattr(
        lifecycle, "resolve_branch_tip", AsyncMock(return_value=(SHA, ""))
    )
    task = dict(await repo.get_task(db, task_id))

    def state(prepared):
        return SimpleNamespace(
            db=db,
            task_id=task_id,
            task={**task, lifecycle.PREPARED_CI_SHA: prepared},
            submission_sha="",
            sha_reason="",
            body=TaskSubmitReview(),
        )

    async with write_transaction(db):  # the commit asked about is the one pinned
        await lifecycle._step_pin_submission_sha(state(SHA))
    assert (await _standing(db, task_id))[0] == "verified"
    async with write_transaction(db):  # another commit was asked about
        await lifecycle._step_pin_submission_sha(state("some-other-tip"))
    assert (await _standing(db, task_id))[:2] == ("unverified", "pending")
    assert (await _evidence(db, task_id))[0] == []


async def test_a_timed_out_check_is_saved_as_tried_and_final_after_six_hours(
    db, monkeypatch
):
    import asyncio

    from hub.integrations.registry import plugins
    from hub.services.ci_report import store_ci_run_report

    young = await _task(db)
    old = await _task(db)
    for task_id, run in ((young, "7701"), (old, "7702")):
        await store_ci_run_report(db, task_id, **GH.attempt(task_id, SHA, run_id=run))
    await db.execute(
        "UPDATE ci_report_attempts SET reported_at = datetime('now', '-7 hours') "
        "WHERE task_id = ?",
        (old,),
    )
    await db.commit()

    async def hangs(*args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(plugins.git_ops, "ci_run_attempt", hangs)
    monkeypatch.setattr(ci_trust, "REFRESH_DEADLINE_SECONDS", 0.2)
    for task_id in (young, old):
        await ci_trust.refresh(db, task_id, SHA)

    rows = {
        r["task_id"]: r
        for r in await db.execute_fetchall(
            "SELECT task_id, trust_reason, trust_final, checked_at FROM ci_report_attempts"
        )
    }
    assert rows[young]["trust_reason"] == "forge_unavailable"
    assert rows[young]["checked_at"] is not None and rows[young]["trust_final"] == 0
    assert rows[old]["checked_at"] is not None and rows[old]["trust_final"] == 1
    # The poller does not take them up again within the interval / at all.
    waiting = {
        r["task_id"]
        for r in await repo.ci_attempts_awaiting_check(db, older_than_minutes=5)
    }
    assert waiting == set()


# ---- round 4 ---------------------------------------------------------------


async def test_a_post_between_the_refresh_and_the_transaction_refuses_the_done_report(
    db, monkeypatch
):
    from fastapi import HTTPException

    from hub import config
    from hub.models import TaskUpdateCreate
    from hub.services import lifecycle
    from hub.services.ci_report import store_ci_run_report

    monkeypatch.setattr(config, "SDD_VALIDATION", "require")
    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="8101"))
    await repo.update_task(
        db, task_id, status="pending_report", auto_review=0, validation_commands='["x"]'
    )
    await db.commit()
    assert (await _evidence(db, task_id))[1] == "pass"

    real_prepare = lifecycle.prepare_ci_standing

    async def prepare_then_a_post_arrives(*args, **kwargs):
        prepared = await real_prepare(*args, **kwargs)  # the refresh: still green
        assert prepared["validation_status"] == "pass"
        await store_ci_run_report(
            db, task_id, **GH.attempt(task_id, SHA, run_id="8102")
        )
        await db.commit()  # ... and a new claim withdraws the evidence
        return prepared

    monkeypatch.setattr(lifecycle, "prepare_ci_standing", prepare_then_a_post_arrives)
    with pytest.raises(HTTPException) as refused:
        await lifecycle.add_update(
            db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="done")
        )
    assert refused.value.status_code == 422
    assert "validation_failed" in str(refused.value.detail)
    assert dict(await repo.get_task(db, task_id))["status"] == "pending_report"


async def test_a_change_between_prepare_and_the_transaction_is_judged_inside_it(
    db, monkeypatch
):
    """The prepared task asks for review (gate skipped); inside the transaction it does not.

    ``auto_review`` flips off and a new report withdraws the evidence AFTER the
    prepare step. Only the task re-read inside the transaction
    (``_task_inside_transaction``) and the gate re-run there
    (``ci_trust.recheck_in_transaction``) can see it: the done report is refused.
    """
    from fastapi import HTTPException

    from hub import config
    from hub.models import TaskUpdateCreate
    from hub.services import lifecycle
    from hub.services.ci_report import store_ci_run_report

    monkeypatch.setattr(config, "SDD_VALIDATION", "require")
    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="8151"))
    await repo.update_task(
        db, task_id, status="pending_report", auto_review=1, validation_commands='["x"]'
    )
    await db.commit()

    real_prepare = lifecycle.prepare_ci_standing

    async def prepare_then_the_task_changes(*args, **kwargs):
        prepared = await real_prepare(*args, **kwargs)
        assert prepared["auto_review"], "the prepared task still asks for review"
        await repo.update_task(db, task_id, auto_review=0)
        await store_ci_run_report(
            db, task_id, **GH.attempt(task_id, SHA, run_id="8152")
        )
        await db.commit()
        return prepared

    rechecks: list[int] = []
    real_recheck = ci_trust.recheck_in_transaction

    async def counting_recheck(*args, **kwargs):
        rechecks.append(1)
        return await real_recheck(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "prepare_ci_standing", prepare_then_the_task_changes)
    monkeypatch.setattr(ci_trust, "recheck_in_transaction", counting_recheck)
    with pytest.raises(HTTPException) as refused:
        await lifecycle.add_update(
            db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="done")
        )
    assert refused.value.status_code == 422
    assert "validation_failed" in str(refused.value.detail)
    assert rechecks == [1], "the decision is re-made through the one helper"
    assert dict(await repo.get_task(db, task_id))["status"] == "pending_report"


async def test_a_deleted_run_and_a_newer_red_one_without_a_report_is_never_verified(db):
    from hub.services.ac_tests import ac_tests_gap

    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="8201", attempt="1"))
    assert (await _gap(db, task_id)) is None
    assert (await _evidence(db, task_id))[0] == [("AC-1", "pass")]

    # R1 is deleted on GitHub; R2 is a completed red run nobody reported.
    GH.runs.pop(("8201", "1"))
    GH.artifacts.pop("8201")
    GH.attempt(task_id, SHA, run_id="8202", attempt="1", conclusion="failure")
    await ci_trust.refresh(db, task_id, SHA)

    assert (await _standing(db, task_id))[:2] == (
        "unverified",
        "newer_attempt_without_report",
    )
    assert (await _evidence(db, task_id))[0] == []
    assert (await _gap(db, task_id)).cause == ci_green_proof.UNVERIFIED
    assert await ac_tests_gap(db, dict(await repo.get_task(db, task_id)))


# ---- round 5: ONE rule for every gate --------------------------------------
#
# Network before the transaction; inside the transaction that applies the
# decision the gate rereads the database and decides again (ci_trust.
# recheck_in_transaction). One regression test per entry: a report POSTed
# between the gate and the apply gives a refusal.


async def _a_report_arrives(db, task_id: int, run_id: str, real: bool = True) -> None:
    """A new claim for the same commit is accepted and committed (not yet settled).

    ``real=False``: GitHub has no artifact for it, so settling it cannot confirm it.
    """
    from hub.services.ci_report import store_ci_run_report

    sent = GH.attempt(task_id, SHA, run_id=run_id, artifact=real)
    await store_ci_run_report(db, task_id, **sent)
    await db.commit()


@pytest.mark.parametrize(
    ("agent", "steward", "ac_mode", "expected"),
    [
        ("reviewer", False, "require", "ac_tests_not_green"),
        ("policy", False, "off", "ci_not_green"),
        ("steward", True, "off", "ci_not_green"),
    ],
)
async def test_a_report_between_the_verdict_gate_and_its_apply_refuses_the_approval(
    db, monkeypatch, agent, steward, ac_mode, expected
):
    from fastapi import HTTPException

    from hub import config
    from hub.models import ReviewVerdict, TaskReviewVerdict
    from hub.services import lifecycle

    monkeypatch.setattr(config, "SDD_AC_TESTS", ac_mode)
    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="9101"))
    await repo.update_task(db, task_id, status="review")
    await db.commit()

    real_apply = lifecycle._apply_verdict

    async def a_report_arrives_first(state):
        await _a_report_arrives(db, task_id, "9102")
        return await real_apply(state)

    monkeypatch.setattr(lifecycle, "_apply_verdict", a_report_arrives_first)
    if steward:  # #2037: the steward's right to approve is read in the transaction
        from hub.services import steward_shadow
        from tests.test_steward_shadow import _project

        async def _granted_now(_db):
            return True

        monkeypatch.setattr(steward_shadow, "act_granted_now", _granted_now)

        await repo.update_task(
            db, task_id, project_id=await _project(db, "ci-trust-steward")
        )
        await db.commit()
    with pytest.raises(HTTPException) as refused:
        await lifecycle.record_review_verdict(
            db,
            task_id,
            TaskReviewVerdict(verdict=ReviewVerdict.approved, agent=agent),
            self_approved=True,
            applied_by_steward=steward,
            expected_generation=1 if agent != "reviewer" else None,
        )
    assert refused.value.status_code == 422
    assert expected in str(refused.value.detail)
    assert dict(await repo.get_task(db, task_id))["review_verdict"] in (None, "")


async def test_the_recheck_helper_refuses_to_run_outside_a_transaction_and_to_ask_github(
    db,
):
    with pytest.raises(RuntimeError):
        await ci_trust.recheck_in_transaction(db)

    from hub.db import write_transaction

    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="9103"))

    async def asks_github():
        await ci_trust.refresh(db, task_id, SHA)

    async with write_transaction(db):
        with pytest.raises(RuntimeError):
            await ci_trust.recheck_in_transaction(db, asks_github)


@pytest.mark.parametrize(("agent", "steward"), [("policy", False), ("steward", True)])
async def test_a_resubmission_between_the_decision_and_the_apply_cancels_the_approval(
    db, monkeypatch, agent, steward
):
    """Automation judged generation 1 (green); the task is resubmitted as 2 (no CI)."""
    from fastapi import HTTPException

    from hub import config
    from hub.models import ReviewVerdict, TaskReviewVerdict
    from hub.services import lifecycle

    monkeypatch.setattr(config, "SDD_AC_TESTS", "off")
    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="9201"))
    await repo.update_task(db, task_id, status="review")
    await db.commit()
    real_apply = lifecycle._apply_verdict

    async def resubmitted_first(state):
        await repo.bump_submission_generation(db, task_id)
        await repo.update_task(db, task_id, submission_sha="sha-new-without-ci")
        await db.commit()
        return await real_apply(state)

    monkeypatch.setattr(lifecycle, "_apply_verdict", resubmitted_first)
    with pytest.raises(HTTPException) as refused:
        await lifecycle.record_review_verdict(
            db,
            task_id,
            TaskReviewVerdict(verdict=ReviewVerdict.approved, agent=agent),
            self_approved=True,
            applied_by_steward=steward,
            expected_generation=1,
        )
    assert refused.value.status_code == 409
    row = dict(await repo.get_task(db, task_id))
    assert row["submission_generation"] == 2
    assert row["review_verdict"] in (None, ""), "generation 2 was not approved"


async def test_an_automated_approval_must_name_the_generation_it_judged(
    db, monkeypatch
):
    from fastapi import HTTPException

    from hub import config
    from hub.models import ReviewVerdict, TaskReviewVerdict
    from hub.services import lifecycle

    monkeypatch.setattr(config, "SDD_AC_TESTS", "off")
    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="9202"))
    await repo.update_task(db, task_id, status="review")
    await db.commit()
    with pytest.raises(HTTPException) as refused:
        await lifecycle.record_review_verdict(
            db,
            task_id,
            TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="policy"),
            self_approved=True,
        )
    assert refused.value.status_code == 422
    assert "generation" in str(refused.value.detail)


async def test_a_report_between_the_done_flow_and_its_deferred_completion_refuses_it(
    db, monkeypatch
):
    """The write of ``completed`` asks the validation gate again, of the database."""
    from fastapi import HTTPException

    from hub import config
    from hub.models import TaskUpdateCreate
    from hub.services import lifecycle

    monkeypatch.setattr(config, "SDD_VALIDATION", "require")
    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="9301"))
    await repo.update_task(
        db, task_id, status="running", auto_review=0, validation_commands='["x"]'
    )
    await db.commit()
    assert (await _evidence(db, task_id))[1] == "pass"
    _forbid_network_in_transaction(db)

    real_finish = lifecycle._finish_deferred_done

    async def a_report_arrives_first(*args, **kwargs):
        await _a_report_arrives(db, task_id, "9302", real=False)
        return await real_finish(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_finish_deferred_done", a_report_arrives_first)
    with pytest.raises(HTTPException) as refused:
        await lifecycle.add_update(
            db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="done")
        )
    assert refused.value.status_code == 422
    assert "validation_failed" in str(refused.value.detail)
    assert dict(await repo.get_task(db, task_id))["status"] != "completed"


async def test_the_deferred_completion_asks_github_before_it_writes_completed(
    db, monkeypatch
):
    """A rerun nobody reported appears after the done flow: only the ask finds it."""
    from fastapi import HTTPException

    from hub import config
    from hub.models import TaskUpdateCreate
    from hub.services import lifecycle

    monkeypatch.setattr(config, "SDD_VALIDATION", "require")
    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="9401", attempt="1"))
    await repo.update_task(
        db, task_id, status="running", auto_review=0, validation_commands='["x"]'
    )
    await db.commit()
    _forbid_network_in_transaction(db)
    real_finish = lifecycle._finish_deferred_done

    async def a_rerun_starts_first(*args, **kwargs):
        GH.attempt(
            task_id,
            SHA,
            run_id="9401",
            attempt="2",
            status="in_progress",
            artifact=False,
        )
        return await real_finish(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_finish_deferred_done", a_rerun_starts_first)
    with pytest.raises(HTTPException) as refused:
        await lifecycle.add_update(
            db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="done")
        )
    assert "validation_failed" in str(refused.value.detail)
    assert dict(await repo.get_task(db, task_id))["status"] != "completed"


# ---- round 7: the evidence is of the commit being completed -----------------


@pytest.mark.parametrize("status", ["pending_report", "running"])
@pytest.mark.parametrize("tip_validation", ["fail", "pass"])
async def test_a_done_report_is_judged_on_the_tip_it_completes_not_on_the_pinned_commit(
    db, monkeypatch, status, tip_validation
):
    """A is pinned with a passing validation; the branch is already at B."""
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from hub import config
    from hub.models import TaskUpdateCreate
    from hub.services import lifecycle

    tip = "sha-tip-b"
    monkeypatch.setattr(config, "SDD_VALIDATION", "require")
    task_id = await _task(db)  # pinned SHA = A, generation 1
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="9501"))
    assert (await _evidence(db, task_id))[1] == "pass", "A's pass is stored on the task"
    await _post(
        db,
        task_id,
        GH.attempt(
            task_id,
            tip,
            run_id="9502",
            validation_status=tip_validation,
            ac_results={"AC-1": tip_validation},
        ),
    )
    await repo.update_task(
        db, task_id, status=status, auto_review=0, validation_commands='["x"]'
    )
    await db.commit()
    monkeypatch.setattr(
        lifecycle, "resolve_branch_tip", AsyncMock(return_value=(tip, ""))
    )
    done = TaskUpdateCreate(agent="dev", kind="done", content="done")

    if tip_validation == "fail":
        with pytest.raises(HTTPException) as refused:
            await lifecycle.add_update(db, task_id, done)
        assert refused.value.status_code == 422
        assert "validation_failed" in str(refused.value.detail)
        assert dict(await repo.get_task(db, task_id))["status"] != "completed"
    else:
        await lifecycle.add_update(db, task_id, done)
        assert dict(await repo.get_task(db, task_id))["status"] == "completed"


async def test_validation_written_from_another_commit_does_not_cover_the_work(
    db, monkeypatch
):
    from hub import config

    monkeypatch.setattr(config, "SDD_VALIDATION", "require")
    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="9601"))
    await _post(db, task_id, GH.attempt(task_id, "sha-other", run_id="9602"))
    await repo.update_task(db, task_id, validation_commands='["x"]')
    await db.commit()
    task = dict(await repo.get_task(db, task_id))
    assert await lifecycle_gap(db, task) is None

    # The task row says its validation came from the attempt of ANOTHER commit.
    await db.execute(
        "UPDATE tasks SET validation_source = 'ci:9602:1' WHERE id = ?", (task_id,)
    )
    await db.commit()
    gap = await lifecycle_gap(db, dict(await repo.get_task(db, task_id)))
    assert gap and "sha-other"[:12] in gap


async def lifecycle_gap(db, task):
    from hub.services.lifecycle import _validation_gap_of_the_work

    return await _validation_gap_of_the_work(db, task)


# ---- round 8: the done gate's three corners ---------------------------------


@pytest.mark.parametrize("status", ["pending_report", "running"])
async def test_a_tip_whose_run_failed_cannot_complete_the_work_even_with_a_pass(
    db, monkeypatch, status
):
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from hub import config
    from hub.models import TaskUpdateCreate
    from hub.services import lifecycle

    tip = "sha-tip-failed-run"
    monkeypatch.setattr(config, "SDD_VALIDATION", "require")
    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="9701"))
    await _post(  # confirmed, validation pass, but the RUN ended in failure
        db, task_id, GH.attempt(task_id, tip, run_id="9702", conclusion="failure")
    )
    await repo.update_task(
        db, task_id, status=status, auto_review=0, validation_commands='["x"]'
    )
    await db.commit()
    monkeypatch.setattr(
        lifecycle, "resolve_branch_tip", AsyncMock(return_value=(tip, ""))
    )
    with pytest.raises(HTTPException) as refused:
        await lifecycle.add_update(
            db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="done")
        )
    assert "validation_failed" in str(refused.value.detail)
    assert dict(await repo.get_task(db, task_id))["status"] != "completed"


def test_writing_a_pass_and_accepting_one_share_one_predicate():
    fp = ci_contract.HUB_FINGERPRINT
    ok = {
        "trust": "verified",
        "contract_fp": fp,
        "provenance": json.dumps({"run_conclusion": "success"}),
    }
    assert ci_trust.can_testify_pass(ok, fp) is True
    assert ci_trust.can_testify_pass({**ok, "trust": "unverified"}, fp) is False
    assert ci_trust.can_testify_pass(ok, "another-contract") is False
    failed = {**ok, "provenance": json.dumps({"run_conclusion": "failure"})}
    assert ci_trust.can_testify_pass(failed, fp) is False


@pytest.mark.parametrize("status", ["pending_report", "running"])
async def test_a_manual_validation_still_completes_work_whose_tip_has_no_ci(
    db, monkeypatch, status
):
    from unittest.mock import AsyncMock

    from hub import config
    from hub.models import TaskUpdateCreate
    from hub.services import lifecycle
    from hub.services.validation_run import record_validation_result

    monkeypatch.setattr(config, "SDD_VALIDATION", "require")
    task_id = await _task(db)  # A pinned, generation 1, no CI at all
    await record_validation_result(
        db, task_id, generation=1, status="pass", source="manual"
    )
    await repo.update_task(
        db, task_id, status=status, auto_review=0, validation_commands='["x"]'
    )
    await db.commit()
    monkeypatch.setattr(
        lifecycle, "resolve_branch_tip", AsyncMock(return_value=("sha-tip-b", ""))
    )
    await lifecycle.add_update(
        db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="done")
    )
    assert dict(await repo.get_task(db, task_id))["status"] == "completed"


async def test_a_task_without_validation_commands_is_not_blocked_by_evidence_of_a_commit(
    db, monkeypatch
):
    """CI-written validation from A; B is resubmitted and approved; nothing to validate."""
    from unittest.mock import AsyncMock

    from hub import config
    from hub.models import ReviewVerdict, TaskReviewVerdict, TaskUpdateCreate
    from hub.services import lifecycle

    monkeypatch.setattr(config, "SDD_VALIDATION", "require")
    monkeypatch.setattr(config, "SDD_AC_TESTS", "off")
    task_id = await _task(db)
    await _post(db, task_id, GH.attempt(task_id, SHA, run_id="9801"))
    task = dict(await repo.get_task(db, task_id))
    assert (
        task["validation_source"].startswith("ci:")
        and task["validation_commands"] == "[]"
    )
    # Generation 2: another commit, no CI; a reviewer approves it.
    await repo.bump_submission_generation(db, task_id)
    await repo.update_task(db, task_id, submission_sha="sha-b", status="review")
    await db.commit()
    await lifecycle.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="reviewer"),
        self_approved=True,
    )
    monkeypatch.setattr(
        lifecycle, "resolve_branch_tip", AsyncMock(return_value=("sha-b", ""))
    )
    await lifecycle.add_update(
        db, task_id, TaskUpdateCreate(agent="dev", kind="done", content="done")
    )
    assert dict(await repo.get_task(db, task_id))["status"] == "completed"


# ---- deep review 917 --------------------------------------------------------


async def test_a_timeout_is_never_taken_for_a_missing_run_because_of_digits_in_an_id(
    db, monkeypatch
):
    """The message of a timeout carries the endpoint; ids with 404 or 410 are not "not found"."""
    from hub.integrations import proc
    from hub.integrations.forge import github as gh_module
    from hub.integrations.registry import plugins
    from hub.services.ci_report import store_ci_run_report

    async def timed_out(*args, repo=None, **kw):
        return 124, "", f"timed out after 20s: gh api {args[1]}"

    async def zip_timed_out(*cmd, **kw):
        return 124, b"", f"timed out after 90s: gh api {cmd[2]}"

    monkeypatch.setattr(gh_module, "_gh", timed_out)
    monkeypatch.setattr(proc, "run_capped", zip_timed_out)
    forge = gh_module.GitHubForge()
    run_id, sha = "18594041234", "a410b404c410"
    assert await forge.ci_run_attempt(run_id, "1", gh_repo="own/rep") is None
    assert await forge.ci_runs_for_sha(sha, gh_repo="own/rep") is None
    assert await forge.ci_run_artifacts(run_id, gh_repo="own/rep") is None
    assert (
        await forge.ci_artifact_zip("4041041", max_bytes=5, gh_repo="own/rep") is None
    )
    # ... while GitHub's own 404 / 410 still mean "absent".
    for text in ("gh: Not Found (HTTP 404)", "HTTP 410: Gone (https://x)"):
        assert gh_module.GitHubForge._api_missing(text) is True
    assert (
        gh_module.GitHubForge._api_missing("timed out after 20s: gh api runs/404")
        is False
    )

    # End to end: the report stays retryable (not final) instead of run_not_found.
    for name in (
        "ci_run_attempt",
        "ci_runs_for_sha",
        "ci_run_artifacts",
        "ci_artifact_zip",
    ):
        monkeypatch.setattr(plugins.git_ops, name, _bound(forge, name), raising=False)
    task_id = await _task(db)
    sent = GH.attempt(task_id, SHA, run_id=run_id)
    await store_ci_run_report(db, task_id, **sent)
    await db.commit()
    standing = await ci_trust.refresh(db, task_id, SHA)
    assert (standing.trust, standing.reason) == ("unverified", "forge_unavailable")
    row = (await repo.list_ci_report_attempts(db, task_id, SHA))[0]
    assert row["trust_final"] == 0 and row["trust_reason"] == "forge_unavailable"


def _bound(forge, name):
    method = getattr(forge, name)

    async def call(*args, forge="", **kwargs):  # the git_ops signature carries `forge`
        return await method(*args, **kwargs)

    return call


async def test_a_timed_out_refresh_does_not_overwrite_a_newer_confirmation(
    db, monkeypatch
):
    import asyncio

    from hub.integrations.registry import plugins
    from hub.services.ci_report import store_ci_run_report

    task_id = await _task(db)
    await store_ci_run_report(db, task_id, **GH.attempt(task_id, SHA, run_id="9901"))
    await db.commit()
    real = GH.ci_run_attempt
    calls: list[int] = []

    async def first_call_hangs(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            await asyncio.sleep(30)
        return await real(*args, **kwargs)

    monkeypatch.setattr(plugins.git_ops, "ci_run_attempt", first_call_hangs)
    monkeypatch.setattr(ci_trust, "REFRESH_DEADLINE_SECONDS", 0.6)
    slow = asyncio.create_task(ci_trust.refresh(db, task_id, SHA))
    await asyncio.sleep(0.15)  # the slow refresh reserved its version and hangs
    ci_trust._locks.clear()  # another refresh (another process) is not blocked by it
    other = await ci_trust.refresh(db, task_id, SHA)
    assert other.trust == "verified"
    await slow  # its deadline passes; its offline fallback must keep its hands off

    assert (await _standing(db, task_id))[:2] == ("verified", "")
    assert (await _evidence(db, task_id))[0] == [("AC-1", "pass")]


async def test_an_unconfirmed_report_is_not_shown_as_if_ci_never_sent_one(db):
    from hub.services import review_evidence
    from hub.services.ci_report import store_ci_run_report

    task_id = await _task(db)
    task = dict(await repo.get_task(db, task_id))
    none_sent = await review_evidence.prepass_state(db, task)
    assert "не присылал" in none_sent.reason

    sent = GH.attempt(task_id, SHA, run_id="9902")
    sent["validation_status"] = "fail"  # the body differs from the artifact
    await store_ci_run_report(db, task_id, **sent)
    await db.commit()
    await ci_trust.refresh(db, task_id, SHA)
    prepass = await review_evidence.prepass_state(db, task)
    assert prepass.state == "unknown"
    assert "CI-отчёт не подтверждён: body_mismatch" in prepass.reason
    assert "не присылал" not in prepass.reason
    assert "CI-отчёт не подтверждён: body_mismatch" in review_evidence.prepass_block(
        prepass
    )
    evidence = await review_evidence.ci_evidence_state(db, task)
    assert "не подтверждён: body_mismatch" in evidence.baseline.reason


# ---- #1848: the project's CI contract ---------------------------------------

AGENT_HEADERS = {"Authorization": "Bearer unused-env-token"}


async def _project(hub, slug: str) -> int:
    resp = await hub.client.post(
        "/api/projects", json={"slug": slug, "name": slug.title()}, headers=hub.human
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _stored_policy(db, pid: int) -> dict:
    return json.loads((await repo.get_project(db, pid))["gate_policy"] or "{}")


async def test_the_project_ci_contract_is_validated_and_human_only(ci_runner_hub):
    from datetime import UTC, datetime, timedelta

    from hub.services import policy_change

    hub = ci_runner_hub
    db, client = hub.db, hub.client
    pid = await _project(hub, "sat-contract")
    default_pid = await _project(hub, "default")

    async def write(value, headers=None, project=pid):
        return await client.patch(
            f"/api/projects/{project}",
            json={"gate_policy": {"ci_contract": value}},
            headers=headers or hub.human,
        )

    # Every malformed contract is refused, and nothing is stored.
    bad = [
        {**SAT_CONTRACT, "workflow_path": "ci.yml"},
        {**SAT_CONTRACT, "workflow_path": ".github/workflows/sub/tests.yml"},
        {**SAT_CONTRACT, "workflow_path": ".github/workflows/../ci.yml"},
        {**SAT_CONTRACT, "workflow_path": ".github/workflows/tests.txt"},
        {**SAT_CONTRACT, "workflow_path": ".github/actions/tests.yml"},
        {**SAT_CONTRACT, "required_checks": []},
        {**SAT_CONTRACT, "required_checks": ["tests", "tests"]},
        {**SAT_CONTRACT, "required_checks": ["tests", "security"]},
        {**SAT_CONTRACT, "extra": 1},
        {"workflow_path": SAT_CONTRACT["workflow_path"]},
        {"required_checks": ["tests"]},
        "tests.yml",
        ["tests"],
    ]
    for value in bad:
        refused = await write(value)
        assert refused.status_code == 422, (value, refused.text)
    assert "ci_contract" not in await _stored_policy(db, pid)

    # An agent and a ci_runner key are refused; neither writes the key.
    assert (await write(SAT_CONTRACT, AGENT_HEADERS)).status_code == 403
    assert (await write(SAT_CONTRACT, hub.ci)).status_code == 403
    assert "ci_contract" not in await _stored_policy(db, pid)

    # The hub's own project keeps the hub contract: refused for everybody.
    locked = await write(SAT_CONTRACT, project=default_pid)
    assert locked.status_code == 422, locked.text
    assert locked.json()["detail"]["error"] == "ci_contract_not_for_default"
    assert "ci_contract" not in await _stored_policy(db, default_pid)

    # A deferred patch with the key (and with null) is refused when created...
    at = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    url = "/api/projects/sat-contract/policy-schedule"
    for patch in ({"ci_contract": SAT_CONTRACT}, {"ci_contract": None}):
        made = await client.post(
            url, json={"at": at, "patch": patch}, headers=hub.human
        )
        assert made.status_code == 422, (patch, made.text)
        assert made.json()["detail"]["error"] == "ci_contract_not_schedulable"
    assert await repo.list_scheduled_policy_changes(db, pid) == []

    # ...and when it is executed (a row that got past creation, e.g. by SQL).
    for patch in ({"ci_contract": SAT_CONTRACT}, {"ci_contract": None}):
        await repo.insert_scheduled_policy_change(
            db,
            project_id=pid,
            at="2026-01-01T00:00:00Z",
            patch=patch,
            note="",
            created_by="x",
        )
    await db.commit()
    outcomes = await policy_change.run_due(db)
    assert [o["outcome"] for o in outcomes] == ["refused", "refused"]
    assert {o["error"] for o in outcomes} == {"ci_contract_not_schedulable"}
    assert "ci_contract" not in await _stored_policy(db, pid)

    # A human writes it; the audit says what it was and what it became.
    ok = await write(
        {"required_checks": ["tests"], "workflow_path": SAT_CONTRACT["workflow_path"]}
    )
    assert ok.status_code == 200, ok.text
    assert (await _stored_policy(db, pid))["ci_contract"] == SAT_CONTRACT
    event = (await repo.list_project_events(db, pid, "project_gate_policy_changed"))[0]
    changes = json.loads(event["payload"])["changes"]["ci_contract"]
    assert changes == {"was": None, "now": SAT_CONTRACT}
    # The checks are stored sorted, whatever order they were sent in.
    sorted_ok = await write({**SAT_CONTRACT, "required_checks": ["tests", "lint"]})
    assert sorted_ok.status_code == 200
    assert (await _stored_policy(db, pid))["ci_contract"]["required_checks"] == [
        "lint",
        "tests",
    ]

    # effective_policy shows the key, with the fingerprint the trust rests on.
    summary = (
        await client.get(
            "/api/projects/sat-contract/effective-policy", headers=hub.human
        )
    ).json()
    row = next(r for r in summary["keys"] if r["key"] == "ci_contract")
    assert row["source"] == "project"
    assert row["value"]["workflow_path"] == SAT_CONTRACT["workflow_path"]
    assert row["value"]["required_checks"] == ["lint", "tests"]
    assert len(row["value"]["fingerprint"]) == 64

    # Removing the key (the rollback) returns the project to the hub contract.
    removed = await client.patch(
        f"/api/projects/{pid}",
        json={"gate_policy": {"ci_contract": None}},
        headers=hub.human,
    )
    assert removed.status_code == 200, removed.text
    assert "ci_contract" not in await _stored_policy(db, pid)
    summary = (
        await client.get(
            "/api/projects/sat-contract/effective-policy", headers=hub.human
        )
    ).json()
    row = next(r for r in summary["keys"] if r["key"] == "ci_contract")
    assert row["source"] == "default"
    assert row["value"]["workflow_path"] == ".github/workflows/ci.yml"


# ---- #1848: a satellite is confirmed by its own contract ---------------------


def _sat_attempt(task_id: int, **kwargs):
    """The report a satellite's trusted sender would send: tests.yml, one check."""
    kwargs.setdefault("path", SAT_PATH)
    kwargs.setdefault("checks", dict(SAT_CHECKS))
    return GH.attempt(task_id, SAT_SHA, **kwargs)


async def _sat_gap(db, task_id: int):
    return await _gap(db, task_id, SAT_SHA)


async def test_a_satellite_report_is_verified_by_its_own_contract(db):
    _, sat = await satellite_task(db)
    out = await _post(db, sat, _sat_attempt(sat))
    assert (out["trust"], out["trust_reason"]) == ("verified", "")
    assert out["applied"] is True
    contract = await ci_contract.contract_for_task(db, sat)
    report = dict(await repo.get_ci_run_report(db, sat, SAT_SHA))
    assert report["contract_fp"] == contract.fingerprint != ci_contract.HUB_FINGERPRINT
    # Green: the run succeeded and `tests` — the only required check — passed.
    assert ci_trust.green_gap(report, contract) is None
    assert await ci_trust.pass_gap(db, sat, SAT_SHA) is None
    assert await _sat_gap(db, sat) is None
    assert (await _evidence(db, sat))[:2] == ([("AC-1", "pass")], "pass")
    assert (await steward_evidence._ci_fact(db, sat, SAT_SHA)).value["passed"] is True
    assert SAT_PATH in GH.asked_workflows, (
        "the forge is asked about the contract's workflow"
    )

    # The same report of a project WITHOUT the key is not confirmed: the hub
    # contract wants ci.yml.
    _, plain = await satellite_task(db, slug="plain", contract=None)
    out = await _post(db, plain, _sat_attempt(plain))
    assert (out["trust"], out["trust_reason"]) == ("unverified", "wrong_workflow")
    assert out["applied"] is False
    assert await ci_trust.pass_gap(db, plain, SAT_SHA) is not None
    assert (await _sat_gap(db, plain)).cause == ci_green_proof.UNVERIFIED

    # The hub's own matrix is unchanged: ci.yml and the four checks.
    hub_task = await _task(db)
    asked = len(GH.asked_workflows)
    out = await _post(db, hub_task, GH.attempt(hub_task, SHA))
    assert (out["trust"], out["applied"]) == ("verified", True)
    assert GH.asked_workflows[asked:] == [ci_contract.HUB_CONTRACT.workflow_path]
    assert await _gap(db, hub_task) is None
    only_tests = await _task(db)
    await _post(db, only_tests, GH.attempt(only_tests, SHA, checks=dict(SAT_CHECKS)))
    assert (await _standing(db, only_tests))[0] == "verified"
    assert (await _gap(db, only_tests)).cause == ci_green_proof.NOT_GREEN


async def test_satellite_provenance_mismatches_fail_closed(db):
    def other_repository(sent):
        GH.runs[(sent["provenance"]["run_id"], "1")]["repository"] = "other/rep"

    def other_workflow(sent):
        GH.runs[(sent["provenance"]["run_id"], "1")]["path"] = (
            ".github/workflows/ci.yml"
        )

    def claims_other_workflow(sent):
        sent["provenance"]["workflow_path"] = ".github/workflows/ci.yml"

    def other_commit(sent):
        GH.runs[(sent["provenance"]["run_id"], "1")]["head_sha"] = "sha-someone-elses"

    def wrong_digest(sent):
        sent["provenance"]["artifact_digest"] = "sha256:" + "0" * 64

    def forged_body(sent):
        sent["checks"] = {"tests": "pass", "lint": "pass"}

    def no_artifact(sent):
        GH.artifacts.clear()

    cases = {
        "wrong_repository": other_repository,
        "wrong_workflow": other_workflow,
        "wrong_workflow#claim": claims_other_workflow,
        "wrong_commit": other_commit,
        "artifact_mismatch": wrong_digest,
        "body_mismatch": forged_body,
        "artifact_missing": no_artifact,
    }
    for label, tamper in cases.items():
        slug = label.replace("#", "-").replace("_", "-")
        _, task_id = await satellite_task(db, slug=slug)
        sent = _sat_attempt(task_id)
        tamper(sent)
        out = await _post(db, task_id, sent)
        reason = label.split("#")[0]
        assert out["trust"] == "unverified", label
        assert out["trust_reason"] == reason, (label, out["trust_reason"])
        assert out["applied"] is False, label
        assert (await _evidence(db, task_id))[0] == [], label
        assert await ci_trust.pass_gap(db, task_id, SAT_SHA) is not None, label
        assert (await _sat_gap(db, task_id)).cause == ci_green_proof.UNVERIFIED, label

    # An old attempt: a newer one started and nobody reported it.
    _, task_id = await satellite_task(db, slug="old-attempt")
    old = _sat_attempt(task_id, run_id="7001", attempt="1")
    _sat_attempt(task_id, run_id="7001", attempt="2", artifact=False)
    out = await _post(db, task_id, old)
    assert (out["trust"], out["trust_reason"]) == (
        "unverified",
        "newer_attempt_without_report",
    )
    assert await ci_trust.pass_gap(db, task_id, SAT_SHA) is not None

    # Verified, but a required check is missing or skipped: verified, not green.
    for label, checks in (("missing", {}), ("skipped", {"tests": "skipped"})):
        _, task_id = await satellite_task(db, slug=f"incomplete-{label}")
        await _post(db, task_id, _sat_attempt(task_id, checks=checks))
        assert (await _standing(db, task_id, SAT_SHA))[0] == "verified", label
        assert (await _sat_gap(db, task_id)).cause == ci_green_proof.NOT_GREEN, label
        assert await ci_trust.pass_gap(db, task_id, SAT_SHA) is not None, label

    # Verified, the run failed, no check says fail: still not green.
    _, task_id = await satellite_task(db, slug="failed-run")
    await _post(db, task_id, _sat_attempt(task_id, conclusion="failure"))
    assert (await _standing(db, task_id, SAT_SHA))[0] == "verified"
    assert (await _sat_gap(db, task_id)).cause == ci_green_proof.NOT_GREEN
    assert await ci_trust.pass_gap(db, task_id, SAT_SHA) is not None


async def test_satellite_predicates_answer_per_state(db):
    """The nine states of #1848 AC-4: the predicates are the old ones, the contract is the project's."""
    from hub.services import review_ci_gate

    contract_dict = SAT_CONTRACT
    base = {
        "checks": {"tests": "pass"},
        "conclusion": "success",
        "validation_status": "pass",
        "trust": "verified",
    }
    # state -> (fields changed, trust, green_gap?, pass_gap?, failed_checks)
    states = {
        "provenance does not match": (
            {"trust": "unverified"},
            "unverified",
            False,
            True,
            [],
        ),
        "base": ({}, "verified", False, False, []),
        "optional lint=fail": (
            {"checks": {"tests": "pass", "lint": "fail"}},
            "verified",
            False,
            False,
            ["lint"],
        ),
        "tests=fail": (
            {"checks": {"tests": "fail"}},
            "verified",
            True,
            True,
            ["tests"],
        ),
        "failure without a failed check": (
            {"conclusion": "failure"},
            "verified",
            True,
            True,
            [],
        ),
        "tests absent": ({"checks": {}}, "verified", True, True, []),
        "tests skipped": ({"checks": {"tests": "skipped"}}, "verified", True, True, []),
        "validation=fail": (
            {"validation_status": "fail"},
            "verified",
            False,
            True,
            ["validation"],
        ),
        "validation=skipped": (
            {"validation_status": "skipped"},
            "verified",
            False,
            True,
            [],
        ),
    }
    for n, (label, (change, trust, green, passes, failed)) in enumerate(states.items()):
        _, task_id = await satellite_task(db, slug=f"state-{n}")
        fields = {**base, **change}
        contract = await ci_contract.contract_for_task(db, task_id)
        assert contract.workflow_path == contract_dict["workflow_path"]
        await repo.upsert_ci_run_report(
            db,
            task_id=task_id,
            head_sha=SAT_SHA,
            ac_results="{}",
            validation_status=fields["validation_status"],
            validation_log="",
            reason="",
            reported_by="ci",
            checks=json.dumps(fields["checks"]),
            provenance=json.dumps(
                {
                    "run_id": "1",
                    "run_attempt": "1",
                    "run_conclusion": fields["conclusion"],
                }
            ),
            trust=fields["trust"],
            trust_reason="wrong_workflow" if fields["trust"] != "verified" else "",
            attempt_key="ci:1:1",
        )
        await ci_contract.stamp_projection(db, task_id, SAT_SHA, contract.fingerprint)
        await db.commit()
        row = dict(await repo.get_ci_run_report(db, task_id, SAT_SHA))
        task = dict(await repo.get_task(db, task_id))
        got = (
            ci_trust.trust_of(row, contract.fingerprint)[0],
            ci_trust.green_gap(row, contract) is not None,
            await ci_trust.pass_gap(db, task_id, SAT_SHA) is not None,
            (await review_ci_gate._pinned_ci(db, task))[1],
        )
        assert got == (trust, green, passes, failed), (label, got)

    # The contract is the ONLY thing that moved: the same fields judged by the
    # hub's four checks are not green.
    row = dict(await repo.get_ci_run_report(db, task_id, SAT_SHA))
    assert ci_trust.green_gap(row, ci_contract.HUB_CONTRACT) is not None


# ---- #1848: a change of the contract revokes what rested on the old one -------

OTHER_CONTRACT = {
    "workflow_path": ".github/workflows/other.yml",
    "required_checks": ["tests"],
}


async def test_a_contract_change_revokes_old_trust(db, db_dsn, monkeypatch):
    from hub.services import lifecycle
    from hub.services.ac_tests import ac_tests_gap

    # 1. verified under A; the contract becomes B (same workflow, one more check).
    pid, sat = await satellite_task(db)
    await _post(db, sat, _sat_attempt(sat))
    await repo.update_task(db, sat, validation_commands='["x"]')
    await db.commit()
    task = dict(await repo.get_task(db, sat))
    assert await ci_trust.pass_gap(db, sat, SAT_SHA) is None
    assert await ac_tests_gap(db, task) is None
    assert await lifecycle._validation_gap_of_the_work(db, task) is None
    attempt = dict((await repo.list_ci_report_attempts(db, sat, SAT_SHA))[0])
    fp_a = attempt["contract_fp"]

    await set_contract(db, pid, SAT_CONTRACT_B)
    fp_b = (await ci_contract.contract_for_task(db, sat)).fingerprint
    assert fp_a != fp_b
    # Nothing was refreshed, and the old row opens nothing.
    row = dict(await repo.get_ci_run_report(db, sat, SAT_SHA))
    assert ci_trust.trust_of(row, fp_b) == ("unverified", "contract_changed")
    assert await ci_trust.trusted_report(db, sat, SAT_SHA) is None
    assert await ci_trust.pass_gap(db, sat, SAT_SHA) is not None
    assert (await _sat_gap(db, sat)).cause == ci_green_proof.UNVERIFIED
    assert not (await steward_evidence._ci_fact(db, sat, SAT_SHA)).is_present
    assert await ac_tests_gap(db, task) is not None, (
        "the AC result is no longer counted"
    )
    assert "контрактом" in (await lifecycle._validation_gap_of_the_work(db, task) or "")
    # The attempt is in the selection of the re-check.
    stale = await ci_contract.attempts_to_recheck(
        db, await ci_contract.settled_attempts(db)
    )
    assert [int(r["id"]) for r in stale] == [attempt["id"]]

    # The poller's sweep re-checks it with no new report: verified under B (the run
    # is the right workflow), but lint is not reported, so not green.
    await ci_trust.sweep(db)
    row = dict(await repo.get_ci_run_report(db, sat, SAT_SHA))
    assert row["contract_fp"] == fp_b
    assert ci_trust.trust_of(row, fp_b) == ("verified", "")
    assert await ci_trust.pass_gap(db, sat, SAT_SHA) is not None
    assert (await _sat_gap(db, sat)).cause == ci_green_proof.NOT_GREEN
    assert [
        dict(r)["contract_fp"]
        for r in await repo.list_ci_report_attempts(db, sat, SAT_SHA)
    ] == [fp_b]
    assert (
        await ci_contract.attempts_to_recheck(
            db, await ci_contract.settled_attempts(db)
        )
        == []
    )

    # 2. unverified(wrong_workflow) under A is final — until the contract changes.
    pid2, wrong = await satellite_task(db, slug="wrong-wf", contract=OTHER_CONTRACT)
    out = await _post(db, wrong, _sat_attempt(wrong))
    assert (out["trust"], out["trust_reason"]) == ("unverified", "wrong_workflow")
    asked = len(GH.calls)
    await ci_trust.refresh(db, wrong, SAT_SHA)
    assert not any(c.startswith("attempt:") for c in GH.calls[asked:]), (
        "final stays final"
    )
    await set_contract(db, pid2, SAT_CONTRACT)
    assert [
        int(r["task_id"])
        for r in await ci_contract.attempts_to_recheck(
            db, await ci_contract.settled_attempts(db)
        )
    ] == [wrong]
    standing = await ci_trust.refresh(db, wrong, SAT_SHA)
    assert standing is not None and standing.verified
    assert await ci_trust.pass_gap(db, wrong, SAT_SHA) is None

    # 3. the contract changes WHILE the refresh is asking GitHub, through ANOTHER
    # connection (another process): the answer computed under the old contract is
    # not written; the round is made again under the new one.
    import asyncio

    import aiosqlite

    pid3, racing = await satellite_task(db, slug="racing")
    from hub.services.ci_report import store_ci_run_report

    await store_ci_run_report(db, racing, **_sat_attempt(racing))
    await db.commit()
    real_attempt = GH.ci_run_attempt
    rounds: list[int] = []
    other = await aiosqlite.connect(db_dsn, uri=True)
    try:
        await other.execute("PRAGMA busy_timeout = 5000")
        other.row_factory = aiosqlite.Row

        async def contract_changes_on_another_connection(*args, **kwargs):
            rounds.append(1)
            if len(rounds) == 1:
                async with asyncio.timeout(10):
                    await set_contract(other, pid3, OTHER_CONTRACT)
            return await real_attempt(*args, **kwargs)

        monkeypatch.setattr(
            GH, "ci_run_attempt", contract_changes_on_another_connection
        )
        from hub.integrations.registry import plugins

        monkeypatch.setattr(plugins.git_ops, "ci_run_attempt", GH.ci_run_attempt)
        async with asyncio.timeout(30):
            standing = await ci_trust.refresh(db, racing, SAT_SHA)
    finally:
        await other.close()
    fp_other = (await ci_contract.contract_for_task(db, racing)).fingerprint
    assert fp_other != ci_contract.contract_from_raw(SAT_CONTRACT, "x").fingerprint
    assert len(rounds) == 2, "the first answer was thrown away, the second asked again"
    assert standing is not None and not standing.verified
    assert (await _standing(db, racing, SAT_SHA))[:2] == (
        "unverified",
        "wrong_workflow",
    )
    attempt = dict((await repo.list_ci_report_attempts(db, racing, SAT_SHA))[0])
    assert attempt["contract_fp"] == fp_other
