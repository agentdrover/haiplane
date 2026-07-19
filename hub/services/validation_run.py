"""Run a task's declared validation_commands and record the result (#509).

validation_commands were declared as "the commands that prove the change works"
but the hub never ran them. This executes them in the project workspace and
records pass/fail stamped with the submission_generation, so #510 can gate
completion on them. The runner is injectable for testing. Secret env vars are
stripped from the child environment so a command cannot read and echo them into
the recorded log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable

from hub import repository as repo
from hub.services.orchestration import project_git_context

log = logging.getLogger("hub")

PASS = "pass"
FAIL = "fail"
SKIPPED = "skipped"
UNKNOWN = "unknown"

_RUN_TIMEOUT = 300
_LOG_TAIL = 4000

# runner(commands, repo_path) -> (returncode, log_tail) or None if it could not run.
ValidationRunner = Callable[[list[str], str | None], Awaitable[tuple[int, str] | None]]

_SECRET_HINTS = ("TOKEN", "SECRET", "PASSWORD", "KEY", "CREDENTIAL")


def _safe_env() -> dict[str, str]:
    """Child env with obvious secrets stripped (#509 AC-2, best-effort)."""
    return {
        k: v
        for k, v in os.environ.items()
        if not any(hint in k.upper() for hint in _SECRET_HINTS)
    }


async def default_validation_runner(
    commands: list[str], repo_path: str | None
) -> tuple[int, str] | None:
    """Run ``commands`` in ``repo_path`` in order, stop at the first failure."""
    if not commands or not repo_path:
        return None
    env = _safe_env()
    logs: list[str] = []
    rc = 0
    for cmd in commands:
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=repo_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=_RUN_TIMEOUT)
        except (OSError, TimeoutError, asyncio.TimeoutError):
            log.warning("validation command failed to run in %s", repo_path)
            return None
        logs.append(f"$ {cmd}\n{out.decode(errors='replace')}")
        rc = proc.returncode or 0
        if rc != 0:
            break
    return rc, "\n".join(logs)[-_LOG_TAIL:]


async def run_validation_commands(
    db: Any,
    task_id: int,
    *,
    runner: ValidationRunner | None = None,
) -> dict:
    """Run a task's validation_commands and record the result (#509).

    Records pass/fail (or unknown when it could not run) stamped with the
    current submission_generation. When there are no commands the step is
    skipped and nothing is recorded (AC-3).
    """
    runner = runner or default_validation_runner
    row = await repo.get_task(db, task_id)
    if not row:
        return {"status": SKIPPED, "generation": 0}
    task = dict(row)
    commands = json.loads(task.get("validation_commands") or "[]")
    generation = task.get("submission_generation") or 0
    if not commands:
        return {"status": SKIPPED, "generation": generation}

    ctx = await project_git_context(db, task_id)
    result = await runner(commands, ctx.get("repo"))
    if result is None:
        status, log_tail = UNKNOWN, ""
    else:
        rc, log_tail = result
        status = PASS if rc == 0 else FAIL
    await repo.update_task(
        db,
        task_id,
        validation_generation=generation,
        validation_status=status,
        validation_log=log_tail,
    )
    await db.commit()
    return {"status": status, "generation": generation}


def validation_gap(task: dict) -> str | None:
    """None when the current validation result is green; else a reason (#510).

    A gap exists when the task has validation_commands but the recorded result
    is missing / stale (wrong generation) / not ``pass``. No commands ⇒ no gap.
    """
    commands = task.get("validation_commands")
    if isinstance(commands, str):
        commands = json.loads(commands or "[]")
    if not commands:
        return None
    generation = task.get("submission_generation") or 0
    if task.get("validation_generation") != generation:
        return "validation_commands не прогонялись для текущего поколения"
    if task.get("validation_status") != PASS:
        return f"validation_commands не прошли: статус {task.get('validation_status')}"
    return None
