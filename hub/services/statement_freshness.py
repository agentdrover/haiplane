"""What was delivered in these areas since the statement was written (#615).

A statement can be invalidated by later work, and nothing noticed. Four cases in
one day (10.08.2026): #471 was done by the delivery gate #605 a month later under
a different name; #461 became impossible because GitHub now demands a paid plan;
#493/#494 were satisfied by pipeline_merges rather than the "releases" table they
asked for; and #546 itself rested on "production has no working copy", which #602
had already made false. Shelf life is not measured in months either — two
statements written an hour earlier carried wrong numbers.

Until now the only defence was one agent's habit of re-reading the premises
before starting. #572 rejected exactly that argument for verdicts: discipline
worked until it failed three times. The difference here is only the price of the
mistake — "unreviewed code merged" there, "unnecessary work done" here.

Three rules this module follows:

1. It never blocks. Rot is not an error, it is the world moving on; a check that
   stops work gets traded away the first time it is inconvenient — the lesson the
   dependency audit taught in #611.
2. Silence is not freshness. "No overlap" and "could not compare" are different
   answers and both are said out loud, because an absent warning reads as
   confirmation (#506, #546).
3. Age is reported, never scored. Moving a number without a new fact is the
   broken-signal class fixed in #610.
"""

from __future__ import annotations

import logging
from typing import Any

from hub.db import deserialize_str_list

log = logging.getLogger("hub")

STATE_DELIVERIES = "deliveries_since"
STATE_NO_OVERLAP = "no_overlap"
STATE_NOT_CHECKED = "not_checked"

# Enough to see the pattern without burying the reader. A cap that hid the rest
# in silence would be its own defect, so the payload says how many were dropped.
_MAX_LISTED = 10

# The comparison is by DECLARED areas, not by what the commits actually touched.
# Said in the payload itself: a reader who mistakes this for a diff comparison
# would trust it more than it deserves. The stronger version (changed files from
# the merge commit — the clone exists since #602, the SHA since #534) is a
# separate step.
DECLARED_AREAS_NOTE = (
    "сверка по ЗАЯВЛЕННЫМ областям (affected_areas), не по фактическим диффам"
)
# pipeline_merges is filled by the delivery gate, which exists since #605.
# Anything delivered by hand before that is invisible here, and pretending
# otherwise would overstate the check.
GATE_HISTORY_NOTE = (
    "учитываются доставки гейтом (#605 и позже); ручные мержи до него не видны"
)


def comparable_timestamp(raw: str) -> str:
    """Normalise a timestamp to the form this table compares in TEXT (#616).

    ``created_at`` and ``pipeline_merges.merged_at`` come from SQLite's
    ``datetime('now')`` — ``2026-08-10 11:36:27`` — but
    ``hub_prepare_developer_task`` writes ``prepared_at`` as
    ``2026-08-10T11:36:27+00:00``. The comparison is a string comparison, and
    ``T`` (0x54) sorts above a space (0x20), so an ISO date silently pushed
    every same-day delivery outside the window: the check answered "nothing
    landed" when something had. Found on production the hour #615 shipped, on
    eight rows that already carry the ISO form.

    Both forms are UTC, so only the text differs — drop the ``T`` and any
    offset or fractional part and the two become comparable again.
    """
    ts = (raw or "").strip()
    if not ts:
        return ""
    ts = ts.replace("T", " ")
    for cut in ("+", "Z"):
        head = ts.split(cut, 1)[0]
        if head != ts:
            ts = head
    return ts.split(".", 1)[0].strip()


def statement_written_at(task: dict[str, Any]) -> tuple[str, str]:
    """(timestamp, what it means) for the statement's date.

    ``prepared_at`` is when the statement was last SHAPED — by an analyst
    through ``hub_prepare_developer_task``, or by any refine that wrote a
    statement field (#616). It is not "who prepared it": ``prepared_by`` stays
    untouched by refine, because a refine caller is not necessarily an analyst,
    so a date without an author is expected here rather than a defect.

    Tasks that never went through preparation still have a date worth comparing
    — their creation — and falling back keeps them inside the check instead of
    dropping them out of it silently.
    """
    prepared = comparable_timestamp(task.get("prepared_at") or "")
    if prepared:
        return prepared, "постановка подготовлена"
    created = comparable_timestamp(task.get("created_at") or "")
    return created, "задача создана (аналитика не проводилась)"


async def statement_freshness(db: Any, task: dict[str, Any]) -> dict:
    """Deliveries in the same areas since this statement was written (#615).

    Returns a dict with ``state``, ``written_at``, ``deliveries``, ``reason``
    and the two honesty notes. Never raises: a failure to compare answers
    ``not_checked`` with the cause, because this must not be able to stop a
    task from starting.
    """
    task_id = task["id"]
    written_at, date_meaning = statement_written_at(task)
    areas = {
        a.strip() for a in deserialize_str_list(task.get("affected_areas")) if a.strip()
    }

    base = {
        "written_at": written_at,
        "date_meaning": date_meaning,
        "areas": sorted(areas),
        "deliveries": [],
        "omitted": 0,
        "declared_areas_note": DECLARED_AREAS_NOTE,
        "gate_history_note": GATE_HISTORY_NOTE,
    }

    if not written_at:
        return {
            **base,
            "state": STATE_NOT_CHECKED,
            "reason": "у задачи нет ни даты подготовки, ни даты создания",
        }
    if not areas:
        return {
            **base,
            "state": STATE_NOT_CHECKED,
            "reason": (
                "у задачи не заявлены affected_areas — сравнивать не с чем. "
                "Это не значит, что посылки свежие: значит, что проверка не проводилась"
            ),
        }

    try:
        rows = await db.execute_fetchall(
            "SELECT m.task_id, m.merged_at, t.title, t.affected_areas "
            "FROM pipeline_merges m JOIN tasks t ON t.id = m.task_id "
            "WHERE m.task_id IS NOT NULL AND m.task_id != ? AND m.merged_at > ? "
            "ORDER BY m.merged_at DESC",
            (task_id, written_at),
        )
    except Exception as exc:  # noqa: BLE001 - degradation is the contract
        log.warning(
            "statement freshness for #%s could not query merges: %s", task_id, exc
        )
        return {
            **base,
            "state": STATE_NOT_CHECKED,
            "reason": f"не удалось прочитать историю доставок: {exc}",
        }

    hits: list[dict] = []
    for row in rows:
        d = dict(row)
        other = {
            a.strip()
            for a in deserialize_str_list(d.get("affected_areas"))
            if a.strip()
        }
        shared = sorted(areas & other)
        if not shared:
            continue
        hits.append(
            {
                "task_id": d["task_id"],
                "title": (d.get("title") or "")[:80],
                "merged_at": d.get("merged_at") or "",
                "shared_areas": shared,
            }
        )

    if not hits:
        return {
            **base,
            "state": STATE_NO_OVERLAP,
            "reason": (
                "с даты постановки в этих областях ничего не доставлялось — "
                "посылки, скорее всего, ещё в силе"
            ),
        }

    listed, omitted = hits[:_MAX_LISTED], max(0, len(hits) - _MAX_LISTED)
    return {
        **base,
        "state": STATE_DELIVERIES,
        "deliveries": listed,
        "omitted": omitted,
        "reason": (
            f"с даты постановки в тех же областях доставлено задач: {len(hits)}. "
            "Перечитайте посылки перед работой — они могли быть отменены"
        ),
    }


def render_freshness(freshness: dict | None) -> str:
    """One human-readable block for the pair-start message (#615).

    Rendering lives next to the computation so the MCP layer only prints what
    the server decided — putting the logic in the tool would leave CLI and REST
    clients blind, the "mechanism right, path not wired" class that showed up
    ten times in two weeks.
    """
    if not freshness:
        return ""
    state = freshness.get("state")
    written = freshness.get("written_at") or "?"
    meaning = freshness.get("date_meaning") or ""
    head = f"Свежесть постановки: {written} ({meaning})."
    if state == STATE_NOT_CHECKED:
        return f"{head} Сверка НЕ проводилась: {freshness.get('reason', '—')}."
    if state == STATE_NO_OVERLAP:
        return (
            f"{head} {freshness.get('reason', '')}. "
            f"Оговорка: {freshness.get('declared_areas_note', '')}."
        )
    lines = [f"{head} {freshness.get('reason', '')}:"]
    for d in freshness.get("deliveries") or []:
        areas = ", ".join(d.get("shared_areas") or [])
        lines.append(
            f"  - #{d['task_id']} «{d.get('title', '')}» "
            f"({d.get('merged_at', '')[:10]}; общие области: {areas})"
        )
    omitted = freshness.get("omitted") or 0
    if omitted:
        lines.append(f"  ... и ещё {omitted} — показаны только последние {_MAX_LISTED}")
    lines.append(
        f"Оговорки: {freshness.get('declared_areas_note', '')}; "
        f"{freshness.get('gate_history_note', '')}."
    )
    return "\n".join(lines)
