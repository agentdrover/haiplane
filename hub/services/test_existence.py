"""Static existence check for AC test locators (#506).

Complements #505: given a verifiable_by=test AC with a valid pytest locator,
check whether that test actually exists via ``pytest --collect-only`` — no test
body runs, no side effects. The result (resolvable / missing / unknown) makes a
broken AC↔test binding a visible defect in the review brief instead of a silent
hole. Best-effort: when the workspace or pytest is unavailable the status is
``unknown``, never a false ``missing``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from hub.process_kill import kill_process_group
from hub.services.test_locator import parse_test_locator

log = logging.getLogger("hub")

RESOLVABLE = "resolvable"
MISSING = "missing"
UNKNOWN = "unknown"

_COLLECT_TIMEOUT = 90


async def collect_test_nodeids(repo_path: str | None) -> set[str] | None:
    """Collect existing pytest nodeids in ``repo_path`` (best-effort, #506).

    Runs ``pytest --collect-only -q`` — collection only, no test body executes.
    Returns the set of nodeids, or ``None`` when collection could not run (no
    workspace, pytest missing, timeout, non-zero exit) so callers report
    ``unknown`` rather than a false ``missing``.
    """
    if not repo_path:
        return None
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "pytest",
            "--collect-only",
            "-q",
            "--no-header",
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own session, so the timeout path can signal the whole group (#544).
            start_new_session=True,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_COLLECT_TIMEOUT)
    except (OSError, TimeoutError, asyncio.TimeoutError):
        # wait_for only cancels the await — the child keeps running and would
        # accumulate orphaned collectors on every brief read (#506). Kill the
        # group, not the pid: we spawn `uv`, which runs pytest as a child, so
        # killing `uv` alone leaves the collector running (#544).
        await kill_process_group(proc)
        log.warning("AC locator collect-only failed in %s", repo_path)
        return None
    if proc.returncode != 0:
        # Collection errors (e.g. import failure) mean we cannot trust the set.
        return None
    nodeids: set[str] = set()
    for raw in out.decode(errors="replace").splitlines():
        line = raw.strip()
        if "::" in line and line.split("::", 1)[0].endswith(".py"):
            nodeids.add(line)
    return nodeids or None


def _verifiable_by(ac: Any) -> str:
    vb = getattr(ac, "verifiable_by", None)
    # Обещали str, а при отсутствующем поле отдавали None: сравнение с "test"
    # молча давало False, а .lower() у вызывающего дал бы AttributeError.
    return str(getattr(vb, "value", vb) or "")


def _base_nodeids(collected: set[str]) -> set[str]:
    """Parametrized nodeids reduced to their bare function form (#506).

    pytest only emits the parametrized ids (``…::test_x[case]``) and never the
    bare ``…::test_x``, but a bare locator is the documented common form and
    runs every case. Without this, a perfectly valid AC binding is reported as
    ``missing`` — the false-missing the module promises never to produce.
    """
    return {c.split("[", 1)[0] for c in collected if "[" in c}


def resolve_ac_locators(acs: Any, collected: set[str] | None) -> list[dict]:
    """Resolution status for each verifiable_by=test AC against ``collected`` (#506).

    ``collected`` is the set of existing nodeids, or ``None`` when collection
    could not run (→ ``unknown``). Non-test AC are skipped — they need no test.
    """
    resolutions: list[dict] = []
    bases = _base_nodeids(collected) if collected else set()
    for ac in acs:
        if _verifiable_by(ac) != "test":
            continue
        locator = getattr(ac, "test_ref", None)
        parsed = parse_test_locator(locator)
        if parsed is None:
            status, reason = MISSING, "no valid pytest locator in test_ref"
        elif collected is None:
            status, reason = UNKNOWN, "test collection unavailable in this environment"
        elif parsed[1] in collected or parsed[1] in bases:
            status, reason = RESOLVABLE, "test found by collection"
        else:
            status, reason = MISSING, "locator does not match any collected test"
        resolutions.append(
            {
                "ac_id": getattr(ac, "id", "?"),
                "locator": locator,
                "status": status,
                "reason": reason,
            }
        )
    return resolutions
