"""What is running in production, and what finished without getting there (#499).

The delivery facts all exist now — the deploy CI reported (#839, #496), the
merges the hub performed (#534), and the comparison between them (#497). What
was missing is the question asked of the whole board at once: a card answers
for one task, and "what has not reached production" meant opening cards one by
one.

Assembled once and read by three interfaces (REST, CLI, MCP). They agree
because there is one builder, not because three call sites are kept in step —
the same reasoning #808 applied to the review report and #823 to the evidence
panel.

Two rules inherited from the facts underneath:

- ``unknown`` is its own list. Folding it into ``not_in_prod`` would turn "we
  could not tell" into "it did not ship", which is the defect this whole epic
  removed (#839, #497, #883).
- the window is bounded AND the bound is stated. Delivery state costs a git
  question per task, so the snapshot covers the newest completed tasks; a
  silently truncated list reads as the whole board, which is the failure #824
  refused to ship.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from hub import repository as repo
from hub.services.delivery_state import (
    IN_PROD,
    NOT_IN_PROD,
    _GitCheck,
    git_delivery_state,
    prepare_delivery,
)

log = logging.getLogger("hub")

DEFAULT_WINDOW = 50
MAX_WINDOW = 200

# #1603: the snapshot used to walk up to 50 tasks one by one, git on each, and
# answered after the MCP client had already given up (12 of 24 calls in 14
# days). The git questions are independent, so they run side by side — but the
# host is 2 CPU, so the ceiling is small. The budget is strictly below the
# client's wait (``mcp_server._TIMEOUT_DEFAULT``, guarded by a test): whatever
# did not finish is reported as unknown BY NAME, never as not_in_prod.
MAX_CONCURRENCY = 8
BUILD_BUDGET_SECONDS = 10.0
BUDGET_REASON = (
    "срок сборки снимка исчерпан — задача не проверена. "
    "Это не «не раскатано»: повторите запрос"
)


async def _answers(
    db: Any, tasks: list[dict[str, Any]], release: Any, started: float
) -> list[dict[str, Any] | None]:
    """One answer per task, in task order; ``None`` where the budget ran out.

    SQL first and in sequence (one aiosqlite connection), git second and side
    by side. Cancelled checks take their git processes with them
    (``proc.run`` kills the group on cancellation).
    """
    results: list[dict[str, Any] | None] = [None] * len(tasks)
    pending_checks: list[tuple[int, _GitCheck]] = []
    for i, task in enumerate(tasks):
        prepared = await prepare_delivery(db, int(task["id"]), release=release)
        if isinstance(prepared, dict):
            results[i] = prepared
        else:
            pending_checks.append((i, prepared))

    gate = asyncio.Semaphore(MAX_CONCURRENCY)

    async def one(check: _GitCheck) -> dict[str, Any]:
        async with gate:
            return await git_delivery_state(check)

    futures = {asyncio.ensure_future(one(check)): i for i, check in pending_checks}
    if futures:
        left = max(0.0, BUILD_BUDGET_SECONDS - (time.monotonic() - started))
        try:
            done, pending = await asyncio.wait(futures, timeout=left)
        except BaseException:
            for fut in futures:
                fut.cancel()
            raise
        for fut in pending:
            fut.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for fut in done:
            results[futures[fut]] = fut.result()
    return results


async def prod_state(db: Any, *, limit: int = DEFAULT_WINDOW) -> dict[str, Any]:
    """A snapshot of production: what is deployed and which tasks are where."""
    window = max(1, min(int(limit or DEFAULT_WINDOW), MAX_WINDOW))

    started = time.monotonic()
    release = await repo.latest_successful_release(db)
    deployed = {
        "sha": str(release.get("deployed_sha") or "") if release else "",
        "ref": str(release.get("ref") or "") if release else "",
        "at": str(release.get("deployed_at") or "") if release else "",
        "source": str(release.get("source") or "") if release else "",
    }

    rows = await repo.list_tasks_by_status(db, "completed", limit=window)
    tasks = [dict(r) for r in rows]

    buckets: dict[str, list[dict[str, Any]]] = {
        IN_PROD: [],
        NOT_IN_PROD: [],
        "unknown": [],
    }
    unchecked = 0
    for task, answer in zip(tasks, await _answers(db, tasks, release, started)):
        if answer is None:
            unchecked += 1
            answer = {"state": "unknown", "reason": BUDGET_REASON}
        entry = {
            "task_id": int(task["id"]),
            "title": task.get("title") or "",
            "reason": answer.get("reason") or "",
        }
        state = str(answer.get("state") or "unknown")
        # Anything that is not a definite answer lands in unknown by name, not
        # by accident: a state this code does not recognise is exactly the case
        # where guessing would be worst.
        bucket = state if state in (IN_PROD, NOT_IN_PROD) else "unknown"
        buckets[bucket].append(entry)

    # The bound is part of the answer, not a footnote. "50 tasks examined" and
    # "the whole board" are different claims, and only one of them is true.
    checked = len(tasks) - unchecked
    note = (
        f"рассмотрены последние {checked} завершённых задач "
        f"(окно {window}); задачи старше окна в снимок не попали"
    )
    if unchecked:
        note += (
            f". Не проверено {unchecked} из {len(tasks)}: срок сборки снимка "
            "исчерпан, они лежат в «неизвестно»"
        )
    if not release:
        note += (
            ". Успешных выкатов не записано — хаб не знает, что раскатано. "
            "Это незнание, а не «ничего не доехало»"
        )

    # #1420: an open release alert is the first thing the steward must read
    # here — «what runs in production» is a stale answer while the release
    # that would change it stands blocked.
    from hub.services.release_alert import active_release_blocks

    return {
        "release_blocks": await active_release_blocks(db),
        "deployed": deployed,
        "in_prod": buckets[IN_PROD],
        "not_in_prod": buckets[NOT_IN_PROD],
        "unknown": buckets["unknown"],
        "examined": checked,
        "window": window,
        "note": note,
    }


def format_prod_state(data: dict[str, Any]) -> str:
    """Human-readable snapshot — shared by the CLI and the MCP tool (#499).

    One formatter as well as one builder: two renderings of the same facts
    drift, and then two readers disagree about production.
    """
    from hub.services.release_alert import release_block_lines

    deployed = data.get("deployed") or {}
    sha = str(deployed.get("sha") or "")
    lines = release_block_lines(list(data.get("release_blocks") or []))
    if sha:
        where = f" ({deployed.get('ref')})" if deployed.get("ref") else ""
        when = f" от {deployed['at']}" if deployed.get("at") else ""
        lines.append(f"Раскатано: {sha[:12]}{where}{when}")
    else:
        lines.append("Раскатано: неизвестно — успешных выкатов не записано")

    for key, label in (
        ("in_prod", "В проде"),
        ("not_in_prod", "Смёржено, но не раскатано"),
        ("unknown", "Состояние неизвестно"),
    ):
        entries = data.get(key) or []
        lines.append(f"{label}: {len(entries)}")
        for entry in entries[:10]:
            lines.append(f"  #{entry['task_id']} {entry.get('title', '')}".rstrip())
        if len(entries) > 10:
            lines.append(f"  … и ещё {len(entries) - 10}")

    if data.get("note"):
        lines.append(str(data["note"]))
    return "\n".join(lines)
