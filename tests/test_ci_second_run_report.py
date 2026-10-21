"""A second green run on one commit must not leave the hub without a report (#1988).

Live case 21.10 (#1980, sha 449384056e61): the dispatch run was green in every
job, but the collector (scripts/ci_report_to_hub.py) read the task card while the
hub answered 502, printed "reporting nothing" and exited 0. The artifact then held
only audit.json, the trusted reporter said "no results.json" without a reason, and
the hub took the newest attempt, which has no report, as the current one
(newer_attempt_without_report) although the earlier run of the same commit was
verified. The rule of ``hub/services/ci_trust.py`` (the newest attempt counts) is
right and stays; what this file pins is that every run of a task branch with the
read key configured leaves a results.json behind.
"""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

from hub import repository as repo
from hub.models import AcceptanceCriterion
from hub.services import ci_green_proof, ci_trust
from hub.services.ci_report import accept_ci_run_report
from tests.ci_trust_support import FakeGitHub, install_fake_github

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
SHA = "sha-pinned"
HEAD = "449384056e6145f5ca9d3c1694106149f0213351"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- AC-1: the collector never leaves a green artifact without results ----


@pytest.fixture
def collector(monkeypatch, tmp_path):
    module = _load("ci_report_to_hub")
    out = tmp_path / "hub-ci-results"
    for key, value in (
        ("HAIPLANE_HUB_URL", "https://hub.example"),
        ("HAIPLANE_HUB_CI_READ_TOKEN", "read-secret"),  # noqa: S105
        ("HAIPLANE_HUB_CI_RESULTS_DIR", str(out)),
        ("HAIPLANE_HUB_CI_CHECKS", "tests=success, lint=success"),
        ("GITHUB_HEAD_REF", "task-1980/x"),
        ("HEAD_SHA", HEAD),
    ):
        monkeypatch.setenv(key, value)
    for key in ("HAIPLANE_HUB_CI_TOKEN", "HAIPLANE_HUB_CI_PYTEST"):
        monkeypatch.delenv(key, raising=False)
    pauses: list[float] = []
    monkeypatch.setattr(module, "_pause", pauses.append, raising=False)
    module.pauses = pauses
    module.results_path = out / "results.json"
    return module


class _Hub:
    """urlopen that answers from a script: an int is an HTTP error, a dict a card."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, req, timeout=0):
        self.calls += 1
        answer = self.answers[min(self.calls, len(self.answers)) - 1]
        if isinstance(answer, int):
            body = io.BytesIO(b"<html><h1>%d</h1></html>" % answer)
            raise urllib.error.HTTPError(req.full_url, answer, "x", {}, body)
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps(answer).encode())


_CARD = {"acceptance_criteria": [], "validation_commands": []}


def _results(module) -> dict:
    assert module.results_path.is_file(), "the run left no results.json behind"
    return json.loads(module.results_path.read_text())


def test_the_collector_never_leaves_a_green_artifact_without_results(
    collector, monkeypatch, capsys
):
    module = collector
    reporter = _load("ci_trusted_report")

    # 1. The live case: the hub answers 502 once or twice, then the card. The
    # read is repeated, and the results are the real ones.
    hub = _Hub(502, 502, _CARD)
    monkeypatch.setattr(module.urllib.request, "urlopen", hub)
    assert module.main() == 0
    assert hub.calls == 3 and len(module.pauses) == 2
    doc = _results(module)
    assert doc["validation_status"] == "skipped" and doc["head_sha"] == HEAD
    assert doc["checks"] == {"tests": "pass", "lint": "pass"}

    # 2. The hub stays down: results.json is still written, says "unknown" and
    # names the cause; the log carries a GitHub error annotation. The run is
    # not turned red (the contract of the action), the report is the signal.
    module.results_path.unlink()
    capsys.readouterr()
    hub = _Hub(502)
    monkeypatch.setattr(module.urllib.request, "urlopen", hub)
    assert module.main() == 0
    assert hub.calls > 1, "a 502 is worth a retry"
    doc = _results(module)
    assert doc["validation_status"] == "unknown" and doc["ac_results"] == {}
    assert "502" in doc["reason"] and "1980" in doc["reason"]
    assert doc["checks"] == {"tests": "pass", "lint": "pass"}, "known facts stay"
    assert "::error" in capsys.readouterr().out
    # ... and the trusted reporter takes exactly this file.
    body = reporter.parse_results(
        module.results_path.read_bytes(), {"head_sha": HEAD}, 1980, {}
    )
    assert body["validation_status"] == "unknown" and "502" in body["reason"]

    # 3. A refusal of the key (4xx) is not repeated, but is not silent either.
    module.results_path.unlink()
    hub = _Hub(403)
    monkeypatch.setattr(module.urllib.request, "urlopen", hub)
    assert module.main() == 0
    assert hub.calls == 1
    assert "403" in _results(module)["reason"]

    # 4. The network breaks (no HTTP answer at all): the same, repeated.
    module.results_path.unlink()
    hub = _Hub(urllib.error.URLError("no route"))
    monkeypatch.setattr(module.urllib.request, "urlopen", hub)
    assert module.main() == 0 and hub.calls > 1
    assert "no route" in _results(module)["reason"]

    # 5. The collector itself crashes after the card was read.
    module.results_path.unlink()
    monkeypatch.setattr(module.urllib.request, "urlopen", _Hub(_CARD))

    def boom(task, head_sha):
        raise RuntimeError("collector blew up")

    monkeypatch.setattr(module, "build_results", boom)
    assert module.main() == 0
    doc = _results(module)
    assert doc["validation_status"] == "unknown"
    assert "collector blew up" in doc["reason"]

    # 6. The reporter names what the artifact held when results.json is not
    # there, and points at the collector step (not a bare "no results.json").
    shown: list[str] = []
    monkeypatch.setattr(reporter, "log", shown.append)
    reporter.send_report(
        {"audit.json": b"{}"},
        {"head_branch": "task-1980/x", "head_sha": HEAD},
        {},
        "https://hub.example",
        "k",
        lambda *a: None,
        "https://github.com",
        "o/r",
    )
    text = " ".join(shown)
    assert "no results.json" in text and "audit.json" in text
    assert "Collect AC test" in text


# ---- AC-2 / AC-3: the rule of the current attempt, two runs of one sha ------

GH: FakeGitHub = FakeGitHub()


@pytest.fixture
def _github(monkeypatch):
    global GH
    GH = install_fake_github(monkeypatch)


async def _task(db) -> int:
    task_id = await repo.create_task(
        db,
        title="Two runs, one commit",
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
    await repo.bump_submission_generation(db, task_id)
    await repo.update_task(db, task_id, submission_sha=SHA)
    await db.commit()
    return task_id


async def _post(db, task_id: int, sent: dict) -> dict:
    return await accept_ci_run_report(db, task_id, **sent)


async def _standing(db, task_id: int) -> tuple[str, str, str]:
    row = dict(await repo.get_ci_run_report(db, task_id, SHA))
    return row["trust"], row["trust_reason"], row["attempt_key"]


async def _gap(db, task_id: int):
    from hub.services import ci_contract

    rows = [dict(r) for r in await repo.list_acceptance_criteria(db, task_id)]
    report = await repo.get_ci_run_report(db, task_id, SHA)
    contract = await ci_contract.contract_for_task(db, task_id)
    return ci_green_proof.proof_gap(
        dict(report) if report else None, rows, SHA, contract
    )


async def _ac(db, task_id: int) -> list[tuple[str, str]]:
    return sorted(
        (r["ac_id"], r["status"]) for r in await repo.list_ac_test_results(db, task_id)
    )


async def test_a_second_green_run_on_the_same_sha_does_not_block_submission(
    db, _github
):
    task_id = await _task(db)
    pr = GH.attempt(task_id, SHA, run_id="7001", event="pull_request")
    assert (await _post(db, task_id, pr))["trust"] == "verified"
    assert await _gap(db, task_id) is None

    # The dispatch run starts later, on the same commit, and reports too.
    dispatch = GH.attempt(task_id, SHA, run_id="7002", event="workflow_dispatch")
    out = await _post(db, task_id, dispatch)
    assert (out["trust"], out["applied"]) == ("verified", True)

    assert await ci_trust.refresh(db, task_id, SHA) is not None
    assert await _standing(db, task_id) == ("verified", "", "ci:7002:1")
    assert await _ac(db, task_id) == [("AC-1", "pass")]
    assert await _gap(db, task_id) is None, "the submission gate has nothing to refuse"


async def test_a_newer_red_or_running_attempt_still_blocks(db, _github):
    red = dict(
        conclusion="failure", validation_status="fail", ac_results={"AC-1": "fail"}
    )

    # (a) a newer red attempt WITH a report: the report is fail.
    one = await _task(db)
    await _post(db, one, GH.attempt(one, SHA, run_id="7101"))
    assert await _gap(db, one) is None
    await _post(db, one, GH.attempt(one, SHA, run_id="7102", **red))
    assert (await _standing(db, one))[2] == "ci:7102:1"
    assert await _ac(db, one) == [("AC-1", "fail")]
    gap = await _gap(db, one)
    assert gap is not None and gap.cause == ci_green_proof.RED

    # (b) a newer red attempt WITHOUT a report.
    two = await _task(db)
    await _post(db, two, GH.attempt(two, SHA, run_id="7201"))
    GH.attempt(two, SHA, run_id="7202", artifact=False, **red)
    await ci_trust.refresh(db, two, SHA)
    assert (await _standing(db, two))[:2] == (
        "unverified",
        "newer_attempt_without_report",
    )
    assert await _ac(db, two) == [], "the earlier green is no proof any more"
    gap = await _gap(db, two)
    assert gap is not None and gap.cause == ci_green_proof.UNVERIFIED

    # (c) a newer attempt that is still running.
    three = await _task(db)
    await _post(db, three, GH.attempt(three, SHA, run_id="7301"))
    GH.attempt(three, SHA, run_id="7302", status="in_progress", artifact=False)
    await ci_trust.refresh(db, three, SHA)
    assert (await _standing(db, three))[:2] == (
        "unverified",
        "current_attempt_incomplete",
    )
    assert await _ac(db, three) == []
    gap = await _gap(db, three)
    assert gap is not None and gap.cause == ci_green_proof.UNVERIFIED
