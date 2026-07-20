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

PASS = "pass"
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
    ac_models = [row_to_ac(r) for r in await repo.list_acceptance_criteria(db, task_id)]
    nodeid_by_ac: dict[str, str] = {}
    for ac in ac_models:
        if ac.verifiable_by.value != "test":
            continue
        parsed = parse_test_locator(ac.test_ref)
        if parsed is not None:
            nodeid_by_ac[ac.id] = parsed[1]
    if not nodeid_by_ac:
        return []

    task_row = await repo.get_task(db, task_id)
    generation = (dict(task_row).get("submission_generation") if task_row else 0) or 0
    ctx = await project_git_context(db, task_id)
    ran = await runner(list(nodeid_by_ac.values()), ctx.get("repo"))

    recorded: list[dict] = []
    for ac_id, nodeid in nodeid_by_ac.items():
        if ran is None or nodeid not in ran:
            status = NOT_FOUND
        else:
            status = PASS if ran[nodeid] else FAIL
        await repo.upsert_ac_test_result(db, task_id, ac_id, generation, status)
        recorded.append({"ac_id": ac_id, "status": status, "generation": generation})
    await db.commit()
    return recorded


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
