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

Env: HAIPLANE_HUB_URL, HAIPLANE_HUB_CI_TOKEN (both absent ⇒ report
nothing and say so),
GITHUB_HEAD_REF / GITHUB_REF_NAME, GITHUB_SHA, and
HAIPLANE_HUB_CI_PYTEST (#761: how to run the AC tests, default ``uv run
pytest`` — this repository's own way, and exactly what a satellite repository
with different tooling has to be able to change),
HAIPLANE_HUB_CI_RAN (#1081: the commands this job already executed, one
``<outcome> <command>`` per line — what they prove is not executed a second
time; absent ⇒ nothing is reused and every command runs as before).
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
    """HAIPLANE_-prefixed env value; empty counts as unset.

    Standalone twin of ``hub.config.env_get`` — this script also runs in
    satellite repositories where the hub package is not importable.
    """
    return os.environ.get(f"HAIPLANE_{suffix}") or ""


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


# Flags that provably do not change WHICH tests run — the only differences
# allowed before two pytest invocations count as the same assertion (#1081).
# Closed on purpose: an unrecognised flag makes the commands non-equivalent and
# the validation command is executed. Equivalence is proven, never assumed.
#
# Every entry was checked against real pytest by collection count, not by
# reading the docs: `--collect-only -q` with the flag and without must report
# the same number. `-p` failed that check and is deliberately ABSENT — it loads
# and disables plugins, and pytest's own `python` plugin IS the collector for
# Python tests: `-p no:python` collects 0 of 2794 and exits 5. A command that
# runs nothing would otherwise have been declared proven by the job's green
# suite, which is exactly the fiction this whole mechanism must not produce.
_NON_SELECTING_FLAGS = {
    "-q",
    "--quiet",
    "-v",
    "--verbose",
    "-s",
    "--no-header",
    "--no-summary",
    "--color",
    "--tb",
    "-r",
    "-n",
    "--numprocesses",
    "--dist",
}


# Whitelisted flags whose value may live in the next token rather than after
# an "=". Kept separate from the whitelist itself: membership here decides
# whether a token is EATEN, and eating one too many is how a narrowed run gets
# mistaken for the whole suite.
_VALUE_TAKING_FLAGS = {"-n", "--numprocesses", "--dist", "--tb", "--color", "-r"}


def _looks_like_a_test_path(token: str) -> bool:
    """Could this token be a test path or nodeid rather than a flag's value?

    Deliberately generous: a false "yes" only costs one extra run, while a
    false "no" swallows a path and turns a narrowed selection into "the whole
    suite, already proven".
    """
    return "/" in token or "::" in token or token.endswith(".py")


def parse_ran_commands(raw: str) -> dict[str, str]:
    """``"success uv run pytest -q"`` lines → ``{command: "pass"}`` (#1081).

    The job already ran these; the reporter is told WHAT was run, not only how
    it ended, because "lint=success" cannot be matched against a task's
    ``uv run ruff check hub tests``. Silence in, silence out: an unparsable or
    outcome-less line contributes nothing, and an empty map simply means
    nothing can be reused.
    """
    ran: dict[str, str] = {}
    for line in (raw or "").splitlines():
        head, _, command = line.strip().partition(" ")
        command = command.strip()
        mapped = _OUTCOME_MAP.get(head.strip().lower())
        if command and mapped:
            ran[command] = mapped
    return ran


def _selection_key(command: str) -> tuple | None:
    """What this pytest invocation SELECTS, or None when that cannot be told.

    Two commands with the same key run the same tests, so a green outcome for
    one is a green outcome for the other. Everything that narrows or reorders
    the run — positional arguments, ``-k``, ``-m``, ``--ignore``,
    ``--deselect``, ``--maxfail``, ``--lf``/``--ff`` — is part of the key.
    Anything not recognised returns None, which means "not proven equivalent".
    """
    try:
        argv = shlex.split(command, comments=True)
    except ValueError:
        return None
    if "pytest" not in argv:
        return None
    rest = argv[argv.index("pytest") + 1 :]
    prefix = tuple(argv[: argv.index("pytest") + 1])
    selecting: list[str] = []
    i = 0
    while i < len(rest):
        token = rest[i]
        if not token.startswith("-"):
            selecting.append(token)  # a path or nodeid narrows the run
            i += 1
            continue
        name = token.split("=", 1)[0]
        if name not in _NON_SELECTING_FLAGS:
            return None  # unknown flag: cannot prove it does not select
        # A whitelisted flag may carry its value in the next token. Consume it
        # only when it cannot be a test path: `pytest -n tests/test_web.py` is
        # a card with a dangling -n, and swallowing the path would key it as
        # the WHOLE suite and declare it proven — while real pytest exits 4 on
        # it. Refusing to consume leaves the path positional, the selection
        # differs, and the command runs. Wrong in the cheap direction only.
        if (
            "=" not in token
            and name in _VALUE_TAKING_FLAGS
            and i + 1 < len(rest)
            and not rest[i + 1].startswith("-")
            and not _looks_like_a_test_path(rest[i + 1])
        ):
            i += 1
        i += 1
    return prefix, tuple(sorted(selecting))


def already_proven(command: str, ran: dict[str, str]) -> tuple[str, str] | None:
    """(outcome, the command that proved it) when the job already ran this.

    Exact string equality first — that covers ruff, mypy and any non-pytest
    command verbatim. Only then selection-equivalence, and only for pytest:
    the job runs ``uv run pytest -q -n auto`` while task cards declare
    ``uv run pytest -q``, which is the same assertion about the same tree
    differing solely in worker count (#1081).
    """
    command = command.strip()
    if command in ran:
        return ran[command], command
    key = _selection_key(command)
    if key is None:
        return None
    for candidate, outcome in ran.items():
        if _selection_key(candidate) == key:
            return outcome, candidate
    return None


def run_validation(
    commands: list[str], ran: dict[str, str] | None = None
) -> tuple[str, str, str]:
    """(status, log_tail, reason) for the task's validation_commands.

    ``ran`` names the commands this job already executed, with their outcomes.
    Anything it proves is not executed a second time: the reporter runs in the
    same job that just ran the suite, and re-running it cost as much as the
    tests themselves — measured 236-333s per run before this (#1081). What it
    does not prove is executed exactly as before; a saving that skipped
    evidence would be worse than the cost it saves.
    """
    if not commands:
        return "skipped", "", "у задачи нет validation_commands"
    ran = ran or {}
    logs: list[str] = []
    not_commands: list[str] = []
    executed = 0
    for cmd in commands:
        # #1103: judged ENTRY BY ENTRY. Prose still never reaches a shell —
        # that requirement (#546) is why this check exists at all: handed to a
        # shell, a sentence exits non-zero and the gate reads "validation
        # failed" about work that is fine. What changed is the blast radius.
        # The check used to sit BEFORE the loop and return for the whole list,
        # so one note written for a human suppressed every real command beside
        # it — measured on #1077, whose two entries begin with a real `gh` and
        # continue in prose: the step took 2s and nothing ran at all.
        if not is_command(cmd):
            not_commands.append(cmd)
            logs.append(f"$ {cmd}\n[не команда: не исполнялась]")
            log(f"validation {cmd[:120]!r}: not a command — not executed")
            continue
        proven = already_proven(cmd, ran)
        if proven is not None:
            outcome, by = proven
            log(
                f"validation {cmd!r}: not re-run — this job already ran "
                f"{by!r} with outcome {outcome}"
            )
            logs.append(
                f"$ {cmd}\n[not re-run: this job already ran {by!r} → {outcome}]"
            )
            # A reused outcome is EXECUTED evidence, not a skipped entry: the
            # job ran it, this process merely declined to run it twice (#1081).
            executed += 1
            if outcome == "fail":
                return "fail", "\n".join(logs)[-_LOG_TAIL:], f"команда упала: {cmd}"
            continue
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
        executed += 1
        logs.append(f"$ {cmd}\n{proc.stdout}{proc.stderr}")
        if proc.returncode != 0:
            return "fail", "\n".join(logs)[-_LOG_TAIL:], f"команда упала: {cmd}"
    if not_commands:
        # Partial execution is NOT a pass. The vocabulary the hub accepts is
        # pass | fail | unknown | skipped, and `unknown` is the only honest
        # member here: some entries were never checked, so the list as a whole
        # proves nothing — even though everything runnable in it passed. The
        # reason says both halves, because "unknown" alone would hide that the
        # real commands did run and did pass.
        shown = ", ".join(repr(c[:80]) for c in not_commands[:3])
        more = f" и ещё {len(not_commands) - 3}" if len(not_commands) > 3 else ""
        return (
            "unknown",
            "\n".join(logs)[-_LOG_TAIL:],
            f"исполнено и прошло {executed} из {len(commands)}; "
            f"не являются командами ({len(not_commands)}): {shown}{more}",
        )
    return "pass", "\n".join(logs)[-_LOG_TAIL:], ""


def main() -> int:
    base = env_get("HUB_URL").rstrip("/")
    token = env_get("HUB_CI_TOKEN")
    if not base or not token:
        log(
            "HAIPLANE_HUB_URL / HAIPLANE_HUB_CI_TOKEN "
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

    ran = parse_ran_commands(env_get("HUB_CI_RAN"))
    if ran:
        log(f"this job already ran {len(ran)} command(s); their outcomes can be reused")
    v_status, v_log, v_reason = run_validation(
        task.get("validation_commands") or [], ran
    )

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
