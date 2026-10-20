"""#1919: the PR run adopts the evidence of the dispatch run on the same head.

The script decides inside job ``treedup``; here it runs against a temporary
repository (a real merge commit, like ``refs/pull/N/merge``) and a fake GitHub
API. Anything unexpected must come out as a recount, never as an exception.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "adopt_evidence", ROOT / "scripts" / "adopt_evidence.py"
)
assert _spec is not None and _spec.loader is not None
adopt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(adopt)

REPO = "agentdrover/haiplane"
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


class Repo:
    """develop with one commit, a branch with one commit, and their merge."""

    def __init__(self, path: Path, *, base_moved: bool = False) -> None:
        self.path = path
        path.mkdir()
        self.git("init", "-b", "develop")
        (path / "a.txt").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.fork = self.git("rev-parse", "HEAD")
        self.git("checkout", "-b", "task-1")
        (path / "b.txt").write_text("branch\n")
        self.git("add", ".")
        self.git("commit", "-m", "branch")
        self.head = self.git("rev-parse", "HEAD")
        self.git("checkout", "develop")
        if base_moved:
            (path / "c.txt").write_text("moved\n")
            self.git("add", ".")
            self.git("commit", "-m", "develop moved")
        self.tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "--detach", "develop")
        self.git("merge", "--no-ff", "-m", "merge", self.head)

    def git(self, *args: str) -> str:
        return subprocess.run(  # nosec B603 B607
            ["git", *args],
            cwd=self.path,
            env=_GIT_ENV,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()


def _documents(repo: Repo, **override: dict) -> tuple[dict, dict]:
    mutations = {
        "state": "ran",
        "base": "origin/develop",
        "head": repo.head,
        "merge_base": repo.tip,
        "base_tip": repo.tip,
    }
    baseline = {
        "state": "ran",
        "base_ref": "origin/develop",
        "head": repo.head,
        "merge_base": repo.tip,
        "base_tip": repo.tip,
    }
    mutations.update(override.get("mutations", {}))
    baseline.update(override.get("baseline", {}))
    return mutations, baseline


def _archive(mutations: dict, baseline: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(adopt.MUTATIONS_FILE, json.dumps(mutations))
        archive.writestr(adopt.BASELINE_FILE, json.dumps(baseline))
    return buffer.getvalue()


class FakeGitHub:
    def __init__(
        self,
        archive: bytes,
        *,
        runs: list[dict] | None = None,
        job_conclusion: str = "success",
        expired: bool = False,
    ) -> None:
        self.archive = archive
        self.runs = (
            runs
            if runs is not None
            else [{"id": 11, "created_at": "2026-10-20T01:00:00Z"}]
        )
        self.job_conclusion = job_conclusion
        self.expired = expired

    def api(self, path: str):
        if "/workflows/ci.yml/runs" in path:
            assert "event=workflow_dispatch" in path and "status=success" in path
            return {"workflow_runs": self.runs}
        if path.endswith("/jobs?per_page=100"):
            return {
                "jobs": [
                    {"name": adopt.EVIDENCE_JOB, "conclusion": self.job_conclusion}
                ]
            }
        if "/artifacts?name=" in path:
            return {"artifacts": [{"id": 77, "expired": self.expired}]}
        raise AssertionError(path)

    def fetch_zip(self, artifact_id: int) -> bytes:
        assert artifact_id == 77
        return self.archive


def _decide(repo: Repo, github: FakeGitHub, base_ref: str = "develop"):
    return adopt.decide(
        github.api,
        github.fetch_zip,
        repo.git,
        repo=REPO,
        head_sha=repo.head,
        base_ref=base_ref,
    )


def test_adopts_when_dispatch_and_pr_base_agree(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r", base_moved=False)
    mutations, baseline = _documents(repo)
    # the branch forked from the tip: merge_base == base_tip == HEAD^1
    assert repo.git("rev-parse", "HEAD^1") == repo.tip
    github = FakeGitHub(
        _archive(mutations, baseline),
        runs=[
            {"id": 5, "created_at": "2026-10-20T00:00:00Z"},
            {"id": 9, "created_at": "2026-10-20T02:00:00Z"},
        ],
    )
    assert _decide(repo, github) == (9, "")


def test_the_allowed_states_are_the_ones_the_writers_emit(monkeypatch) -> None:
    """The allowlist is cross-checked against the writer, not typed twice.

    The first version listed ``no_changes``; the writer says
    ``no_changed_functions``, so every run without a changed Python function
    (a CI-only change, the commonest one for this very task) was recounted.
    """
    spec = importlib.util.spec_from_file_location(
        "mutation_changed", ROOT / "scripts" / "mutation_changed.py"
    )
    assert spec is not None and spec.loader is not None
    writer = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "mutation_changed", writer)  # dataclasses
    spec.loader.exec_module(writer)
    informative = {writer.STATE_RAN, writer.STATE_NO_CHANGES, writer.STATE_NO_TESTS}
    assert adopt.MUTATIONS_STATES == informative
    # and the states that carry no information are not allowed
    assert writer.STATE_BASELINE_RED not in adopt.MUTATIONS_STATES
    assert writer.STATE_ERROR not in adopt.MUTATIONS_STATES


def test_the_names_the_script_reads_are_the_names_ci_writes() -> None:
    """Same class as the state token: a name typed twice drifts without a sound.

    The job, the artifact and the two file names live in ci.yml; the baseline
    states live in scripts/red_test_baseline.py. A mismatch would not fail
    anything, it would recount on every run.
    """
    import yaml

    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    evidence = doc["jobs"]["evidence"]
    assert evidence["name"] == adopt.EVIDENCE_JOB
    upload = next(
        s for s in evidence["steps"] if "upload-artifact" in str(s.get("uses"))
    )
    assert upload["with"]["name"] == adopt.ARTIFACT
    assert upload["with"]["path"].split() == [
        adopt.MUTATIONS_FILE,
        adopt.BASELINE_FILE,
    ]
    baseline_source = (ROOT / "scripts" / "red_test_baseline.py").read_text()
    for state in adopt.BASELINE_STATES:
        assert f'state="{state}"' in baseline_source, state
    assert adopt.WORKFLOW == "ci.yml"


def test_a_run_without_changed_python_functions_is_adopted(tmp_path: Path) -> None:
    """A CI-only branch: mutations say no_changed_functions, the baseline no_tests."""
    repo = Repo(tmp_path / "r")
    mutations, baseline = _documents(
        repo,
        mutations={"state": "no_changed_functions"},
        baseline={"state": "no_tests"},
    )
    github = FakeGitHub(_archive(mutations, baseline))
    assert _decide(repo, github) == (11, "")


@pytest.mark.parametrize(
    "case",
    [
        "base_moved",
        "tree_differs_only",
        "not_a_merge_of_head",
        "other_base_ref",
        "doc_other_base",
        "artifact_expired",
        "evidence_job_failed",
        "mutations_error",
        "baseline_error",
        "baseline_red",
        "merge_base_differs",
        "base_tip_differs",
        "missing_merge_base",
        "missing_base_tip",
        "other_head",
        "no_dispatch",
        "broken_archive",
    ],
)
def test_recounts_when_anything_disagrees(tmp_path: Path, case: str) -> None:
    repo = Repo(tmp_path / "r", base_moved=case in {"base_moved", "tree_differs_only"})
    override: dict = {}
    github_kwargs: dict = {}
    head_sha = repo.head
    base_ref = "develop"
    archive: bytes | None = None
    if case == "base_moved":
        # the dispatch run measured against the old tip of develop
        override = {
            "mutations": {"merge_base": repo.fork, "base_tip": repo.fork},
            "baseline": {"merge_base": repo.fork, "base_tip": repo.fork},
        }
    elif case == "not_a_merge_of_head":
        head_sha = repo.fork
    elif case == "other_base_ref":
        base_ref = "main"
    elif case == "doc_other_base":
        override = {"mutations": {"base": "origin/main"}}
    elif case == "artifact_expired":
        github_kwargs = {"expired": True}
    elif case == "evidence_job_failed":
        github_kwargs = {"job_conclusion": "failure"}
    elif case == "mutations_error":
        override = {"mutations": {"state": "error"}}
    elif case == "baseline_error":
        override = {"baseline": {"state": "error"}}
    elif case == "baseline_red":
        override = {"mutations": {"state": "baseline_red"}}
    elif case == "merge_base_differs":
        override = {"mutations": {"merge_base": "1" * 40}}
    elif case == "base_tip_differs":
        override = {"baseline": {"base_tip": "2" * 40}}
    elif case == "missing_merge_base":
        override = {"mutations": {"merge_base": None}}
    elif case == "missing_base_tip":
        override = {"baseline": {"base_tip": None}}
    elif case == "other_head":
        override = {"baseline": {"head": "0" * 40}}
    elif case == "no_dispatch":
        github_kwargs = {"runs": []}
    elif case == "broken_archive":
        archive = b"not a zip"
    mutations, baseline = _documents(repo, **override)
    github = FakeGitHub(
        archive if archive is not None else _archive(mutations, baseline),
        **github_kwargs,
    )
    run_id, reason = adopt.decide(
        github.api,
        github.fetch_zip,
        repo.git,
        repo=REPO,
        head_sha=head_sha,
        base_ref=base_ref,
    )
    assert (run_id, bool(reason)) == (0, True), case


def test_a_failing_api_is_a_recount_not_a_crash(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")

    def boom(_path: str):
        raise OSError("network down")

    def no_zip(_id: int) -> bytes:
        raise AssertionError("never reached")

    run_id, reason = adopt.decide(
        boom, no_zip, repo.git, repo=REPO, head_sha=repo.head, base_ref="develop"
    )
    assert run_id == 0 and "OSError" in reason


def test_the_base_of_the_pr_run_is_head_parent_one_not_the_event_sha(
    tmp_path: Path,
) -> None:
    """The event's base.sha may lag; only HEAD^1 describes what the run measured."""
    repo = Repo(tmp_path / "r", base_moved=False)
    parent1, why = adopt.merge_facts(repo.git, repo.head)
    assert why == "" and parent1 == repo.git("rev-parse", "HEAD^1")


def test_a_moved_base_changes_the_merge_tree_and_forces_a_recount(
    tmp_path: Path,
) -> None:
    """develop moved after the fork: the merge holds code the dispatch never saw."""
    repo = Repo(tmp_path / "r", base_moved=True)
    parent1, why = adopt.merge_facts(repo.git, repo.head)
    assert parent1 == "" and "tree" in why


def test_stamp_writes_the_source_run_into_both_files(tmp_path: Path) -> None:
    for name in (adopt.MUTATIONS_FILE, adopt.BASELINE_FILE):
        (tmp_path / name).write_text(json.dumps({"state": "ran", "mutants": 3}))
    adopt.stamp(tmp_path, 4242)
    for name in (adopt.MUTATIONS_FILE, adopt.BASELINE_FILE):
        document = json.loads((tmp_path / name).read_text())
        assert document["adopted_from_run"] == 4242
        assert "provenance" not in document
    assert json.loads((tmp_path / adopt.MUTATIONS_FILE).read_text())["mutants"] == 3
