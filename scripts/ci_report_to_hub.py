"""Run a task's bound AC tests and validation commands, report to the hub (#546).

Runs inside CI, which is the only place task-supplied commands are allowed to
execute: the production hub deliberately has no test runner (decision of
31.07.2026), so it consumes evidence instead of producing it.

Two rules this script exists to obey:

* It never fails the job. Every problem — no task branch, no token, hub
  unreachable, a validation entry that is prose rather than a command — prints a
  reason and exits 0. The delivery gate (#605/#606) reads the job's outcome to
  decide whether a PR may merge, so a reporting failure here would block merges
  for every task, not just this one.
* It never reports a guess. What it did not run is reported as ``not_found`` for
  AC and left unreported for validation, with the reason attached. A false
  ``fail`` would block a verdict for something unrelated to the work.

Env: HAIPLANE_HUB_URL, HAIPLANE_HUB_CI_TOKEN (legacy OPENCLAW_* names are
accepted as fallback; both absent ⇒ report nothing and say so),
GITHUB_HEAD_REF / GITHUB_REF_NAME, GITHUB_SHA, and
HAIPLANE_HUB_CI_PYTEST (#761: how to run the AC tests, default ``uv run
pytest`` — this repository's own way, and exactly what a satellite repository
with different tooling has to be able to change).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess  # nosec B404 - runs the task's own declared commands, in CI
import sys
import urllib.error
import urllib.request

TASK_BRANCH = re.compile(r"^task-(\d+)/")
# A validation entry we are willing to hand to a shell. Task text mixes real
# commands with prose instructions for humans ("check on a live PR that…"), and
# handing prose to a shell produces a non-zero exit — a false failure about the
# work. Anything that does not look like an executable invocation is reported as
# unknown with its text, never as a failure.
COMMAND_TOKEN = re.compile(r"^[A-Za-z0-9._/-]+$")
_RUN_TIMEOUT = 900
_LOG_TAIL = 4000
# The default is this repository's own runner. A satellite repository sets
# HAIPLANE_HUB_CI_PYTEST to whatever runs ITS tests; an unparsable or missing
# runner reports not_found with a reason, never a failure about the work.
_DEFAULT_AC_RUNNER = "uv run pytest"


def env_get(suffix: str) -> str:
    """HAIPLANE_ first, legacy OPENCLAW_ fallback; empty counts as unset.

    Standalone twin of ``hub.config.env_get`` — this script also runs in
    satellite repositories where the hub package is not importable.
    """
    return (
        os.environ.get(f"HAIPLANE_{suffix}")
        or os.environ.get(f"OPENCLAW_{suffix}")
        or ""
    )


def log(msg: str) -> None:
    print(f"[hub-report] {msg}", flush=True)


def task_id_from_branch() -> int | None:
    branch = (
        os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME") or ""
    )
    m = TASK_BRANCH.match(branch.strip())
    if not m:
        log(f"branch {branch!r} is not task-N/... — nothing to report (unknown)")
        return None
    return int(m.group(1))


def reported_commit() -> str:
    """The commit this run is evidence ABOUT — the branch head, not the merge.

    On ``pull_request`` events GitHub builds a throwaway merge of the head into
    the base and sets GITHUB_SHA to THAT commit, which exists nowhere in the
    branch. The hub pins the branch tip at submission (#572), so a report keyed
    on GITHUB_SHA could never match what is under review — it would be filed
    against a commit nobody can find and silently never applied. The workflow
    therefore passes the head explicitly in HEAD_SHA
    (``github.event.pull_request.head.sha``), and GITHUB_SHA stays as the
    fallback for ``push`` runs, where it IS the branch tip.
    """
    head = (os.environ.get("HEAD_SHA") or "").strip()
    if head:
        log(f"reporting commit {head[:12]} (HEAD_SHA, the branch head)")
        return head
    fallback = (os.environ.get("GITHUB_SHA") or "").strip()
    if fallback:
        log(f"reporting commit {fallback[:12]} (GITHUB_SHA fallback)")
    return fallback


def hub_request(url: str, token: str, payload: dict | None = None) -> dict | None:
    data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - https URL from env
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:400]
        log(f"hub said {exc.code}: {body}")
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        log(f"hub unreachable: {exc}")
    return None


def is_command(entry: str) -> bool:
    """True when ``entry`` looks like something a shell can execute."""
    entry = entry.strip()
    if not entry:
        return False
    first = entry.split()[0]
    if not COMMAND_TOKEN.match(first):
        return False
    return shutil.which(first) is not None


def ac_runner() -> list[str]:
    """The argv prefix that runs the AC tests, from env or this repo's default.

    Returns [] when the runner is unusable — unparsable, empty, or absent from
    the PATH. The caller turns that into ``not_found`` with a reason: a report
    that guessed "failed" because the runner was missing would accuse the work
    of something the tooling did.
    """
    raw = (env_get("HUB_CI_PYTEST") or _DEFAULT_AC_RUNNER).strip()
    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        log(f"AC runner {raw!r} does not parse ({exc}) — every AC stays not_found")
        return []
    if not argv:
        log("AC runner is empty — every AC stays not_found")
        return []
    if shutil.which(argv[0]) is None:
        log(f"AC runner {argv[0]!r} is not on PATH — every AC stays not_found")
        return []
    return argv


def run_nodeids(nodeids: list[str]) -> dict[str, bool]:
    """Run the AC tests for ``nodeids`` and return {nodeid: passed} for what ran."""
    if not nodeids:
        return {}
    runner = ac_runner()
    if not runner:
        return {}
    cmd = [
        *runner,
        *nodeids,
        "-v",
        "--no-header",
        "-p",
        "no:cacheprovider",
    ]
    try:
        proc = subprocess.run(  # nosec B603 - fixed argv, nodeids come from the hub
            cmd, capture_output=True, text=True, timeout=_RUN_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"pytest could not run ({exc}) — every AC stays not_found")
        return {}
    out: dict[str, bool] = {}
    wanted = set(nodeids)
    for raw in proc.stdout.splitlines():
        parts = raw.strip().split(None, 1)
        if len(parts) != 2:
            continue
        reported, rest = parts
        key = reported if reported in wanted else reported.split("[", 1)[0]
        if key not in wanted:
            continue
        if "PASSED" in rest:
            passed = True
        elif "FAILED" in rest or "ERROR" in rest:
            passed = False
        else:
            continue
        # Any failing parametrized case fails the AC.
        out[key] = out.get(key, True) and passed
    return out


# GitHub spells a step's result its own way; the hub's contract has three
# outcomes (#875). Anything unrecognised is dropped rather than guessed: a
# check whose result we cannot name must not become one the reviewer trusts.
_OUTCOME_MAP = {
    "success": "pass",
    "pass": "pass",
    "passed": "pass",
    "failure": "fail",
    "fail": "fail",
    "failed": "fail",
    "skipped": "skipped",
    "cancelled": "skipped",
    "canceled": "skipped",
}


def parse_checks(raw: str) -> dict[str, str]:
    """``"lint=success, types=failure"`` → ``{"lint": "pass", "types": "fail"}``.

    Silence in, silence out: an empty or unparsable entry contributes nothing.
    An empty map means "this report names no checks", which the hub reads as
    "nothing is proven" — never as "everything passed".
    """
    checks: dict[str, str] = {}
    for chunk in (raw or "").replace("\n", ",").split(","):
        name, _, outcome = chunk.partition("=")
        name = name.strip()
        mapped = _OUTCOME_MAP.get(outcome.strip().lower())
        if name and mapped:
            checks[name] = mapped
        elif name:
            log(f"check {name!r}: outcome {outcome.strip()!r} not recognised — dropped")
    return checks


def run_validation(commands: list[str]) -> tuple[str, str, str]:
    """(status, log_tail, reason) for the task's validation_commands."""
    if not commands:
        return "skipped", "", "у задачи нет validation_commands"
    prose = [c for c in commands if not is_command(c)]
    if prose:
        return (
            "unknown",
            "",
            "не запускались: среди validation_commands есть записи, "
            f"которые не являются командами ({len(prose)} из {len(commands)}), "
            f"например {prose[0][:120]!r}",
        )
    logs: list[str] = []
    for cmd in commands:
        try:
            proc = subprocess.run(  # nosec B602 - the task's own declared commands, run in a disposable CI runner
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=_RUN_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return "unknown", "\n".join(logs)[-_LOG_TAIL:], f"команда прервана: {exc}"
        logs.append(f"$ {cmd}\n{proc.stdout}{proc.stderr}")
        if proc.returncode != 0:
            return "fail", "\n".join(logs)[-_LOG_TAIL:], f"команда упала: {cmd}"
    return "pass", "\n".join(logs)[-_LOG_TAIL:], ""


def main() -> int:
    base = env_get("HUB_URL").rstrip("/")
    token = env_get("HUB_CI_TOKEN")
    if not base or not token:
        log(
            "HAIPLANE_HUB_URL / HAIPLANE_HUB_CI_TOKEN (or legacy OPENCLAW_*) "
            "not configured — reporting nothing; the hub will read this as "
            "unknown, not as failure"
        )
        return 0

    task_id = task_id_from_branch()
    if task_id is None:
        return 0
    head_sha = reported_commit()
    if not head_sha:
        log("no commit to report — a report must name its commit; reporting nothing")
        return 0

    task = hub_request(f"{base}/api/tasks/{task_id}", token)
    if task is None:
        log(
            f"could not read task #{task_id} from the hub — reporting nothing; "
            "the hub will read this as unknown, not as failure"
        )
        return 0

    nodeid_by_ac: dict[str, str] = {}
    for ac in task.get("acceptance_criteria") or []:
        if (ac.get("verifiable_by") or "") != "test":
            continue
        ref = (ac.get("test_ref") or "").strip()
        if "::" in ref:
            nodeid_by_ac[ac["id"]] = ref
    ran = run_nodeids(sorted(set(nodeid_by_ac.values())))
    ac_results = {
        ac_id: ("pass" if ran[nodeid] else "fail") if nodeid in ran else "not_found"
        for ac_id, nodeid in nodeid_by_ac.items()
    }

    v_status, v_log, v_reason = run_validation(task.get("validation_commands") or [])

    checks = parse_checks(env_get("HUB_CI_CHECKS"))
    if checks:
        log(f"deterministic checks reported: {checks}")

    payload = {
        "head_sha": head_sha,
        "ac_results": ac_results,
        "validation_status": v_status,
        "validation_log": v_log,
        "reason": v_reason,
        "reported_by": "github-actions",
        "checks": checks,
    }
    result = hub_request(f"{base}/api/tasks/{task_id}/ci-run-report", token, payload)
    if result is None:
        log("report not delivered — the hub will read this as unknown")
        return 0
    log(
        f"reported {len(ac_results)} AC result(s), validation={v_status}; "
        f"applied={result.get('applied')} ({result.get('reason')})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
