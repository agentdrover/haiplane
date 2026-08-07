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
from typing import Any, Awaitable, Callable

from hub import repository as repo
from hub.services.orchestration import project_git_context
from hub.services.refinement import row_to_ac
from hub.services.test_locator import parse_test_locator

log = logging.getLogger("hub")

# A test outcome, not a credential — bandit's B105 matches the name alone.
PASS = "pass"  # nosec B105
FAIL = "fail"
NOT_FOUND = "not_found"

_RUN_TIMEOUT = 180

# runner(nodeids, repo_path) -> {nodeid: passed} for the tests it managed to
# run, or None when it could not run at all.
TestRunner = Callable[[list[str], str | None], Awaitable[dict[str, bool] | None]]


async def default_test_runner(
    nodeids: list[str], repo_path: str | None
) -> dict[str, bool] | None:
    """Run ``nodeids`` with pytest in ``repo_path`` (best-effort, #507)."""
    if not nodeids or not repo_path:
        return None
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
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_RUN_TIMEOUT)
    except (OSError, TimeoutError, asyncio.TimeoutError):
        log.warning("AC test run failed in %s", repo_path)
        return None
    results: dict[str, bool] = {}
    wanted = set(nodeids)
    for raw in out.decode(errors="replace").splitlines():
        parts = raw.strip().split(None, 1)
        if len(parts) != 2:
            continue
        reported, rest = parts
        # pytest -v prints the nodeid first, then the outcome. Match the EXACT
        # nodeid (or its parametrized base) — substring matching let
        # "…::test_a" absorb the verdict of "…::test_a_extra", and the last
        # matching line silently overwrote earlier ones, so a passing AC could
        # be recorded as failed or vice versa (#507).
        key = reported if reported in wanted else reported.split("[", 1)[0]
        if key not in wanted:
            continue
        if "PASSED" in rest:
            passed = True
        elif "FAILED" in rest or "ERROR" in rest:
            passed = False
        else:
            continue
        # Aggregate parametrized cases: any failing case fails the AC.
        results[key] = results.get(key, True) and passed
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
    nodeid_by_ac = await test_ac_nodeids(db, task_id)
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
    generation = task.get("submission_generation") or 0
    rows = {
        dict(r)["ac_id"]: dict(r) for r in await repo.list_ac_test_results(db, task_id)
    }
    not_green = [
        ac.id
        for ac in test_acs
        if ac.id not in unlocatable
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
