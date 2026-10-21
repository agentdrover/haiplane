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
from hub.services import ci_green_proof
from hub.services.ci_report import accept_ci_run_report
from tests.ci_trust_support import FakeGitHub, install_fake_github

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
SHA = "sha-pinned"
HEAD = "ab12" * 10


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
            body = io.BytesIO(b"<html><h1>%d</h1> secret-project-slug</html>" % answer)
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
    assert doc["reason"] == "card_read_failed: HTTP 502"
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
    # The body of the refusal names projects: it is not logged, at any code.
    shown = capsys.readouterr().out
    assert "403" in shown and "secret-project-slug" not in shown

    # 4. The network breaks (no HTTP answer at all): the same, repeated.
    module.results_path.unlink()
    hub = _Hub(urllib.error.URLError("no route"))
    monkeypatch.setattr(module.urllib.request, "urlopen", hub)
    assert module.main() == 0 and hub.calls > 1
    assert _results(module)["reason"] == "card_read_failed: unreachable (URLError)"

    # 5. The collector itself crashes after the card was read.
    module.results_path.unlink()
    monkeypatch.setattr(module.urllib.request, "urlopen", _Hub(_CARD))

    def boom(task, head_sha):
        raise RuntimeError("collector blew up")

    monkeypatch.setattr(module, "build_results", boom)
    assert module.main() == 0
    doc = _results(module)
    assert doc["validation_status"] == "unknown"
    assert doc["reason"] == "collector_crashed: RuntimeError"

    # 5b. The text of an exception never reaches results.json or the log.
    module.results_path.unlink()
    capsys.readouterr()
    leak = "ochk_SECRET123"
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        _Hub(urllib.error.URLError(leak), OSError(leak), TimeoutError(leak)),
    )
    assert module.main() == 0
    assert leak not in module.results_path.read_text()
    assert leak not in capsys.readouterr().out
    module.results_path.unlink()
    monkeypatch.setattr(module.urllib.request, "urlopen", _Hub(_CARD))

    def leaky(task, head_sha):
        raise RuntimeError(leak)

    monkeypatch.setattr(module, "build_results", leaky)
    assert module.main() == 0
    assert leak not in module.results_path.read_text()
    assert leak not in capsys.readouterr().out

    # 5c. When even the failure result cannot be written, the step is red: this
    # is the only case where it is (nothing is left that the hub could read).
    def disk_full(payload, task_id):
        raise OSError(leak)

    monkeypatch.setattr(module, "write_results", disk_full)
    assert module.main() == 1
    assert leak not in capsys.readouterr().out

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


# ---- every hub-facing script logs the code of a refusal, never its body ------


@pytest.mark.parametrize(
    ("name", "call"),
    [
        ("ci_report_to_hub", lambda m: m.hub_request("https://h/x", "t")),
        ("ci_trusted_report", lambda m: m.hub_post("https://h/x", "t", {})),
        (
            "ci_report_audit_to_hub",
            lambda m: m.hub_post("https://h", "t", "/x", {}),
        ),
    ],
)
def test_no_script_logs_the_body_of_a_hub_refusal(name, call, monkeypatch, capsys):
    module = _load(name)
    leak = "ochk_SECRET123"

    def refuse(req, timeout=0):
        body = io.BytesIO(b"projects of the key: secret-project-slug")
        raise urllib.error.HTTPError(req.full_url, 403, "no", {}, body)

    monkeypatch.setattr(module.urllib.request, "urlopen", refuse)
    assert call(module) is None
    shown = capsys.readouterr().out
    assert "403" in shown and "secret-project-slug" not in shown

    def broken(req, timeout=0):
        raise OSError(leak)

    monkeypatch.setattr(module.urllib.request, "urlopen", broken)
    assert call(module) is None
    shown = capsys.readouterr().out
    assert "OSError" in shown and leak not in shown


# ---- the refusal says why -----------------------------------------------------

GH: FakeGitHub = FakeGitHub()


@pytest.fixture
def _github(monkeypatch):
    global GH
    GH = install_fake_github(monkeypatch)


async def test_a_report_without_results_names_the_collectors_reason(db, _github):
    """A collector that failed says why in the refusal, not only "unknown"."""
    from hub.services import ci_contract

    task_id = await repo.create_task(
        db,
        title="x",
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
    sent = GH.attempt(task_id, SHA, ac_results={}, validation_status="unknown")
    sent["reason"] = "card_read_failed: HTTP 502"
    await accept_ci_run_report(db, task_id, **sent)
    rows = [dict(r) for r in await repo.list_acceptance_criteria(db, task_id)]
    report = await repo.get_ci_run_report(db, task_id, SHA)
    gap = ci_green_proof.proof_gap(
        dict(report), rows, SHA, await ci_contract.contract_for_task(db, task_id)
    )
    # The task has a bound AC test, so the missing result is "red"; the cause
    # the collector gave leads the violations either way.
    assert gap is not None and gap.cause == ci_green_proof.RED
    assert "CI-отчёт без результатов" in gap.violations[0]
    assert "card_read_failed: HTTP 502" in gap.violations[0]

    # A task with no bound AC test: the same words on the validation cause.
    await repo.replace_acceptance_criteria(db, task_id, [])
    rows = [dict(r) for r in await repo.list_acceptance_criteria(db, task_id)]
    gap = ci_green_proof.proof_gap(
        dict(report), rows, SHA, await ci_contract.contract_for_task(db, task_id)
    )
    assert gap is not None and gap.cause == ci_green_proof.VALIDATION_NOT_PASS
    assert "card_read_failed: HTTP 502" in gap.violations[0]


# ---- AC-2 / AC-3: the real submission with two runs on one sha -----------------

_GREEN = {"lint": "pass", "format": "pass", "types": "pass", "tests": "pass"}
_PASS = {
    "validation_status": "pass",
    "checks": _GREEN,
    "ac_results": {"AC-1": "pass", "AC-2": "pass"},
}


async def _submit_gate_task(db, slug: str) -> int:
    from tests import test_red_test_gate as gate
    from tests.test_submit_gates import _ci_task

    task_id = await _ci_task(db, slug, "require")
    global GH
    GH = gate.GH
    return task_id


async def _report(client, task_id: int, *, event: str = "workflow_dispatch", **body):
    from tests.test_red_test_gate import _TIP, _post_as_ci

    real = GH.attempt
    GH.attempt = lambda *a, **k: real(*a, event=event, **k)  # type: ignore[method-assign]
    try:
        resp = await _post_as_ci(client, task_id, {"head_sha": _TIP, **body})
    finally:
        GH.attempt = real  # type: ignore[method-assign]
    assert resp.status_code == 200, resp.text


async def test_a_second_green_run_on_the_same_sha_does_not_block_submission(client, db):
    from tests.test_red_test_gate import _submit

    task_id = await _submit_gate_task(db, "two-green")
    await _report(client, task_id, event="pull_request", **_PASS)
    await _report(client, task_id, event="workflow_dispatch", **_PASS)

    resp, dispatch = await _submit(client, task_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["submission_generation"] == 1
    dispatch.assert_awaited_once()
    row = dict(await repo.get_ci_run_report(db, task_id, "c" * 40))
    dispatch_run = next(
        run_id
        for (run_id, _), f in GH.runs.items()
        if f["event"] == "workflow_dispatch"
    )
    assert row["trust"] == "verified"
    assert row["attempt_key"] == f"ci:{dispatch_run}:1", "the newest run is current"
    assert len(await repo.list_ci_report_attempts(db, task_id, "c" * 40)) == 2


async def test_a_newer_red_or_running_attempt_still_blocks(client, db):
    from tests.test_red_test_gate import _TIP, _detail, _submit

    red = {**_PASS, "validation_status": "fail", "ac_results": {"AC-1": "fail"}}
    red["checks"] = {**_GREEN, "tests": "fail"}

    # (a) a newer red attempt WITH a report: the report is fail.
    one = await _submit_gate_task(db, "newer-red")
    await _report(client, one, **_PASS)
    await _report(client, one, **red)
    resp, dispatch = await _submit(client, one)
    assert resp.status_code == 422, resp.text
    assert _detail(resp)["cause"] == "red"
    dispatch.assert_not_awaited()

    # (b) a newer red attempt WITHOUT a report.
    two = await _submit_gate_task(db, "newer-silent")
    await _report(client, two, **_PASS)
    GH.attempt(two, _TIP, conclusion="failure", artifact=False)
    resp, dispatch = await _submit(client, two)
    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["cause"] == "unverified"
    assert "newer_attempt_without_report" in detail["message"]
    dispatch.assert_not_awaited()

    # (c) a newer attempt that is still running.
    three = await _submit_gate_task(db, "newer-running")
    await _report(client, three, **_PASS)
    GH.attempt(three, _TIP, status="in_progress", artifact=False)
    resp, dispatch = await _submit(client, three)
    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["cause"] == "unverified"
    assert "current_attempt_incomplete" in detail["message"]
    dispatch.assert_not_awaited()
