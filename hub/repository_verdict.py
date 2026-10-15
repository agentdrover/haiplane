"""SQL of the review verdict write (#1706).

Beside ``hub.repository`` and not in it: that file is at its size ceiling
(docs/agent-context/file-size-budget.json). ``repository.record_review_verdict``
stays the name every caller uses and delegates here.

One write, one conditional UPDATE. Every condition that decides whether a
verdict may land is IN the statement: nothing read before it can be stale by
the time it runs (#1601). That is what makes the refusal safe against a
concurrent writer, where a check in Python followed by an unconditional write
(the shape that let a stale APPROVED replace CHANGES_REQUESTED on #1699) is not.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

#: A verdict stands when it was written for the generation that is live now.
_STANDING = (
    "(COALESCE(review_verdict,'')!='' "
    "AND review_verdict_generation=submission_generation)"
)


def _guard(
    expected_generation: int | None,
    supersedes_revision: int | None,
    claim: tuple[int, str] | None,
) -> tuple[str, list[Any]]:
    """The WHERE tail and its parameters for one verdict write."""
    guard = ""
    params: list[Any] = []
    if expected_generation is not None:
        guard += " AND submission_generation=?"
        params.append(expected_generation)
    if supersedes_revision is None:
        # First write on this generation, and nothing else.
        guard += f" AND NOT {_STANDING}"
    else:
        # Replacement: the revision the replacer saw must still be the live
        # one, and a window closed by a human decision is not reopened (#1286).
        guard += (
            " AND review_verdict_revision=? AND (NOT "
            f"{_STANDING} OR review_verdict_closed_generation IS NOT "
            "submission_generation)"
        )
        params.append(supersedes_revision)
    if claim is not None:
        # #1601: the steward's application writes only while ITS mark is intact.
        guard += (
            " AND EXISTS (SELECT 1 FROM steward_judgements sj WHERE sj.id=? "
            "AND sj.advisor_outcome='applying' AND sj.advisor_claimed_at=?)"
        )
        params.extend([claim[0], claim[1]])
    return guard, params


async def write_review_verdict(
    db: aiosqlite.Connection,
    task_id: int,
    verdict: str,
    *,
    findings_json: str = "[]",
    self_approved: bool = False,
    expected_generation: int | None = None,
    claim: tuple[int, str] | None = None,
    supersedes_revision: int | None = None,
    digest: str = "",
) -> bool:
    """Persist a verdict bound to the live generation; True when it was written.

    Without ``supersedes_revision`` the write lands only when no verdict stands
    on the live generation. With it, only when that revision is still the live
    one and the approval window was not closed. ``expected_generation``, when
    given, must equal the live generation. The revision grows on every write,
    for every writer, because the statement is the only place that writes it.
    Findings, the self-approved flag and the closed-window mark belong to the
    verdict and are overwritten with it (#308, #434, #1286).
    """
    guard, guard_params = _guard(expected_generation, supersedes_revision, claim)
    cursor = await db.execute(
        "UPDATE tasks SET review_verdict=?, "  # nosec B608 - guard is built from constants
        "review_verdict_generation=submission_generation, "
        "review_findings=?, review_self_approved=?, "
        "review_verdict_closed_generation=NULL, "
        "review_verdict_revision=review_verdict_revision+1, "
        "review_verdict_digest=?, updated_at=datetime('now') WHERE id=?" + guard,
        (
            verdict,
            findings_json,
            1 if self_approved else 0,
            digest,
            task_id,
            *guard_params,
        ),
    )
    return (cursor.rowcount or 0) > 0
