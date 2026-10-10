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
time; absent ⇒ nothing is reused and every command runs as before),
HAIPLANE_HUB_CI_MUTATIONS (#1270: path to the JSON written by
scripts/mutation_changed.py — sent under its own ``mutations`` key; absent or
unreadable ⇒ the key is not sent, and the log says why),
HAIPLANE_HUB_CI_BASELINE (#913: path to the JSON written by
scripts/red_test_baseline.py — the branch's changed tests run over the
merge-base code, sent under its own ``baseline`` key; absent or unreadable ⇒
the key is not sent, and the log says why),
HAIPLANE_HUB_CI_MUTATIONS_OUTCOME / HAIPLANE_HUB_CI_BASELINE_OUTCOME (#1606: how
the step that wrote the file ended. ``skipped`` ⇒ the step did not run and the
key is NOT sent, so the hub keeps what it stored for this commit; any other
value ⇒ it ran, and a missing or broken file is sent as an explicit
``state=error`` with its cause; empty ⇒ not stated, the file alone decides),
HAIPLANE_HUB_CI_EVENT_ACTION (#1606: the event's activity type, part of the
provenance — run id, URL, event, time — written inside each evidence block).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess  # nosec B404 - runs the task's own declared commands, in CI
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable

TASK_BRANCH = re.compile(r"^task-(\d+)/")
# A validation entry we are willing to hand to a shell. Task text mixes real
# commands with prose instructions for humans ("check on a live PR that…"), and
# handing prose to a shell produces a non-zero exit — a false failure about the
# work. Anything that does not look like an executable invocation is reported as
# unknown with its text, never as a failure.
COMMAND_TOKEN = re.compile(r"^[A-Za-z0-9._/-]+$")
_RUN_TIMEOUT = 900
# #1666: the CI step that runs this script is capped at 20 minutes, while the
# budgets above are per call (AC run 900 s, then 900 s for EACH validation
# command). Without one overall deadline GitHub could kill the step before the
# POST and the evidence would vanish silently. Past 17 minutes from start no
# further command is launched, what was left is named as not run, and the
# report is still sent.
_REPORT_DEADLINE = 17 * 60
_STARTED = time.monotonic()
_LOG_TAIL = 4000
# The default is this repository's own runner. A satellite repository sets
# HAIPLANE_HUB_CI_PYTEST to whatever runs ITS tests; an unparsable or missing
# runner reports not_found with a reason, never a failure about the work.
_DEFAULT_AC_RUNNER = "uv run pytest"


def _remaining() -> float:
    """Seconds left before the reporter must stop launching work and send."""
    return _REPORT_DEADLINE - (time.monotonic() - _STARTED)


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


# pytest names an id it cannot resolve on its own line and then stops
# collecting: "no tests ran", with the rest of the ids unrun.
_NOT_FOUND_LINE = re.compile(
    r"^ERROR: (file or directory not found|not found): (\S+)", re.MULTILINE
)


def _split_id(ident: str) -> tuple[str, str]:
    """(absolute normalized file path, ``::`` tail) of a test id or path.

    pytest may print an absolute path for a relative argument; both are brought
    to one form, relative to the directory the tests are run from, and compared
    for equality, never by suffix: ``nested/t.py`` is not ``t.py``.
    """
    path, sep, tail = ident.partition("::")
    return os.path.abspath(path), sep + tail


def _missing_nodeids(output: str, nodeids: list[str]) -> dict[str, str]:
    """{nodeid: reason} for the ids pytest said it could not find.

    "not found: <id>" names one test id. "file or directory not found: <path>"
    names a whole file (with or without a ``::test`` tail), so every id living
    in that file is missing with it.
    """
    missing: dict[str, str] = {}
    split = {nodeid: _split_id(nodeid) for nodeid in nodeids}
    for match in _NOT_FOUND_LINE.finditer(output):
        kind, reported = match.groups()
        reason = match.group(0).removeprefix("ERROR: ")
        r_file, r_tail = _split_id(reported)
        for nodeid, (n_file, n_tail) in split.items():
            if r_file == n_file and (kind != "not found" or r_tail == n_tail):
                missing[nodeid] = reason
    return missing


def _parse_outcomes(stdout: str, nodeids: list[str]) -> dict[str, bool]:
    out: dict[str, bool] = {}
    wanted = set(nodeids)
    for raw in stdout.splitlines():
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


def run_nodeids(nodeids: list[str]) -> dict[str, bool]:
    """Run the AC tests for ``nodeids`` and return {nodeid: passed} for what ran.

    One pytest start for the ids. If pytest reports some of them as not found it
    has stopped collecting, so those are set aside with the reason it gave (they
    stay not_found) and only the rest are run again: a typo in one test_ref must
    not hide the real outcome of the others (#1581). Nothing missing costs
    nothing extra; each distinct missing batch costs one more start, all inside
    one ``_RUN_TIMEOUT`` budget.
    """
    if not nodeids:
        return {}
    runner = ac_runner()
    if not runner:
        return {}
    deadline = time.monotonic() + min(_RUN_TIMEOUT, max(1.0, _remaining()))
    pending = list(nodeids)
    while pending:
        if _remaining() <= 0:
            log(
                "report deadline reached — the AC tests were not run, they stay not_found"
            )
            return {}
        cmd = [*runner, *pending, "-v", "--no-header", "-p", "no:cacheprovider"]
        try:
            proc = subprocess.run(  # nosec B603 - fixed argv, nodeids come from the hub
                cmd,
                capture_output=True,
                text=True,
                timeout=max(1.0, deadline - time.monotonic()),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log(f"pytest could not run ({exc}) — every AC stays not_found")
            return {}
        missing = _missing_nodeids(proc.stdout + "\n" + proc.stderr, pending)
        if not missing:
            return _parse_outcomes(proc.stdout, pending)
        for nodeid, reason in sorted(missing.items()):
            log(f"{nodeid} stays not_found: pytest says {reason}")
        pending = [n for n in pending if n not in missing]
    return {}


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
    # #1666: only prints the slowest tests, selects nothing.
    "--durations",
    "--durations-min",
}


# Whitelisted flags whose value may live in the next token rather than after
# an "=". Kept separate from the whitelist itself: membership here decides
# whether a token is EATEN, and eating one too many is how a narrowed run gets
# mistaken for the whole suite.
_VALUE_TAKING_FLAGS = {
    "-n",
    "--numprocesses",
    "--dist",
    "--tb",
    "--color",
    "-r",
    "--durations",
    "--durations-min",
}


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
        if _remaining() <= 0:
            # #1666: the deadline is spent: do not launch this one or the rest.
            idx = commands.index(cmd)
            skipped = [c for c in commands[idx:] if is_command(c)]
            for c in skipped:
                logs.append(f"$ {c}\n[не выполнена: deadline отчёта исчерпан]")
            log(f"report deadline reached — {len(skipped)} command(s) not run")
            return (
                "unknown",
                "\n".join(logs)[-_LOG_TAIL:],
                f"deadline отчёта исчерпан; исполнено {executed} из "
                f"{len(commands)}, не выполнены по deadline ({len(skipped)}): "
                + ", ".join(repr(c[:80]) for c in skipped[:3]),
            )
        try:
            proc = subprocess.run(  # nosec B602 - the task's own declared commands, run in a disposable CI runner
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=min(_RUN_TIMEOUT, max(1.0, _remaining())),
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


# The hub refuses a mutation report above 32 000 chars (#1270). Survivors are
# trimmed well below that here, and the trimmed count is sent alongside: a list
# cut silently would read as "these are all of them".
_MUTATION_SURVIVORS_MAX = 40
_MUTATION_DIFF_MAX = 300


def trim_mutations(report: dict) -> dict:
    """Bound the survivor list; counts are never trimmed."""
    survivors = report.get("survivors")
    if not isinstance(survivors, list):
        return report
    kept = [
        {**s, "diff": str(s["diff"])[-_MUTATION_DIFF_MAX:]}
        if isinstance(s, dict) and "diff" in s
        else s
        for s in survivors[:_MUTATION_SURVIVORS_MAX]
    ]
    trimmed = dict(report, survivors=kept)
    if len(survivors) > len(kept):
        trimmed["survivors_trimmed"] = len(survivors) - len(kept)
    return trimmed


def _read_report(path: str) -> tuple[dict | None, str]:
    """A step's JSON file as (report, ""), or (None, the reason it is unusable)."""
    try:
        with open(path, encoding="utf-8") as handle:
            report = json.load(handle)
    except (OSError, ValueError) as exc:
        return None, str(exc)
    if not isinstance(report, dict):
        return None, "expected an object"
    return report, ""


# The states each step's own script can write (scripts/mutation_changed.py,
# scripts/red_test_baseline.py). A JSON object without one of them is not that
# step's report, and provenance on it would make it look like evidence.
_MUTATION_STATES = frozenset(
    {"ran", "baseline_red", "no_changed_functions", "no_tests", "error"}
)
_BASELINE_STATES = frozenset({"ran", "no_tests", "error"})


def provenance() -> dict:
    """Which run produced an evidence block (#1606): kept INSIDE the block.

    The hub shows it next to the evidence, and "run unknown" for a block that
    predates it. No new column: the block is already free-form JSON.
    """
    server = os.environ.get("GITHUB_SERVER_URL") or "https://github.com"
    repository = os.environ.get("GITHUB_REPOSITORY") or ""
    run_id = os.environ.get("GITHUB_RUN_ID") or ""
    event = os.environ.get("GITHUB_EVENT_NAME") or ""
    action = env_get("HUB_CI_EVENT_ACTION")
    return {
        "run_id": run_id,
        "run_url": (
            f"{server}/{repository}/actions/runs/{run_id}"
            if run_id and repository
            else ""
        ),
        "event": f"{event}.{action}" if event and action else event,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def evidence_block(
    label: str,
    path: str,
    outcome: str,
    trim: Callable[[dict], dict],
    states: frozenset[str],
) -> dict | None:
    """The block to send under an evidence key, or None for "send no key" (#1606).

    Three cases, never collapsed: a step that did not run (``skipped``) sends
    no key, because the hub reads an absent key as "keep what you stored"; a
    step that ran but left no usable file sends ``state=error`` with the cause,
    because a timeout is a fact the reader must see; a valid file is sent as
    it is. An outcome the caller does not state falls back to the file alone,
    which is how reporters written before this contract behave.
    """
    outcome = (outcome or "").strip().lower()
    if not path:
        return None
    if outcome == "skipped":
        log(f"{label} step did not run — no key sent; the hub keeps what it stored")
        return None
    report, why = _read_report(path)
    if report is not None and report.get("state") not in states:
        report, why = (
            None,
            f"state {report.get('state')!r} is not one of {sorted(states)}",
        )
    if report is None:
        log(f"{label} report {path} not usable: {why}")
        if not outcome:
            return None
        report = {
            "state": "error",
            "reason": (
                f"the {label} step ended {outcome} without a readable report ({why})"
            ),
        }
    else:
        report = trim(report)
    return {**report, "provenance": provenance()}


# The hub refuses a baseline above 32 000 chars (#913). A changed test module
# can hold hundreds of tests; the AC tests are always kept, the rest trimmed,
# and the trimmed count is sent — a cut list must not read as "all of them".
_BASELINE_TESTS_MAX = 200
_BASELINE_ERRORS_MAX = 30


def trim_baseline(report: dict, keep: set[str]) -> dict:
    """Bound the per-test map; AC nodeids (and their variants) are never cut."""
    tests = report.get("tests")
    if not isinstance(tests, dict):
        return report

    def wanted(nodeid: str) -> bool:
        return nodeid in keep or nodeid.split("[", 1)[0] in keep

    kept = {k: v for k, v in tests.items() if wanted(k)}
    for nodeid, status in tests.items():
        if len(kept) >= _BASELINE_TESTS_MAX:
            break
        kept.setdefault(nodeid, status)
    trimmed = dict(report, tests=kept)
    if len(tests) > len(kept):
        trimmed["tests_trimmed"] = len(tests) - len(kept)
    errors = report.get("collection_errors")
    if isinstance(errors, dict) and len(errors) > _BASELINE_ERRORS_MAX:
        trimmed["collection_errors"] = dict(list(errors.items())[:_BASELINE_ERRORS_MAX])
    return trimmed


def attach_evidence(payload: dict, keep: set[str]) -> None:
    """Add the mutations / baseline keys the run actually has to report."""
    mutations = evidence_block(
        "mutation",
        env_get("HUB_CI_MUTATIONS"),
        env_get("HUB_CI_MUTATIONS_OUTCOME"),
        trim_mutations,
        _MUTATION_STATES,
    )
    if mutations is not None:
        payload["mutations"] = mutations
        log(f"mutation run reported: state={mutations.get('state')!r}")
    baseline = evidence_block(
        "baseline",
        env_get("HUB_CI_BASELINE"),
        env_get("HUB_CI_BASELINE_OUTCOME"),
        lambda report: trim_baseline(report, keep),
        _BASELINE_STATES,
    )
    if baseline is not None:
        payload["baseline"] = baseline
        log(
            f"red-test baseline reported: state={baseline.get('state')!r}, "
            f"merge_base={str(baseline.get('merge_base'))[:12]}, "
            f"tests={len(baseline.get('tests') or {})}"
        )
        for nodeid in sorted(keep):
            log(
                f"  baseline {nodeid}: {(baseline.get('tests') or {}).get(nodeid, '—')}"
            )


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
    attach_evidence(payload, set(nodeid_by_ac.values()))
    result = hub_request(f"{base}/api/tasks/{task_id}/ci-run-report", token, payload)
    if result is None:
        log("report not delivered — the hub will read this as unknown")
        return 0
    log(
        f"reported {len(ac_results)} AC result(s), validation={v_status}; "
        f"applied={result.get('applied')} ({result.get('reason')}); "
        f"mutations={result.get('mutations_state', 'not accepted by this hub')}; "
        f"baseline={result.get('baseline_state', 'not accepted by this hub')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
