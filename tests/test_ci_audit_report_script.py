"""A dependency advisory becomes work, not a repo-wide freeze (#611).

On 07.08.2026 PYSEC-2026-3552 appeared after develop's last green run. The audit
step failed the CI job, the delivery gate reads that job, and every task stopped
— PR #247 went red over a diff that had nothing to do with cryptography. The fix
needed a manual merge because a dependency bump has no task in the hub.

Two halves are tested here, and the second matters as much as the first: the
audit must not block delivery, AND the finding must land somewhere a human will
see it. A step that can never fail is a step nobody reads; without the draft we
would only have traded a loud false stop for silent rot.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "ci_report_audit_to_hub.py"
)
_WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def _load():
    spec = importlib.util.spec_from_file_location("ci_report_audit_to_hub", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script():
    return _load()


@pytest.fixture(autouse=True)
def _clean_prefixed_env(monkeypatch):
    """Neither prefix may leak in from the developer's shell (Task 4)."""
    for suffix in ("HUB_URL", "HUB_CI_TOKEN"):
        monkeypatch.delenv(f"HAIPLANE_{suffix}", raising=False)


def test_hub_credentials_accepted_under_the_haiplane_prefix(
    script, monkeypatch, tmp_path
):
    """The filer reads the HAIPLANE_* credentials."""
    report = tmp_path / "audit.json"
    report.write_text(
        json.dumps(
            _report({"id": "PYSEC-9", "fix_versions": ["1.0"], "description": "d"})
        )
    )
    monkeypatch.setenv("AUDIT_JSON", str(report))
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://hub.example")
    monkeypatch.setenv("HAIPLANE_HUB_CI_TOKEN", "new-prefix-token")  # noqa: S105

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        script,
        "hub_post",
        lambda base, token, path, payload: (
            seen.append((base, token)) or {"id": 1, "status": "draft"}
        ),
    )
    assert script.main() == 0
    assert seen == [("https://hub.example", "new-prefix-token")]


def _report(*vulns: dict) -> dict:
    return {
        "dependencies": [
            {"name": "cryptography", "version": "49.0.0", "vulns": list(vulns)}
        ]
    }


def test_the_audit_step_cannot_block_delivery():
    # AC-1 (#611): read from the workflow itself, because the property that
    # matters is a property of the pipeline, not of our code. The gate keys on
    # the job's outcome, so the audit must not be able to change it — while the
    # steps that judge THIS PR's own code (lint, tests, secrets) still must.
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    job = workflow["jobs"]["test"]
    steps = {s["name"]: s for s in job["steps"]}

    assert job["name"] == "Ruff and pytest", (
        "the delivery gate finds the job by this name (#605/#606)"
    )
    assert steps["Dependency vulnerability audit"].get("continue-on-error") is True
    assert steps["File audit findings as Hub drafts"].get("continue-on-error") is True

    for blocking in ("Lint", "Test", "Secret scan", "Static security scan"):
        assert not steps[blocking].get("continue-on-error"), (
            f"{blocking} judges this PR's own code and must still fail the job"
        )


def test_each_vulnerability_becomes_a_hub_draft(script, monkeypatch, tmp_path):
    # AC-2 (#611): the finding leaves a trace with an owner, not just a log line.
    report = tmp_path / "audit.json"
    report.write_text(
        json.dumps(
            _report(
                {
                    "id": "PYSEC-2026-3552",
                    "fix_versions": ["50.0.0"],
                    "description": "a flaw",
                },
                {"id": "GHSA-xxxx", "fix_versions": [], "description": ""},
            )
        )
    )
    monkeypatch.setenv("AUDIT_JSON", str(report))
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://hub.example")
    monkeypatch.setenv("HAIPLANE_HUB_CI_TOKEN", "irrelevant")  # noqa: S105

    sent: list[dict] = []
    monkeypatch.setattr(
        script,
        "hub_post",
        lambda base, token, path, payload: (
            sent.append((path, payload)) or {"id": 900 + len(sent), "status": "draft"}
        ),
    )

    assert script.main() == 0
    assert [p for p, _ in sent] == ["/api/tasks", "/api/tasks"]
    first = sent[0][1]
    assert "PYSEC-2026-3552" in first["title"]
    assert "cryptography" in first["title"]
    assert "50.0.0" in first["description"]
    assert first["source"] == "agent", (
        "source=agent keeps the human approval gate — the hub files it as a draft"
    )
    assert first["human_owner"], "a finding with no owner is a finding nobody handles"
    # An advisory with no fix must say so rather than leave the field blank.
    assert "исправленной версии пока нет" in sent[1][1]["description"]


def test_repeated_runs_neither_duplicate_nor_conflict(script, monkeypatch, tmp_path):
    # AC-3 (#611): the hub refuses a known client_request_id that arrives with a
    # DIFFERENT payload, so anything run-specific in the body — a run number, a
    # branch, a timestamp — would turn the second PR of the day into a 409
    # instead of silence. The payload must be a function of the advisory alone.
    report = tmp_path / "audit.json"
    report.write_text(
        json.dumps(
            _report({"id": "PYSEC-1", "fix_versions": ["2.0"], "description": "d"})
        )
    )
    monkeypatch.setenv("AUDIT_JSON", str(report))
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://hub.example")
    monkeypatch.setenv("HAIPLANE_HUB_CI_TOKEN", "irrelevant")  # noqa: S105

    payloads: list[dict] = []
    monkeypatch.setattr(
        script,
        "hub_post",
        lambda base, token, path, payload: payloads.append(payload) or {"id": 1},
    )

    # Two runs from different branches and different run numbers.
    monkeypatch.setenv("GITHUB_REF_NAME", "task-611/a")
    monkeypatch.setenv("GITHUB_RUN_ID", "111")
    script.main()
    monkeypatch.setenv("GITHUB_REF_NAME", "develop")
    monkeypatch.setenv("GITHUB_RUN_ID", "222")
    script.main()

    assert payloads[0]["client_request_id"] == "pip-audit:PYSEC-1"
    assert payloads[0] == payloads[1], (
        "identical advisory must produce an identical payload, or idempotency "
        "turns into a 409 on the next run"
    )


def test_a_clean_audit_creates_nothing(script, monkeypatch, tmp_path):
    # AC-4 (#611): no findings, no noise.
    report = tmp_path / "audit.json"
    report.write_text(
        json.dumps({"dependencies": [{"name": "x", "version": "1", "vulns": []}]})
    )
    monkeypatch.setenv("AUDIT_JSON", str(report))
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://hub.example")
    monkeypatch.setenv("HAIPLANE_HUB_CI_TOKEN", "irrelevant")  # noqa: S105

    called: list = []
    monkeypatch.setattr(script, "hub_post", lambda *a, **k: called.append(a) or {})

    assert script.main() == 0
    assert called == []


def test_the_reporter_never_fails_the_job(script, monkeypatch, tmp_path, capsys):
    # AC-5 (#611): every failure path ends in exit 0 WITH a reason. A reporting
    # hiccup that reddened the job would block merges for every task — the exact
    # failure this task exists to remove.
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://hub.example")
    monkeypatch.setenv("HAIPLANE_HUB_CI_TOKEN", "irrelevant")  # noqa: S105

    # 1. No report file at all (the audit step itself died early).
    monkeypatch.setenv("AUDIT_JSON", str(tmp_path / "missing.json"))
    assert script.main() == 0
    assert "no audit report" in capsys.readouterr().out

    # 2. Malformed JSON — a pip-audit format change must not raise.
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    monkeypatch.setenv("AUDIT_JSON", str(broken))
    assert script.main() == 0
    assert "unreadable" in capsys.readouterr().out

    # 3. A shape we do not recognise: keys missing, wrong types.
    odd = tmp_path / "odd.json"
    odd.write_text(json.dumps({"dependencies": [{"vulns": [{"no_id": 1}]}, "junk"]}))
    monkeypatch.setenv("AUDIT_JSON", str(odd))
    assert script.main() == 0

    # 4. Findings exist but the hub is unreachable.
    report = tmp_path / "audit.json"
    report.write_text(json.dumps(_report({"id": "PYSEC-2", "fix_versions": []})))
    monkeypatch.setenv("AUDIT_JSON", str(report))
    monkeypatch.setattr(script, "hub_post", lambda *a, **k: None)
    assert script.main() == 0
    assert "not filed" in capsys.readouterr().out

    # 5. Findings exist but no credentials: they must still be printed, so the
    #    run is not silent about a vulnerability it could not file.
    monkeypatch.delenv("HAIPLANE_HUB_CI_TOKEN", raising=False)
    assert script.main() == 0
    out = capsys.readouterr().out
    assert "PYSEC-2" in out and "not configured" in out
