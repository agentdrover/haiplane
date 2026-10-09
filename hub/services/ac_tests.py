"""Run the tests bound to acceptance criteria and record the result (#507).

Given a task's verifiable_by=test AC with resolvable locators (#505/#506), run
those pytest nodeids and record pass/fail per AC, stamped with the current
``submission_generation``. A resubmission bumps the generation, so the old
result stops counting as current (same mechanic as review verdicts, #305). The
runner is injectable so the orchestration is testable without a real pytest.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from typing import Any, Awaitable, Callable

from hub import config
from hub import repository as repo
from hub.integrations.registry import plugins
from hub.process_kill import kill_process_group
from hub.services import test_existence
from hub.services.orchestration import project_git_context
from hub.services.refinement import row_to_ac
from hub.services.test_locator import PYTEST, parse_test_locator, runner_of

log = logging.getLogger("hub")

# A test outcome, not a credential — bandit's B105 matches the name alone.
PASS = "pass"  # nosec B105
FAIL = "fail"
NOT_FOUND = "not_found"

_RUN_TIMEOUT = 180

# What a manually started AC run may inherit from the hub (#1650). A list of
# what is allowed, not of what is forbidden: a name filter ("TOKEN", "KEY")
# lets through every secret that is called something else. HOME is not here on
# purpose — it is a fresh temporary directory per run. This is about the
# environment only; the run is still the code of the branch, not a sandbox.
_RUN_ENV_ALLOW = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
)

# runner(nodeids, repo_path) -> {nodeid: passed} for the tests it managed to
# run, or None when it could not run at all.
TestRunner = Callable[[list[str], str | None], Awaitable[dict[str, bool] | None]]


def _run_env(home: str) -> dict[str, str]:
    """The minimal environment of a manual AC run: the allowlist and a clean HOME."""
    env = {k: os.environ[k] for k in _RUN_ENV_ALLOW if k in os.environ}
    env["HOME"] = home
    return env


# One line of pytest -v output is kept up to this many bytes. A nodeid and its
# outcome sit at the start of the line, so the head is what is read; a longer
# line makes the whole run INCOMPLETE rather than silently shorter.
_LINE_CAP = 64 * 1024


# ``<nodeid> <OUTCOME> [ NN%]``: the outcome is the LAST token of the line, so
# the nodeid is everything before it, spaces and outcome words inside a
# parameter id included ("t.py::test_p[case PASSED] FAILED [100%]" is a FAILED
# test_p[case PASSED]). The pattern is anchored at the end and has no nested
# quantifiers.
_OUTCOME_LINE = re.compile(
    r"^(?P<nodeid>\S.*?) (?P<outcome>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
    r"(?: +\[ *\d+%\])? *$"
)


def _apply_line(raw: bytes, wanted: set[str], results: dict[str, bool]) -> None:
    line = raw.decode(errors="replace").rstrip()
    match = _OUTCOME_LINE.match(line)
    if match is None:
        return
    reported, outcome = match["nodeid"], match["outcome"]
    # Match the EXACT nodeid (or its parametrized base) — substring matching
    # let "…::test_a" absorb the verdict of "…::test_a_extra" (#507).
    key = reported if reported in wanted else reported.split("[", 1)[0]
    if key not in wanted:
        return
    if outcome == "PASSED":
        passed = True
    elif outcome in ("FAILED", "ERROR"):
        passed = False
    else:
        return
    # Aggregate parametrized cases: any failing case fails the AC.
    results[key] = results.get(key, True) and passed


async def _stream_results(proc: Any, wanted: set[str]) -> tuple[dict[str, bool], bool]:
    """Results aggregated over the WHOLE output as it streams; memory stays bounded.

    The output used to be cut at a byte limit and parsed afterwards, so a FAILED
    printed after the limit was lost and the AC read as passed (#1650). Now every
    complete line is read as it arrives and nothing but the current line is kept.
    ``incomplete`` is True when a line was too long to be read whole.
    """
    results: dict[str, bool] = {}
    pending = b""
    skipping = False
    incomplete = False
    while True:
        chunk = await proc.stdout.read(65536)
        if not chunk:
            break
        if skipping:
            _, newline, chunk = chunk.partition(b"\n")
            if not newline:
                continue
            skipping = False
        pending += chunk
        *lines, pending = pending.split(b"\n")
        for raw in lines:
            _apply_line(raw[:_LINE_CAP], wanted, results)
            incomplete = incomplete or len(raw) > _LINE_CAP
        if len(pending) > _LINE_CAP:
            _apply_line(pending[:_LINE_CAP], wanted, results)
            pending, skipping, incomplete = b"", True, True
    if pending:
        _apply_line(pending[:_LINE_CAP], wanted, results)
        incomplete = incomplete or len(pending) > _LINE_CAP
    await proc.wait()
    return results, incomplete


async def default_test_runner(
    nodeids: list[str], repo_path: str | None
) -> dict[str, bool] | None:
    """Run ``nodeids`` with pytest in ``repo_path`` (best-effort, #507).

    The ONE place the hub starts pytest, and only for a human's explicit
    run-ac-tests (#1646). The child gets the allowlisted environment, its own
    session — the timeout and a cancelled request kill the whole group (by the
    group id saved at spawn, so a launcher that already exited does not hide
    its children; after a NORMAL exit the saved number is not used, because
    once the group is empty it may belong to someone else), and results are
    read as a stream (#1650). Incomplete output
    yields no positive result.
    """
    if not nodeids or not repo_path:
        return None
    home = tempfile.mkdtemp(prefix="hub-ac-run-")
    proc = None
    pgid = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "pytest",
            *nodeids,
            "-v",
            "--no-header",
            "-p",
            "no:cacheprovider",
            cwd=repo_path,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_run_env(home),
            start_new_session=True,
        )
        # With start_new_session the child leads a group of its own: the group
        # id is its pid, and it stays the group id after the leader exits.
        pgid = proc.pid
        results, incomplete = await asyncio.wait_for(
            _stream_results(proc, set(nodeids)), timeout=_RUN_TIMEOUT
        )
    except (OSError, TimeoutError, asyncio.TimeoutError):
        await kill_process_group(proc, pgid=pgid)
        log.warning("AC test run failed in %s", repo_path)
        return None
    except asyncio.CancelledError:
        await kill_process_group(proc, pgid=pgid)
        raise
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if incomplete:
        # Part of the output was unreadable: a failure may be hiding in it, so
        # nothing may pass on this run.
        return {k: v for k, v in results.items() if not v}
    return results


async def test_ac_nodeids(db: Any, task_id: int) -> dict[str, str]:
    """{ac_id: nodeid} for every verifiable_by=test AC with a valid locator.

    The single answer to "what would a run of this task cover" — shared by the
    local runner and by the CI report intake (#546), so the two can never
    disagree about which AC a run was allowed to speak for.
    """
    ac_models = [row_to_ac(r) for r in await repo.list_acceptance_criteria(db, task_id)]
    out: dict[str, str] = {}
    for ac in ac_models:
        if ac.verifiable_by.value != "test":
            continue
        parsed = parse_test_locator(ac.test_ref)
        if parsed is not None:
            out[ac.id] = parsed[1]
    return out


async def runnable_ac_nodeids(db: Any, task_id: int) -> dict[str, str]:
    """{ac_id: nodeid} for the test-AC this hub can actually RUN (#1203).

    Today that is pytest and only pytest. Handing a vitest locator to
    ``uv run pytest`` produced ``not_found`` — the same word a genuinely
    missing test gets — so the author was told their test was absent when the
    truth was that nothing had tried to run it.
    """
    return {
        ac_id: nodeid
        for ac_id, nodeid in (await test_ac_nodeids(db, task_id)).items()
        if runner_of(nodeid) == PYTEST
    }


async def unresolved_locators(
    db: Any,
    task_id: int,
) -> tuple[dict[str, str], bool]:
    """AC whose named test is PROVEN absent, and whether the check ran (#1032).

    Returns ``({ac_id: nodeid}, checked)``. The files are read with git at one
    ref (submission_sha → task branch → project base) and parsed, never
    imported (#1650): approving an epic must not run code from the clone.
    Only ``missing`` is a dead locator. ``unknown`` and ``unparseable`` mean
    the hub could not tell, and ``checked=False`` says the whole look gave no
    answer at all — the empty mapping then says nothing about the locators,
    which is why the flag travels with it instead of being inferred from an
    empty result (#725).

    Only well-formed locators are asked about: a malformed ``test_ref`` is a
    different defect, already refused by the refine gate where the policy
    requires it.
    """
    # Only locators this hub can read: a foreign one is not "dead", it is
    # unasked, and reporting it here would be an accusation (#1203).
    nodeid_by_ac = await runnable_ac_nodeids(db, task_id)
    if not nodeid_by_ac:
        return {}, True
    ctx = await project_git_context(db, task_id)
    task = dict(await repo.get_task(db, task_id) or {})
    acs = [
        ac
        for ac in (
            row_to_ac(r) for r in await repo.list_acceptance_criteria(db, task_id)
        )
        if ac.id in nodeid_by_ac
    ]
    resolutions = await test_existence.resolve_locators_at_ref(
        plugins.git_ops,
        ctx.get("repo"),
        acs,
        submission_sha=task.get("submission_sha") or "",
        branch=task.get("branch") or "",
        base=ctx.get("base_branch") or config.PAIR_BASE_BRANCH,
    )
    if all(r["status"] != test_existence.MISSING for r in resolutions) and all(
        r["status"] in (test_existence.UNKNOWN, test_existence.UNPARSEABLE)
        for r in resolutions
    ):
        return {}, False
    dead = {
        r["ac_id"]: nodeid_by_ac[r["ac_id"]]
        for r in resolutions
        if r["status"] == test_existence.MISSING
    }
    return dead, True


async def record_ac_test_results(
    db: Any,
    task_id: int,
    statuses: dict[str, str],
    generation: int,
) -> list[dict]:
    """Write per-AC outcomes for ``generation``. Does NOT commit (#546).

    The one write path for AC results: the local runner (#507) and the CI
    report intake (#546) both come through here, so a result written by a
    runner and a result written by a report are the same fact, stored the same
    way. Callers own the transaction — the submission path needs these rows to
    land inside its own write lock.
    """
    recorded: list[dict] = []
    for ac_id, status in statuses.items():
        await repo.upsert_ac_test_result(db, task_id, ac_id, generation, status)
        recorded.append({"ac_id": ac_id, "status": status, "generation": generation})
    return recorded


async def run_ac_tests(
    db: Any,
    task_id: int,
    *,
    runner: TestRunner | None = None,
) -> list[dict]:
    """Run the bound tests for a task's test-AC and record results (#507).

    Records one row per verifiable_by=test AC (with a valid locator) stamped
    with the current submission_generation: ``pass``/``fail`` from the runner,
    or ``not_found`` when the test could not be run/located. Returns the
    recorded results. Non-test AC and AC without a valid locator are skipped.
    """
    runner = runner or default_test_runner
    # An unrunnable locator gets no row at all rather than a not_found one:
    # a recorded not_found is read as "the test is not there" (#1203).
    nodeid_by_ac = await runnable_ac_nodeids(db, task_id)
    if not nodeid_by_ac:
        return []

    task_row = await repo.get_task(db, task_id)
    generation = (dict(task_row).get("submission_generation") if task_row else 0) or 0
    ctx = await project_git_context(db, task_id)
    ran = await runner(list(nodeid_by_ac.values()), ctx.get("repo"))

    statuses: dict[str, str] = {}
    for ac_id, nodeid in nodeid_by_ac.items():
        if ran is None or nodeid not in ran:
            statuses[ac_id] = NOT_FOUND
        else:
            statuses[ac_id] = PASS if ran[nodeid] else FAIL
    recorded = await record_ac_test_results(db, task_id, statuses, generation)
    await db.commit()
    return recorded


async def ac_tests_gap(db: Any, task: dict) -> str | None:
    """None when every current test-AC is green; else a human-readable gap (#508).

    A gap exists when any verifiable_by=test AC has no result for the current
    submission_generation, has a result that is not ``pass``, or carries a
    locator no runner can resolve. That last case must count: an unlocatable AC
    is never run by #507, so exempting it here let an AC declared
    machine-verifiable pass the require gate with zero test evidence — and
    SDD_AC_LOCATOR defaults to off, so refine accepts such a locator. Non-test
    AC never contribute; they are not gated by this policy.
    """
    task_id = task["id"]
    ac_models = [row_to_ac(r) for r in await repo.list_acceptance_criteria(db, task_id)]
    test_acs = [ac for ac in ac_models if ac.verifiable_by.value == "test"]
    if not test_acs:
        return None
    unlocatable = [ac.id for ac in test_acs if not parse_test_locator(ac.test_ref)]
    # Well-formed, but for a runner this hub cannot run. Kept apart from both
    # other groups: the author has nothing to fix, and a line that lumps it in
    # with "локатор не разрешается" sends them editing a correct locator
    # (#419, #1203).
    unrunnable = {
        ac.id: runner_of(ac.test_ref)
        for ac in test_acs
        if ac.id not in unlocatable and runner_of(ac.test_ref) != PYTEST
    }
    generation = task.get("submission_generation") or 0
    rows = {
        dict(r)["ac_id"]: dict(r) for r in await repo.list_ac_test_results(db, task_id)
    }
    not_green = [
        ac.id
        for ac in test_acs
        if ac.id not in unlocatable
        and ac.id not in unrunnable
        and (
            (r := rows.get(ac.id)) is None
            or r["submission_generation"] != generation
            or r["status"] != PASS
        )
    ]
    gaps = []
    if not_green:
        gaps.append(
            "AC-тесты не зелёные для текущего поколения: " + ", ".join(not_green)
        )
    if unlocatable:
        gaps.append(
            "AC объявлены verifiable_by=test, но локатор теста не разрешается: "
            + ", ".join(unlocatable)
        )
    if unrunnable:
        named = ", ".join(f"{ac_id} ({runner})" for ac_id, runner in unrunnable.items())
        gaps.append(
            "AC объявлены verifiable_by=test, локатор верен, но его раннер хаб "
            "прогнать не умеет — правки локатора это не требует: " + named
        )
    return "; ".join(gaps) if gaps else None


def current_ac_test_results(rows: Any, generation: int) -> list[dict]:
    """Filter recorded AC results to those stamped with ``generation`` (#507)."""
    out = []
    for r in rows:
        d = dict(r)
        out.append(
            {
                "ac_id": d["ac_id"],
                "status": d["status"],
                "is_current": d["submission_generation"] == generation,
            }
        )
    return out


def describe_recorded_results(recorded: Any) -> str:
    """Say what the recorded AC results ARE, not how many rows there are (#1056).

    The submission report used to print ``len(ac_recorded)`` — five results
    that all said ``not_found`` and five that all passed came out as the same
    sentence, "5 AC result(s)". On #1042 that sentence stood in the feed while
    not one of the five locators resolved, and two verdicts in a row were
    written without the fact ever being mentioned. A count is not an outcome,
    and the rule the hub enforces on agent reports (#762: an absent answer is
    never a clean one) binds the hub's own lines too.

    Names the outcome, never grades the work: "none of 5 resolved" is a fact
    about locators, not a judgement about the submission.
    """
    rows = [dict(r) for r in recorded or []]
    total = len(rows)
    if not total:
        return "AC: none reported"
    not_found = sum(1 for r in rows if r.get("status") == NOT_FOUND)
    failing = sum(1 for r in rows if r.get("status") == FAIL)
    resolved = total - not_found
    if resolved == 0:
        head = f"AC: none of {total} resolved ({total} {NOT_FOUND})"
    elif not_found == 0:
        head = f"AC: all {total} resolved"
    else:
        head = f"AC: {resolved} of {total} resolved ({not_found} {NOT_FOUND})"
    return f"{head}, {failing} failing" if failing else head
