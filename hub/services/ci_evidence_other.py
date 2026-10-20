"""The brief's mutations and baseline when the current attempt carries none (#1936).

A ``pull_request:synchronize`` run does not count mutations and baseline and
starts after the executor's dispatch run, so it is the current attempt and its
empty blocks used to hide what the dispatch run counted on the same commit. The
brief now shows the block of another verified attempt of the commit
(``ci_trust.borrowed_evidence_block``) with its source named. Only the brief:
the projection and every gate stay on the current attempt.
"""

from __future__ import annotations

import json
from typing import Any

from hub.services.ci_trust import borrowed_evidence_block


def _is_empty(raw: str | None) -> bool:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return True
    return not isinstance(data, dict) or not data


async def block_of_the_commit(
    db: Any, task_id: int, pinned: str, stored: dict, column: str, label: str
):
    """The current attempt's block, or — if it has none — the one another attempt counted.

    A current block of any content wins. ``stored`` is the confirmed projection
    row (empty when there is none: nothing is borrowed then).
    """
    from hub.services.review_evidence import _evidence_block

    raw = stored.get(column)
    if stored and _is_empty(raw):
        found = await borrowed_evidence_block(db, task_id, pinned, column, stored)
        if found is not None:
            return _signed(
                _evidence_block(label, found.raw, pinned),
                found.run_id,
                found.run_attempt,
                column,
            )
    return _evidence_block(label, raw, pinned)


def _signed(block, run_id: str, run_attempt: str, column: str):
    """The block, saying in ``run`` and in ``reason`` which attempt counted it."""
    source = f"из попытки {run_id}/{run_attempt} того же коммита"
    reason = f"{source}: {block.reason}" if block.reason else source
    if column == "baseline":
        reason += " (гейт red-test багов его не учитывает)"
    return block.model_copy(
        update={"run": f"{block.run}; {source} (не текущая попытка)", "reason": reason}
    )
