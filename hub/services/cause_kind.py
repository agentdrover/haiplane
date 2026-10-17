"""The kind of cause of a defect agrees with its causal link (#1791).

``tasks.cause_kind`` names what the defect was; ``tasks.caused_by_task_id``
names the delivery that broke it. The two are one fact seen twice, so the pair
is judged as a pair, on what is stored AFTER every write of the refine that
touched either half, inside that refine's savepoint:

* ``regression`` needs a link: a regression that no change caused is not one;
* any other kind refuses a link: a latent design flaw, a dependency, an ops
  incident or a non-defect has no delivery to blame;
* no kind at all is always allowed, link or not. A row with a link and no kind
  is the history from before the field; it is not called a regression, and an
  unrelated PATCH of it (a date, a stage) is not judged at all.

Refine is the only writer of both halves (``refine``, ``refine-bulk`` and the
prod-defect intake go through ``refinement.refine_task``), so judging there
covers every input; a refusal is a ``DefectPassportError`` and the savepoint
takes the whole refine back, a bulk request with it.
"""

from __future__ import annotations

import aiosqlite

from hub.db import fetchall

CAUSE_KINDS = "regression, latent_design, dependency, ops, not_a_defect, unknown"


async def pair_problem(db: aiosqlite.Connection, task_id: int) -> str:
    """Why the stored (cause_kind, caused_by_task_id) pair disagrees, or ""."""
    rows = await fetchall(
        db, "SELECT cause_kind, caused_by_task_id FROM tasks WHERE id = ?", (task_id,)
    )
    if not rows:
        return ""
    kind, link = rows[0]["cause_kind"], rows[0]["caused_by_task_id"]
    if kind == "regression" and link is None:
        return (
            "cause_kind='regression' требует caused_by_task_id: назовите задачу, "
            "чья поставка сломала (--caused-by), или выберите другой вид причины "
            f"({CAUSE_KINDS})"
        )
    if kind and kind != "regression" and link is not None:
        return (
            f"cause_kind={kind!r} не совместим с caused_by_task_id=#{link}: "
            "ссылка на задачу-причину есть только у regression. Снимите ссылку "
            "(clear_caused_by) или поставьте cause_kind='regression'"
        )
    return ""
