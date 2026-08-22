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

import logging
from typing import Any

from hub import repository as repo
from hub.services.delivery_state import IN_PROD, NOT_IN_PROD, delivery_state

log = logging.getLogger("hub")

DEFAULT_WINDOW = 50
MAX_WINDOW = 200


async def prod_state(db: Any, *, limit: int = DEFAULT_WINDOW) -> dict[str, Any]:
    """A snapshot of production: what is deployed and which tasks are where."""
    window = max(1, min(int(limit or DEFAULT_WINDOW), MAX_WINDOW))

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
    for task in tasks:
        answer = await delivery_state(db, int(task["id"]))
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
    note = (
        f"рассмотрены последние {len(tasks)} завершённых задач "
        f"(окно {window}); задачи старше окна в снимок не попали"
    )
    if not release:
        note += (
            ". Успешных выкатов не записано — хаб не знает, что раскатано. "
            "Это незнание, а не «ничего не доехало»"
        )

    return {
        "deployed": deployed,
        "in_prod": buckets[IN_PROD],
        "not_in_prod": buckets[NOT_IN_PROD],
        "unknown": buckets["unknown"],
        "examined": len(tasks),
        "window": window,
        "note": note,
    }


def format_prod_state(data: dict[str, Any]) -> str:
    """Human-readable snapshot — shared by the CLI and the MCP tool (#499).

    One formatter as well as one builder: two renderings of the same facts
    drift, and then two readers disagree about production.
    """
    deployed = data.get("deployed") or {}
    sha = str(deployed.get("sha") or "")
    lines = []
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
