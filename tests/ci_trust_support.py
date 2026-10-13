"""A GitHub that tests can set up run by run (#1687).

The hub asks the forge four things about a CI report: the attempt, the runs of
ci.yml for the commit, the run's artifacts and the artifact archive. ``FakeGitHub``
answers all four from tables the test fills, and ``attempt()`` builds the report a
trusted reporter would send for what it just registered — so a test says "this
attempt ran, and here is what it reported" and may then break exactly one thing.

No network: the fake replaces the four methods of ``plugins.git_ops``.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from typing import Any

from hub import config
from hub import repository as repo
from hub.integrations.registry import plugins

REPO = "own/rep"
CI_PATH = ".github/workflows/ci.yml"
GREEN_CHECKS = {"lint": "pass", "format": "pass", "types": "pass", "tests": "pass"}


#: The columns a stored report row has once GitHub confirmed it, for tests that
#: insert a row straight into the table (they test the readers, not the check).
VERIFIED_ROW: dict[str, str] = {
    "trust": "verified",
    "trust_reason": "",
    "attempt_key": "ci:1:1",
    "checks": json.dumps(GREEN_CHECKS),
    "provenance": json.dumps(
        {"run_id": "1", "run_attempt": "1", "run_conclusion": "success"}
    ),
}


def archive(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


class FakeGitHub:
    def __init__(self) -> None:
        self.runs: dict[tuple[str, str], dict[str, Any]] = {}
        self.artifacts: dict[str, list[dict[str, Any]]] = {}
        self.zips: dict[str, bytes] = {}
        self.down = False
        self.zip_down = False
        self.calls: list[str] = []
        self._clock = 0
        self._ids = 1000
        self._runs = 0

    # ---- what the hub asks -------------------------------------------------

    def _facts(self, facts: dict[str, Any], gh_repo: str | None) -> dict[str, Any]:
        asked = gh_repo or config.REPO_NAME or REPO
        return {
            **facts,
            "repository": facts["repository"] or asked,
            "head_repository": facts["head_repository"] or asked,
        }

    async def ci_run_attempt(self, run_id, attempt, repo=None, gh_repo=None, forge=""):
        self.calls.append(f"attempt:{run_id}/{attempt}")
        if self.down:
            return None
        found = self.runs.get((str(run_id), str(attempt)))
        return self._facts(found, gh_repo) if found else {}

    async def ci_runs_for_sha(self, head_sha, repo=None, gh_repo=None, forge=""):
        self.calls.append(f"runs:{head_sha}")
        if self.down:
            return None
        latest: dict[str, dict[str, Any]] = {}
        for (run_id, attempt), facts in self.runs.items():
            if facts["head_sha"] != head_sha:
                continue
            if run_id not in latest or int(attempt) > int(
                latest[run_id]["run_attempt"]
            ):
                latest[run_id] = facts
        return [self._facts(v, gh_repo) for v in latest.values()]

    async def ci_run_artifacts(self, run_id, repo=None, gh_repo=None, forge=""):
        self.calls.append(f"artifacts:{run_id}")
        if self.down:
            return None
        return [dict(a) for a in self.artifacts.get(str(run_id), [])]

    async def ci_artifact_zip(
        self, artifact_id, max_bytes, repo=None, gh_repo=None, forge=""
    ):
        self.calls.append(f"zip:{artifact_id}")
        if self.down or self.zip_down:
            return None
        return self.zips.get(str(artifact_id), b"")

    # ---- what a test sets up -----------------------------------------------

    def tick(self) -> str:
        self._clock += 1
        return f"2026-10-12T10:{self._clock // 60:02d}:{self._clock % 60:02d}Z"

    def attempt(
        self,
        task_id: int,
        head_sha: str,
        *,
        run_id: str | None = None,
        attempt: str = "1",
        started: str | None = None,
        status: str = "completed",
        conclusion: str = "success",
        path: str = CI_PATH,
        event: str = "workflow_dispatch",
        repository: str | None = None,
        head_repository: str | None = None,
        ac_results: dict[str, str] | None = None,
        checks: dict[str, str] | None = None,
        validation_status: str = "pass",
        validation_log: str = "",
        mutations: dict | None = None,
        baseline: dict | None = None,
        artifact: bool = True,
        artifact_in_run: str | None = None,
        stamp: bool = True,
    ) -> dict[str, Any]:
        """Register one attempt on the fake GitHub; return the report it would send."""
        slug = REPO
        self._runs += 1
        run_id = run_id or str(5000 + self._runs)
        self.runs[(run_id, attempt)] = {
            "run_id": run_id,
            "run_attempt": attempt,
            "path": path,
            "head_sha": head_sha,
            "status": status,
            "conclusion": conclusion if status == "completed" else "",
            "event": event,
            "run_started_at": started or self.tick(),
            # None = "whatever repository the hub asks about": the hub compares
            # the repository of the run with its project's, and a test that is
            # not about that comparison need not know the project's name.
            "repository": repository,
            "head_repository": head_repository,
        }
        ac = {"AC-1": "pass"} if ac_results is None else ac_results
        marks = dict(GREEN_CHECKS) if checks is None else checks
        doc: dict[str, Any] = {
            "schema": 1,
            "task_id": task_id,
            "head_sha": head_sha,
            "ac_results": ac,
            "validation_status": validation_status,
            "validation_log": validation_log,
            "reason": "",
            "reported_by": "github-actions",
            "checks": marks,
        }
        run_stamp = {
            "run_id": run_id,
            "run_url": f"https://github.com/{slug}/actions/runs/{run_id}",
            "event": event,
            "at": "2026-10-12T10:59:00Z",
        }
        if mutations is not None:
            doc["mutations"] = mutations
        if baseline is not None:
            doc["baseline"] = baseline
        blob = archive({"results.json": json.dumps(doc).encode()})
        self._ids += 1
        art_id = str(self._ids)
        digest = "sha256:" + hashlib.sha256(blob).hexdigest()
        if artifact:
            self.zips[art_id] = blob
            self.artifacts.setdefault(artifact_in_run or run_id, []).append(
                {
                    "id": art_id,
                    "name": f"hub-ci-results-{attempt}",
                    "digest": digest,
                    "size": len(blob),
                    "expired": False,
                }
            )
        return {
            "head_sha": head_sha,
            "ac_results": ac,
            "checks": marks,
            "validation_status": validation_status,
            "validation_log": validation_log,
            "reported_by": "github-actions",
            "mutations": _stamped(mutations, run_stamp if stamp else None),
            "baseline": _stamped(baseline, run_stamp if stamp else None),
            "provenance": {
                "run_id": run_id,
                "run_attempt": attempt,
                "workflow_path": path,
                "event": event,
                "head_repository": head_repository or slug,
                "run_conclusion": conclusion,
                "artifact_id": art_id,
                "artifact_digest": digest,
            },
        }


def backed_report(
    fake: FakeGitHub, task_id: int, report: dict, run_id: str | None = None
) -> dict:
    """``report`` as a trusted reporter would send it, with its run on ``fake``."""
    sent = fake.attempt(
        task_id,
        report["head_sha"],
        run_id=run_id,
        ac_results=report.get("ac_results") or {},
        checks=report.get("checks") or {},
        validation_status=report.get("validation_status") or "",
        validation_log=report.get("validation_log") or "",
        mutations=report.get("mutations"),
        baseline=report.get("baseline"),
        stamp=False,
    )
    for key in ("reason", "reported_by"):
        if key in report:
            sent[key] = report[key]
    return sent


def backed_body(fake: FakeGitHub, task_id: int, body: dict) -> dict:
    """The JSON body of a CI report POST, with the run behind it on ``fake``."""
    sent = backed_report(fake, task_id, body)
    out = {k: v for k, v in sent.items() if v is not None and k != "provenance"}
    out.update(sent["provenance"])
    out.update({k: v for k, v in body.items() if k in ("submission_generation",)})
    return out


async def accept_backed(db: Any, task_id: int, **report: Any) -> dict:
    """accept_ci_run_report for a run that GitHub (the fake) really has.

    For tests of what the READERS do with a confirmed report. The fake is
    attached to whatever ``plugins.git_ops`` is at the moment of the call, and
    the repository the hub compares with is fixed for the call.
    """
    from hub.services.ci_report import accept_ci_run_report

    git = plugins.git_ops
    fake = getattr(git, "_fake_github", None)
    if fake is None:
        fake = attach_fake_github(git)
        git._fake_github = fake  # type: ignore[attr-defined]
    config.REPO_NAME = REPO  # restored after the test by tests/conftest.py
    return await accept_ci_run_report(
        db, task_id, **backed_report(fake, task_id, report)
    )


def _stamped(block: dict | None, stamp: dict | None) -> dict | None:
    if block is None or stamp is None:
        return block
    return {**block, "provenance": stamp}


_READS = ("ci_run_attempt", "ci_runs_for_sha", "ci_run_artifacts", "ci_artifact_zip")


def install_fake_github(monkeypatch) -> FakeGitHub:
    """Point the four forge reads of ``plugins.git_ops`` at a fresh ``FakeGitHub``."""
    fake = FakeGitHub()
    monkeypatch.setattr(config, "REPO_NAME", REPO)
    for name in _READS:
        monkeypatch.setattr(plugins.git_ops, name, getattr(fake, name), raising=False)
    return fake


def attach_fake_github(target: Any) -> FakeGitHub:
    """The same for a git_ops object a test builds itself (no monkeypatch needed)."""
    fake = FakeGitHub()
    config.REPO_NAME = REPO  # restored after the test by tests/conftest.py
    for name in _READS:
        setattr(target, name, getattr(fake, name))
    return fake


async def seed_verified_report(
    db: Any, task_id: int, head_sha: str, **fields: Any
) -> None:
    """A stored report row GitHub has confirmed, for tests of the READERS.

    Tests of what a gate does with a confirmed report do not need the whole
    check against GitHub — that is tested in test_ci_trust.py — only the row
    the check would have left.
    """
    row: dict[str, Any] = {
        "ac_results": "{}",
        "validation_status": "pass",
        "validation_log": "",
        "reason": "",
        "reported_by": "ci",
        "checks": json.dumps(GREEN_CHECKS),
        "provenance": json.dumps(
            {"run_id": "1", "run_attempt": "1", "run_conclusion": "success"}
        ),
    }
    row.update({k: v for k, v in fields.items() if v is not None})
    for key in ("ac_results", "checks", "provenance", "mutations", "baseline"):
        if key in row and not isinstance(row[key], str):
            row[key] = json.dumps(row[key])
    await repo.upsert_ci_run_report(
        db,
        task_id=task_id,
        head_sha=head_sha,
        trust="verified",
        trust_reason="",
        attempt_key="ci:1:1",
        **row,
    )
