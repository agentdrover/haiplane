"""Send the CI results of a finished run to the hub, from the default branch (#1686).

The job of a task branch runs the branch's code, so it must never hold the key
the hub believes a CI report with. It collects the results into an artifact
(scripts/ci_report_to_hub.py, key: read-only). This script runs in
.github/workflows/ci-report.yml on ``workflow_run``: that workflow is always the
one on the default branch, ``github.sha`` is the default branch head, and the
checkout it runs from contains no code of the branch under test.

Everything the artifact says is DATA. The facts that name the run — commit,
run id, attempt, workflow path, event, head repository, conclusion — come from
the event payload, never from the artifact; a disagreement is a refusal. The
archive is read in memory by name, with size limits, and never extracted; its
strings go into a JSON body and nowhere else (no shell, no import, no path).

Like the other reporters it never fails the job: a refusal prints its reason and
exits 0, and the hub reads silence as "unknown", never as success.

Env: GITHUB_EVENT_PATH, GITHUB_REPOSITORY, GITHUB_API_URL, RUNNER_TEMP,
GITHUB_WORKSPACE, GH_TOKEN (the workflow's own token, to read the run's
artifacts), HAIPLANE_HUB_URL, HAIPLANE_HUB_CI_TOKEN (the report key),
HAIPLANE_HUB_CI_AUDIT_TOKEN (the key that files audit drafts).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

EXPECTED_WORKFLOW_PATH = ".github/workflows/ci.yml"
ALLOWED_EVENTS = frozenset({"pull_request", "push", "workflow_dispatch"})
AUDIT_BRANCHES = frozenset({"develop", "main"})
ARTIFACT_PREFIX = "hub-ci-results-"
RESULTS_MEMBER = "results.json"
AUDIT_MEMBER = "audit.json"
ALLOWED_MEMBERS = frozenset({RESULTS_MEMBER, AUDIT_MEMBER})
MAX_ARCHIVE_BYTES = 5 * 1024 * 1024
MAX_MEMBERS = 10
MAX_UNPACKED_BYTES = 20 * 1024 * 1024
MAX_RESULTS_BYTES = 2 * 1024 * 1024
RESULTS_SCHEMA = 1
TASK_BRANCH = re.compile(r"^task-(\d+)/")
# #1700: the steps whose minutes the hub names, by the step name in ci.yml. A
# renamed step shows up as a missing key in the hub, never as a wrong one.
TIMING_STEPS = {
    "Test": "test",
    "Mutations of changed functions (warning only)": "mutations",
    "Red-test baseline on merge-base (data only)": "baseline",
    "Collect AC test and validation results for the Hub": "ac_collect",
}
MAX_JOBS_PAGES = 5
MAX_JOBS_BYTES = 2 * 1024 * 1024
MAX_TIMED_JOBS = 50

AC_STATUSES = frozenset({"pass", "fail", "not_found"})
CHECK_STATUSES = frozenset({"pass", "fail", "skipped"})
VALIDATION_STATUSES = frozenset({"pass", "fail", "unknown", "skipped", ""})
RESULT_KEYS = frozenset(
    {
        "schema",
        "task_id",
        "head_sha",
        "ac_results",
        "validation_status",
        "validation_log",
        "reason",
        "reported_by",
        "checks",
        "mutations",
        "baseline",
    }
)
# The limits of hub/models.py CIRunReportSubmit; a longer string is cut, not
# refused, because the hub would answer 422 for the whole report.
_LOG_MAX = 4000
_REASON_MAX = 500
_BLOCK_MAX_CHARS = 32_000

HubPost = Callable[[str, str, dict], dict | None]


class Refused(Exception):
    """The run or its artifact is not something we report. ``str(exc)`` is why."""


def log(msg: str) -> None:
    print(f"[hub-ci-report] {msg}", flush=True)


def env_get(suffix: str) -> str:
    """HAIPLANE_-prefixed env value; empty counts as unset."""
    return os.environ.get(f"HAIPLANE_{suffix}") or ""


# ---- the event --------------------------------------------------------------


def run_facts(event: dict, repository: str) -> dict[str, str]:
    """The facts about the finished run, from the event payload, or Refused."""
    run = event.get("workflow_run")
    if not isinstance(run, dict):
        raise Refused("the event carries no workflow_run")
    # workflow_run.path may carry an "@<ref>" suffix; the file is what counts.
    path = str(run.get("path") or "")
    if path.split("@", 1)[0] != EXPECTED_WORKFLOW_PATH:
        raise Refused(f"workflow path {path!r} is not {EXPECTED_WORKFLOW_PATH}")
    head_repo = (run.get("head_repository") or {}).get("full_name")
    if not repository or head_repo != repository:
        raise Refused(f"head repository {head_repo!r} is not {repository!r}")
    kind = str(run.get("event") or "")
    if kind not in ALLOWED_EVENTS:
        raise Refused(f"event {kind!r} is not one of {sorted(ALLOWED_EVENTS)}")
    facts = {
        "run_id": str(run.get("id") or ""),
        "run_attempt": str(run.get("run_attempt") or ""),
        "workflow_path": path,
        "event": kind,
        "head_repository": str(head_repo),
        "run_conclusion": str(run.get("conclusion") or ""),
        "head_sha": str(run.get("head_sha") or ""),
        "head_branch": str(run.get("head_branch") or ""),
        "updated_at": str(run.get("updated_at") or ""),
    }
    for name in ("run_id", "run_attempt", "head_sha"):
        if not facts[name]:
            raise Refused(f"the event carries no {name}")
    return facts


# ---- the artifact -----------------------------------------------------------


def select_artifact(listing: dict, facts: dict[str, str]) -> dict:
    """The one artifact of THIS attempt, or Refused. No other attempt stands in."""
    wanted = f"{ARTIFACT_PREFIX}{facts['run_attempt']}"
    found = [
        a
        for a in (listing.get("artifacts") or [])
        if isinstance(a, dict) and a.get("name") == wanted
    ]
    if not found:
        raise Refused(f"no artifact {wanted!r} in run {facts['run_id']}")
    if len(found) > 1:
        raise Refused(f"{len(found)} artifacts named {wanted!r}: refusing to choose")
    artifact = found[0]
    if artifact.get("expired"):
        raise Refused(f"artifact {wanted!r} has expired")
    size = artifact.get("size_in_bytes")
    if not isinstance(size, int) or size > MAX_ARCHIVE_BYTES:
        raise Refused(f"artifact {wanted!r} is {size!r} bytes, the limit is 5 MB")
    owner = artifact.get("workflow_run")
    if isinstance(owner, dict):
        if owner.get("id") is not None and str(owner["id"]) != facts["run_id"]:
            raise Refused("the artifact belongs to another run")
        if owner.get("head_sha") and owner["head_sha"] != facts["head_sha"]:
            raise Refused("the artifact belongs to another commit")
    return artifact


def download_dir(runner_temp: str, workspace: str) -> Path:
    """An empty directory under RUNNER_TEMP and outside the checkout, or Refused."""
    if not runner_temp:
        raise Refused("RUNNER_TEMP is not set")
    target = Path(runner_temp).resolve() / "hub-ci-results"
    checkout = Path(workspace or os.getcwd()).resolve()
    if target == checkout or checkout in target.parents or target in checkout.parents:
        raise Refused("the download directory overlaps the checkout")
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise Refused(f"{target} is not empty")
    return target


def _member_problem(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if info.flag_bits & 0x1:
        return f"{name!r} is encrypted"
    if name.startswith(("/", "\\")) or "\\" in name or ".." in name.split("/"):
        return f"{name!r} is not a plain relative name"
    if info.is_dir() or "/" in name:
        return f"{name!r} is not a top-level file"
    if stat.S_ISLNK(info.external_attr >> 16):
        return f"{name!r} is a symbolic link"
    if name not in ALLOWED_MEMBERS:
        return f"{name!r} is not an expected file"
    return ""


def read_archive(path: Path) -> dict[str, bytes]:
    """The expected members of the archive, read in memory; Refused otherwise.

    Nothing is extracted: no member name ever becomes a path on this machine.
    """
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise Refused(f"the artifact is not a readable archive ({exc})") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise Refused(f"{len(infos)} files, the limit is {MAX_MEMBERS}")
        names = [i.filename for i in infos]
        if len(set(names)) != len(names):
            raise Refused("the archive repeats a file name")
        members: dict[str, bytes] = {}
        budget = MAX_UNPACKED_BYTES
        for info in infos:
            problem = _member_problem(info)
            if problem:
                raise Refused(problem)
            if info.file_size > budget:
                raise Refused("the unpacked size exceeds 20 MB")
            with archive.open(info) as handle:
                data = handle.read(budget + 1)
            if len(data) > budget:
                raise Refused("the unpacked size exceeds 20 MB")
            budget -= len(data)
            members[info.filename] = data
        return members


# ---- the results ------------------------------------------------------------


def _string_map(value: Any, label: str, allowed: frozenset[str]) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 500:
        raise Refused(f"{label} is not a small object")
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > 200:
            raise Refused(f"{label} has a bad key")
        if item not in allowed:
            raise Refused(
                f"{label}[{key!r}] = {item!r} is not one of {sorted(allowed)}"
            )
    return dict(value)


def _bounded_text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise Refused(f"{label} is not text")
    return value if len(value) <= limit else value[-limit:]


def _evidence_block(value: Any, label: str, provenance: dict) -> dict | None:
    """A mutations / baseline block as data, stamped with THIS run's provenance."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise Refused(f"{label} is not an object")
    block = dict(value, provenance=provenance)
    if len(json.dumps(block, ensure_ascii=False)) > _BLOCK_MAX_CHARS:
        return {
            "state": "error",
            "reason": f"the {label} block exceeds {_BLOCK_MAX_CHARS} characters",
            "provenance": provenance,
        }
    return block


def block_provenance(facts: dict[str, str], server: str, repository: str) -> dict:
    """Which run produced an evidence block: from the event, not from the branch."""
    run_id = facts["run_id"]
    return {
        "run_id": run_id,
        "run_url": f"{server}/{repository}/actions/runs/{run_id}",
        "event": facts["event"],
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def parse_results(
    raw: bytes, facts: dict[str, str], task_id: int, provenance: dict
) -> dict:
    """The results file as a validated report body, or Refused."""
    if len(raw) > MAX_RESULTS_BYTES:
        raise Refused("results.json is larger than 2 MB")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refused(f"results.json is not JSON ({exc})") from exc
    if not isinstance(doc, dict):
        raise Refused("results.json is not an object")
    extra = sorted(set(doc) - RESULT_KEYS)
    if extra:
        raise Refused(f"results.json has unexpected keys: {extra}")
    if doc.get("schema") != RESULTS_SCHEMA:
        raise Refused(f"results.json schema {doc.get('schema')!r} is not 1")
    if doc.get("head_sha") != facts["head_sha"]:
        raise Refused("the artifact is about another commit than the run")
    if doc.get("task_id") != task_id:
        raise Refused("the artifact is about another task than the branch")
    status = doc.get("validation_status", "")
    if status not in VALIDATION_STATUSES:
        raise Refused(f"validation_status {status!r} is not recognised")
    body: dict[str, Any] = {
        "head_sha": facts["head_sha"],
        "ac_results": _string_map(doc.get("ac_results", {}), "ac_results", AC_STATUSES),
        "checks": _string_map(doc.get("checks", {}), "checks", CHECK_STATUSES),
        "validation_status": status,
        "validation_log": _bounded_text(
            doc.get("validation_log", ""), "validation_log", _LOG_MAX
        ),
        "reason": _bounded_text(doc.get("reason", ""), "reason", _REASON_MAX),
        "reported_by": "github-actions",
    }
    for key in ("mutations", "baseline"):
        block = _evidence_block(doc.get(key), key, provenance)
        if block is not None:
            body[key] = block
    return body


def provenance_fields(facts: dict[str, str], artifact: dict, blob: bytes) -> dict:
    """The flat provenance fields of the report: the event, plus the artifact."""
    digest = str(artifact.get("digest") or "")
    computed = "sha256:" + hashlib.sha256(blob).hexdigest()
    if digest.startswith("sha256:") and digest != computed:
        raise Refused("the downloaded archive does not match the artifact digest")
    return {
        **{
            k: facts[k]
            for k in (
                "run_id",
                "run_attempt",
                "workflow_path",
                "event",
                "head_repository",
                "run_conclusion",
            )
        },
        "artifact_id": str(artifact.get("id") or ""),
        "artifact_digest": digest or computed,
    }


# ---- transport --------------------------------------------------------------


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """The archive URL redirects to storage that must not see the GitHub token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.remove_header("Authorization")
        return new


class GitHubApi:
    """Read-only access to one run's artifacts with the workflow's own token."""

    def __init__(self, api: str, token: str) -> None:
        self.api = api.rstrip("/")
        self.token = token

    def _request(self, url: str) -> urllib.request.Request:
        return urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def list_artifacts(self, repository: str, run_id: str, name: str) -> dict:
        url = (
            f"{self.api}/repos/{repository}/actions/runs/{run_id}/artifacts"
            f"?per_page=100&name={name}"
        )
        opener = urllib.request.build_opener()
        with opener.open(self._request(url), timeout=30) as resp:  # nosec B310 - GitHub API
            return json.load(resp)

    def list_jobs(self, repository: str, run_id: str, attempt: str) -> list[dict]:
        """Every job of one attempt, or an exception when the list is not whole."""
        jobs: list[dict] = []
        opener = urllib.request.build_opener()
        for page in range(1, MAX_JOBS_PAGES + 1):
            url = (
                f"{self.api}/repos/{repository}/actions/runs/{run_id}"
                f"/attempts/{attempt}/jobs?per_page=100&page={page}"
            )
            with opener.open(self._request(url), timeout=30) as resp:  # nosec B310 - GitHub API
                raw = resp.read(MAX_JOBS_BYTES + 1)
            if len(raw) > MAX_JOBS_BYTES:
                raise Refused("the jobs answer is larger than 2 MB")
            doc = json.loads(raw.decode("utf-8"))
            jobs += [j for j in doc.get("jobs") or [] if isinstance(j, dict)]
            if len(jobs) >= int(doc.get("total_count") or 0):
                return jobs
        raise Refused(f"more than {MAX_JOBS_PAGES * 100} jobs: the list is not whole")

    def download(self, url: str) -> bytes:
        opener = urllib.request.build_opener(_DropAuthOnRedirect)
        with opener.open(self._request(url), timeout=60) as resp:  # nosec B310 - GitHub API
            data = resp.read(MAX_ARCHIVE_BYTES + 1)
        if len(data) > MAX_ARCHIVE_BYTES:
            raise Refused("the archive is larger than 5 MB")
        return data


def hub_post(url: str, token: str, payload: dict) -> dict | None:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - https URL from env
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        log(f"hub said {exc.code}: {exc.read().decode(errors='replace')[:300]}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log(f"hub unreachable: {exc}")
    return None


# ---- the timing (#1700) -----------------------------------------------------


def _clip(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _seconds(started: Any, completed: Any) -> float | None:
    """Whole-job or whole-step seconds from GitHub's two times, or None."""
    from datetime import datetime

    try:
        begin = datetime.fromisoformat(str(started))
        end = datetime.fromisoformat(str(completed))
    except ValueError:
        return None
    return max(0.0, (end - begin).total_seconds())


def _timed_step(step: dict) -> dict | None:
    key = (
        TIMING_STEPS.get(step.get("name"))
        if isinstance(step.get("name"), str)
        else None
    )
    if key is None:
        return None
    conclusion = _clip(step.get("conclusion"), 40)
    seconds = None
    if conclusion != "skipped":
        seconds = _seconds(step.get("started_at"), step.get("completed_at"))
    return {
        "key": key,
        "name": _clip(step.get("name"), 200),
        "conclusion": conclusion,
        "seconds": seconds,
    }


def _timed_job(job: dict) -> dict:
    steps = [_timed_step(s) for s in job.get("steps") or [] if isinstance(s, dict)]
    return {
        "name": _clip(job.get("name"), 200),
        "conclusion": _clip(job.get("conclusion"), 40),
        "started_at": _clip(job.get("started_at"), 40) or None,
        "completed_at": _clip(job.get("completed_at"), 40) or None,
        "steps": [s for s in steps if s is not None][:20],
    }


def timing_body(
    facts: dict[str, str], jobs: list[dict] | None, reason: str = ""
) -> dict:
    """The timing of the finished attempt, from the event plus GitHub's jobs."""
    body: dict[str, Any] = {
        "run_id": facts["run_id"],
        "run_attempt": facts["run_attempt"],
        "head_sha": facts["head_sha"],
        "event": facts["event"],
        "conclusion": facts["run_conclusion"],
        "completed_at": facts["updated_at"],
        "state": "ok" if jobs is not None else "timing_unavailable",
        "reason": reason[:500],
        "jobs": [_timed_job(j) for j in (jobs or [])[:MAX_TIMED_JOBS]],
    }
    return body


def send_timing(
    facts: dict[str, str],
    api: GitHubApi | Any,
    repository: str,
    base: str,
    token: str,
    post: HubPost,
) -> None:
    """Report what the attempt cost; independent of the artifact and its verdict."""
    match = TASK_BRANCH.match(facts["head_branch"])
    if match is None:
        log(f"branch {facts['head_branch']!r} is not task-N/... — no timing")
        return
    if not facts["updated_at"]:
        log("the event carries no completion time — no timing")
        return
    jobs: list[dict] | None = None
    reason = ""
    try:
        jobs = api.list_jobs(repository, facts["run_id"], facts["run_attempt"])
    except (Refused, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        reason = f"jobs unreadable: {exc}"
        log(f"timing_unavailable — {reason}")
    task_id = int(match.group(1))
    result = post(
        f"{base}/api/tasks/{task_id}/ci-run-timing",
        token,
        timing_body(facts, jobs, reason),
    )
    shown = "not delivered" if result is None else f"stored={result.get('stored')}"
    log(
        f"timing of task #{task_id} run {facts['run_id']}/{facts['run_attempt']}: {shown}"
    )


# ---- the two reports --------------------------------------------------------


def send_report(
    members: dict[str, bytes],
    facts: dict[str, str],
    prov: dict,
    base: str,
    token: str,
    post: HubPost,
    server: str,
    repository: str,
) -> None:
    raw = members.get(RESULTS_MEMBER)
    if raw is None:
        log("no results.json in the artifact — no task report")
        return
    match = TASK_BRANCH.match(facts["head_branch"])
    if match is None:
        log(f"branch {facts['head_branch']!r} is not task-N/... — no task report")
        return
    task_id = int(match.group(1))
    body = parse_results(
        raw, facts, task_id, block_provenance(facts, server, repository)
    )
    body.update(prov)
    result = post(f"{base}/api/tasks/{task_id}/ci-run-report", token, body)
    if result is None:
        log("report not delivered — the hub will read this as unknown")
        return
    log(
        f"reported task #{task_id} commit {facts['head_sha'][:12]}: "
        f"applied={result.get('applied')} ({result.get('reason')})"
    )


def _audit_module() -> Any:
    """The audit script next to this one, loaded by path (it is base-branch code)."""
    import importlib.util

    path = Path(__file__).resolve().parent / "ci_report_audit_to_hub.py"
    spec = importlib.util.spec_from_file_location("ci_report_audit_to_hub", path)
    if spec is None or spec.loader is None:
        raise Refused("the audit script cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def file_audit(
    members: dict[str, bytes],
    facts: dict[str, str],
    base: str,
    token: str,
    post: HubPost,
) -> None:
    """Draft tasks for audit findings; only for code already merged (push)."""
    if facts["event"] != "push" or facts["head_branch"] not in AUDIT_BRANCHES:
        log("audit findings are filed only for push to develop or main — skipped")
        return
    raw = members.get(AUDIT_MEMBER)
    if raw is None:
        log("no audit.json in the artifact — no drafts")
        return
    audit = _audit_module()
    try:
        report = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refused(f"audit.json is not JSON ({exc})") from exc
    if not isinstance(report, dict):
        raise Refused("audit.json is not an object")
    for finding in audit.findings(report):
        result = post(f"{base}/api/tasks", token, audit.draft_payload(finding))
        shown = "not filed" if result is None else f"hub task #{result.get('id')}"
        log(f"{finding['id']} in {finding['package']}: {shown}")


def _independently(step: Callable[[], None]) -> None:
    """One report's refusal must not take the other one down."""
    try:
        step()
    except Refused as exc:
        log(f"not reporting this part: {exc}")


def _report_artifact(
    facts: dict[str, str],
    environ: dict[str, str],
    api: GitHubApi | Any,
    post: HubPost,
    base: str,
    token: str,
    repository: str,
    server: str,
) -> None:
    """The CI report and the audit drafts: everything that needs the artifact."""
    try:
        target = download_dir(
            environ.get("RUNNER_TEMP", ""), environ.get("GITHUB_WORKSPACE", "")
        )
        name = f"{ARTIFACT_PREFIX}{facts['run_attempt']}"
        artifact = select_artifact(
            api.list_artifacts(repository, facts["run_id"], name), facts
        )
        blob = api.download(str(artifact.get("archive_download_url") or ""))
        prov = provenance_fields(facts, artifact, blob)
        (target / "artifact.zip").write_bytes(blob)
        members = read_archive(target / "artifact.zip")
        _independently(
            lambda: send_report(
                members, facts, prov, base, token, post, server, repository
            )
        )
        audit_token = environ.get("HAIPLANE_HUB_CI_AUDIT_TOKEN") or ""
        if audit_token:
            _independently(lambda: file_audit(members, facts, base, audit_token, post))
    except Refused as exc:
        log(f"not reporting this run: {exc}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log(f"GitHub or the disk failed ({exc}) — reporting nothing (unknown)")


def run(
    event: dict,
    environ: dict[str, str],
    api: GitHubApi | Any,
    post: HubPost = hub_post,
) -> int:
    """Report one finished CI run. Always 0: a refusal is logged, never red."""
    repository = environ.get("GITHUB_REPOSITORY", "")
    server = environ.get("GITHUB_SERVER_URL") or "https://github.com"
    try:
        facts = run_facts(event, repository)
    except Refused as exc:
        log(f"not reporting this run: {exc}")
        return 0
    base = (environ.get("HAIPLANE_HUB_URL") or "").rstrip("/")
    token = environ.get("HAIPLANE_HUB_CI_TOKEN") or ""
    if not base or not token:
        log("hub URL or report key not configured — reporting nothing (unknown)")
        return 0
    _independently(
        lambda: _report_artifact(
            facts, environ, api, post, base, token, repository, server
        )
    )
    # #1700: after the report, and outside everything the artifact can refuse.
    try:
        send_timing(facts, api, repository, base, token, post)
    except Exception as exc:  # noqa: BLE001 - statistics must never fail the job
        log(f"timing not sent: {exc}")
    return 0


def main() -> int:
    path = os.environ.get("GITHUB_EVENT_PATH") or ""
    try:
        event = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log(f"event payload unreadable ({exc}) — reporting nothing")
        return 0
    api = GitHubApi(
        os.environ.get("GITHUB_API_URL") or "https://api.github.com",
        os.environ.get("GH_TOKEN") or "",
    )
    return run(event, dict(os.environ), api)


if __name__ == "__main__":
    sys.exit(main())
