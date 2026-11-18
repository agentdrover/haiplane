from copy import deepcopy
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

from hub.integrations.forge import github_ci_selection as selection
from hub.integrations.forge.github import GitHubForge
from hub.integrations.protocols import CIProbeOutcome

REPO = "agentdrover/home-infra"
HEAD_REF = "task-2372/olymp-figure-rollout"
HEAD = "60c77f897b158729668a029f2da3ad3abe7bc558"
BASE = "4db39d5bfd83c220a15f4723bf43ae85836e56c2"
OLD, NEW = 44332283068, 44333582843


def run(number, event, suite, created, conclusion):
    return {
        "id": number,
        "event": event,
        "check_suite_id": suite,
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
        "head_branch": HEAD_REF,
        "head_sha": HEAD,
        "path": ".github/workflows/tests.yml",
        "workflow_id": 397248744,
        "status": "completed",
        "conclusion": conclusion,
        "run_attempt": 1,
        "pull_requests": [{"number": 229}] if event == "pull_request" else [],
        "created_at": created,
    }


def step(name):
    return {"name": name, "status": "completed", "conclusion": "success"}


class Evidence:
    def __init__(self):
        self.old = run(
            OLD, "pull_request", 120005696734, "2026-11-18T15:09:13Z", "failure"
        )
        self.native = run(
            NEW, "workflow_dispatch", 120009237489, "2026-11-18T15:25:14Z", "success"
        )
        self.runs = [self.native, self.old]
        self.pr = {
            "state": "open",
            "head": {"ref": HEAD_REF, "sha": HEAD, "repo": {"full_name": REPO}},
            "base": {"ref": "main", "sha": BASE, "repo": {"full_name": REPO}},
        }
        self.jobs = {OLD: self._make_jobs(self.old), NEW: self._make_jobs(self.native)}
        self.checks = [
            self._as_check(r, job) for r in self.runs for job in self.jobs[r["id"]]
        ]
        self.rules = []
        self.statuses = []
        self.base_blob = self.head_blob = "bec1443cb1890775c3e11d1d2858eafe3c5fcb55"
        self.missing = set()
        self.pr_reads = 0
        self.switch_head = False
        self.requests = []

    @staticmethod
    def _make_jobs(r):
        jobs = []
        is_old = r["id"] == OLD
        names = [
            *(
                (f"test-launch.sh (part {n}/4)", f"launcher-{n}", "test-launch.sh")
                for n in range(4)
            ),
            (
                "other shell tests",
                "scripts-0",
                "tests/test-*.sh and hosts/**/test-*.sh (except test-launch.sh)",
            ),
            ("python tests (k8 agents)", "python-0", "unittest"),
        ]
        for n, (name, suffix, test) in enumerate(names):
            job = {
                "id": r["id"] * 100 + n,
                "name": name,
                "run_id": r["id"],
                "run_attempt": r["run_attempt"],
                "head_sha": HEAD,
                "status": "completed",
                "conclusion": r["conclusion"],
                "labels": ["ubuntu-24.04"]
                if is_old
                else [f"k8-home-infra-tests-{NEW}-1-{suffix}"],
                "runner_id": 0 if is_old else 100 + n,
                "steps": []
                if is_old
                else [
                    step(
                        "Run actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"
                    ),
                    step("Record k8 checkout and runner"),
                    step(test),
                ],
            }
            jobs.append(job)
        for n, name in enumerate(
            (
                "k8 full suite (slot ${{ matrix.slot }}/2)",
                "Collect Hub evidence on k8",
                "Collect Hub evidence",
            ),
            start=6,
        ):
            jobs.append(
                {
                    **jobs[0],
                    "id": r["id"] * 100 + n,
                    "name": name,
                    "labels": ["ubuntu-24.04"],
                    "steps": [],
                    "runner_id": 0,
                    "conclusion": "failure" if is_old and n == 8 else "skipped",
                }
            )
        return jobs

    @staticmethod
    def _as_check(r, job):
        return {
            "id": job["id"],
            "name": job["name"],
            "head_sha": HEAD,
            "app": {"slug": "github-actions"},
            "check_suite": {"id": r["check_suite_id"]},
            "status": job["status"],
            "conclusion": job["conclusion"],
        }

    async def api(self, endpoint, **_kwargs):
        self.requests.append(endpoint)
        parsed = urlsplit(endpoint)
        path = parsed.path.removeprefix(f"repos/{REPO}/")
        query = parse_qs(parsed.query)
        if (path, query.get("page", ["1"])[0]) in self.missing:
            return None, False
        if path == "pulls/229":
            self.pr_reads += 1
            result = deepcopy(self.pr)
            if self.switch_head and self.pr_reads > 1:
                result["head"]["sha"] = "c" * 40
            return result, False
        if path.startswith("contents/"):
            return {
                "sha": self.head_blob if query["ref"] == [HEAD] else self.base_blob
            }, False
        if path == "rules/branches/main":
            return self.rules, False
        if path == "branches/main":
            return {"protected": False, "commit": {"sha": BASE}}, False
        if path.endswith("/annotations"):
            return [
                {"annotation_level": "failure", "message": selection.BILLING_MESSAGE}
            ], False
        if path.endswith("/statuses"):
            return self.statuses, False
        if path.endswith("/jobs"):
            rows, key = self.jobs[int(path.split("/")[2])], "jobs"
        elif path.endswith("/check-runs"):
            rows, key = self.checks, "check_runs"
        elif path == "actions/runs":
            rows, key = self.runs, "workflow_runs"
        else:
            raise AssertionError(endpoint)
        page = int(query["page"][0])
        size = int(query["per_page"][0])
        return {
            "total_count": len(rows),
            key: deepcopy(rows[(page - 1) * size : page * size]),
        }, False

    async def probe(
        self, monkeypatch, primary=(0, '[{"name":"hosted","state":"FAILURE"}]', "")
    ):
        forge = GitHubForge()
        monkeypatch.setattr(forge, "_api_json", self.api)
        monkeypatch.setattr(
            "hub.integrations.forge.github._gh", AsyncMock(return_value=primary)
        )
        return await forge.check_pr_ci(
            229,
            gh_repo=REPO,
            contract_fingerprint=selection.PILOT_CONTRACT,
        )


async def test_later_equivalent_full_k8_run_supersedes_old_billing_failure(monkeypatch):
    e = Evidence()
    result = await e.probe(monkeypatch)
    assert result.outcome is CIProbeOutcome.passed
    assert result.reason == "ci_equivalent_native_run"
    assert result.details and all(
        str(part) in result.details for part in (OLD, NEW, HEAD, "billing-only")
    )


@pytest.mark.parametrize(
    "status,conclusion,expected",
    [
        ("queued", None, CIProbeOutcome.pending),
        ("completed", "failure", CIProbeOutcome.failed),
        ("completed", "cancelled", CIProbeOutcome.failed),
        ("completed", "timed_out", CIProbeOutcome.failed),
        ("completed", "future-state", CIProbeOutcome.unavailable),
    ],
)
async def test_newer_attempt_never_falls_back_to_old_green(
    monkeypatch, status, conclusion, expected
):
    e = Evidence()
    e.runs.insert(
        0, {**e.native, "id": NEW + 1, "status": status, "conclusion": conclusion}
    )
    assert (await e.probe(monkeypatch)).outcome is expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_repository", {"full_name": "other/home-infra"}),
        ("repository", {"full_name": "other/home-infra"}),
        ("head_sha", "a" * 40),
        ("head_branch", "main"),
        ("workflow_id", 56),
        ("path", ".github/workflows/other.yml"),
        ("event", "push"),
    ],
)
async def test_identity_and_independent_required_checks_remain_gates(
    monkeypatch, field, value
):
    e = Evidence()
    e.native[field] = value
    assert (await e.probe(monkeypatch)).outcome is not CIProbeOutcome.passed
    e = Evidence()
    e.checks.append(
        {
            **e.checks[0],
            "id": 1,
            "name": "independent gate",
            "conclusion": "failure",
        }
    )
    assert (await e.probe(monkeypatch)).outcome is CIProbeOutcome.failed
    e = Evidence()
    e.rules = [{"type": "required_status_checks"}]
    assert (await e.probe(monkeypatch)).outcome is CIProbeOutcome.unavailable


@pytest.mark.parametrize(
    "problem",
    ["missing job", "skipped step", "bogus label", "unknown blob", "collector missing"],
)
async def test_incomplete_or_unknown_coverage_cannot_supersede(monkeypatch, problem):
    e = Evidence()
    job = e.jobs[NEW][0]
    if problem == "missing job":
        e.jobs[NEW].pop(0)
    elif problem == "skipped step":
        job["steps"][-1]["conclusion"] = "skipped"
    elif problem == "bogus label":
        job["labels"] = ["self-hosted"]
    elif problem == "unknown blob":
        e.head_blob = "a" * 40
    else:
        # A collector job declared as successful must prove its own steps.
        e.jobs[NEW][7]["conclusion"] = "success"
    assert (await e.probe(monkeypatch)).outcome is CIProbeOutcome.unavailable


async def test_pagination_read_failures_and_head_races_fail_closed(monkeypatch):
    monkeypatch.setattr(selection, "PAGE_SIZE", 3)
    e = Evidence()
    assert (await e.probe(monkeypatch)).outcome is CIProbeOutcome.passed
    assert any("/jobs?" in path and "page=3" in path for path in e.requests)
    e = Evidence()
    e.missing.add((f"commits/{HEAD}/check-runs", "2"))
    assert (await e.probe(monkeypatch)).outcome is CIProbeOutcome.unavailable
    e = Evidence()
    e.switch_head = True
    result = await e.probe(monkeypatch)
    assert result.outcome is CIProbeOutcome.unavailable
    assert result.details and "changed" in result.details


async def test_primary_and_fallback_share_selection_rules(monkeypatch):
    primary = await Evidence().probe(monkeypatch)
    fallback = await Evidence().probe(monkeypatch, primary=(1, "", "HTTP 403"))
    assert primary == fallback
    assert primary.outcome is CIProbeOutcome.passed
    e = Evidence()
    e.missing.add((f"commits/{HEAD}/check-runs", "1"))
    assert (
        await e.probe(monkeypatch, primary=(1, "", "HTTP 403"))
    ).outcome is CIProbeOutcome.unavailable


async def test_collector_enabled_profile_requires_proof_and_preserves_trust_boundary(
    monkeypatch,
):
    e = Evidence()
    e.head_blob = "f602fcbfef42ed6b93cd703ca0d48517f0b33a2e"
    assert (await e.probe(monkeypatch)).outcome is CIProbeOutcome.unavailable
    collector = e.jobs[NEW][7]
    collector.update(
        conclusion="success",
        labels=[f"k8-home-infra-tests-{NEW}-1-evidence-0"],
        runner_id=200,
        steps=[
            step("Run actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"),
            step("Record k8 checkout and runner"),
            step("Summarize selected suite"),
            step("Collect AC test and validation results for the Hub"),
            step("Upload results for the trusted k8 sender"),
        ],
    )
    e.checks = [e._as_check(r, job) for r in e.runs for job in e.jobs[r["id"]]]
    result = await e.probe(monkeypatch)
    assert result.outcome is CIProbeOutcome.passed
    assert "native collector" in (result.details or "")
    assert not any("/artifacts" in evidence for evidence in e.requests)
