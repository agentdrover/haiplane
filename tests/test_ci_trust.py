"""The hub asks GitHub whether a CI report is true (#1687).

A report is evidence only when the run, the attempt and the artifact it names
exist on GitHub and say what the report says. These tests run against a fake
GitHub (tests/ci_trust_support.py); nothing here touches the network.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from hub import repository as repo
from hub.models import AcceptanceCriterion
from hub.services import ci_artifact, ci_green_proof, ci_trust, steward_evidence
from hub.services.ci_report import accept_ci_run_report, adopt_ci_run_report
from tests.ci_trust_support import (
    GREEN_CHECKS,
    FakeGitHub,
    archive,
    install_fake_github,
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
    return ci_green_proof.proof_gap(dict(report) if report else None, rows, sha)


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
        assert not ci_trust.is_verified(dict(row)) or ci_trust.green_gap(dict(row))
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
        assert ci_green_proof.proof_gap(report, [], "old-sha").cause == "unverified"
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
