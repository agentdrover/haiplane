"""The category_checks catalogue reaches its two readers (#920, feature #908).

A rule is a row of ``category_checks`` (#878): a finding class and the named
check that closes it. Until now nobody read it — the check bought by an
expensive catch did not reach the next review in the same place, and a class
coming back after its rule was not visible anywhere. Two readers here:

* the review brief — :func:`rules_for_areas` picks the rules whose area
  touches the task's ``affected_areas``;
* the repeat report — :func:`rule_breaches` counts confirmed findings of a
  ruled class dated AFTER the rule was set up (``category_checks.created_at``).
  A finding from before is the history the rule was written from, not a breach.

What "the rule's area" is, stated honestly: the catalogue has no area column
and this module does not invent one. The area is where the class was actually
met — the ``affected_areas`` of the tasks whose confirmed machine-review
findings carry the category, the ``file`` those findings name, and the
``affected_areas`` of every prod defect that named the rule on close. It is
derived from records, so a rule nobody ever met anywhere reaches no brief.

The link "rule ↔ the defect that bought it" is the one #919 already stores:
``tasks.defect_prevention`` with ``kind='rule'`` and ``ref`` = the category.
It is read from there rather than copied into the catalogue — one source of
truth. A rule recorded straight from the recurrence debt (#878) has no such
defect, and the brief says so instead of leaving the field blank.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import aiosqlite

from hub.db import fetchall

#: A task ruled by nothing is the common case; the list stays short.
_MAX_TASK_IDS = 10


def _norm(path: str) -> str:
    """A comparable area: no ``./``, no slashes at the ends, no ``:line``."""
    cleaned = (path or "").strip().split(":", 1)[0].strip()
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.strip("/")


def _overlap(a: str, b: str) -> bool:
    """Same path, or one is a directory holding the other."""
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _areas(raw: Any) -> list[str]:
    """``affected_areas`` as stored (JSON text or a list), normalised."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    return [a for a in (_norm(str(x)) for x in raw) if a]


async def _catalogue(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    rows = await fetchall(
        db,
        "SELECT category, check_ref, note, "
        "COALESCE(created_at, recorded_at) AS created_at "
        "FROM category_checks ORDER BY category ASC",
    )
    return [dict(r) for r in rows]


async def _source_defects(
    db: aiosqlite.Connection,
) -> dict[str, list[dict[str, Any]]]:
    """category → the prod defects that named it as their rule (#919)."""
    rows = await fetchall(
        db,
        "SELECT id, title, affected_areas, "
        "json_extract(defect_prevention, '$.ref') AS category FROM tasks "
        "WHERE defect_prevention IS NOT NULL AND json_valid(defect_prevention) "
        "AND json_extract(defect_prevention, '$.kind') = 'rule' ORDER BY id ASC",
    )
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_category[str(row["category"] or "")].append(dict(row))
    return by_category


async def _met_in(
    db: aiosqlite.Connection, categories: list[str]
) -> dict[str, dict[str, Any]]:
    """category → {areas, task_ids} where its confirmed findings landed."""
    met: dict[str, dict[str, Any]] = {
        c: {"areas": set(), "task_ids": set()} for c in categories
    }
    if not categories:
        return met
    marks = ", ".join("?" for _ in categories)
    rows = await fetchall(
        db,
        "SELECT json_extract(f.value, '$.category') AS category, "  # nosec B608 - placeholders only, values stay params
        "mr.task_id AS task_id, "
        "COALESCE(json_extract(f.value, '$.file'), '') AS file, "
        "t.affected_areas AS affected_areas "
        "FROM machine_reviews mr JOIN tasks t ON t.id = mr.task_id "
        "JOIN json_each(mr.findings_confirmed) f "
        f"WHERE json_extract(f.value, '$.category') IN ({marks})",
        tuple(categories),
    )
    for row in rows:
        slot = met[str(row["category"])]
        slot["task_ids"].add(int(row["task_id"]))
        slot["areas"].update(_areas(row["affected_areas"]))
        file = _norm(str(row["file"] or ""))
        if file:
            slot["areas"].add(file)
    return met


def _defect_view(defect: dict[str, Any]) -> dict[str, Any]:
    return {"task_id": int(defect["id"]), "title": str(defect["title"] or "")}


async def rules_for_areas(
    db: aiosqlite.Connection, task_areas: list[str]
) -> list[dict[str, Any]]:
    """Catalogue rules whose area touches ``task_areas``; [] when none do.

    An empty list is the whole answer for "no rules here": the brief renders
    no section for it, never a header over nothing.
    """
    wanted = [a for a in (_norm(x) for x in task_areas) if a]
    if not wanted:
        return []
    catalogue = await _catalogue(db)
    if not catalogue:
        return []
    defects = await _source_defects(db)
    met = await _met_in(db, [r["category"] for r in catalogue])
    rules: list[dict[str, Any]] = []
    for rule in catalogue:
        category = rule["category"]
        sources = defects.get(category, [])
        areas = set(met[category]["areas"])
        for defect in sources:
            areas.update(_areas(defect["affected_areas"]))
        matched = sorted(a for a in wanted if any(_overlap(a, b) for b in areas))
        if not matched:
            continue
        rules.append(
            {
                "category": category,
                "check_ref": rule["check_ref"],
                "note": rule["note"] or "",
                "created_at": rule["created_at"] or "",
                "matched_areas": matched,
                "source_defects": [_defect_view(d) for d in sources],
                "seen_in_tasks": sorted(met[category]["task_ids"])[:_MAX_TASK_IDS],
            }
        )
    return rules


async def rule_breaches(db: aiosqlite.Connection) -> dict[str, Any]:
    """Rules whose class came back after they were set up (#920).

    Not windowed: a rule is judged against its whole life since it was set up,
    and a window would forgive a breach by letting it age out. Carried-over
    report copies (#1361) are skipped — one finding is one breach, not one per
    base-only merge that re-attached the report.
    """
    from hub.services.orchestration import ORIGINAL_READ_SQL

    catalogue = await _catalogue(db)
    rows = await fetchall(
        db,
        "SELECT cc.category AS category, mr.task_id AS task_id, "  # nosec B608 - constant fragment
        "mr.created_at AS created_at "
        "FROM machine_reviews mr JOIN json_each(mr.findings_confirmed) f "
        "JOIN category_checks cc "
        "ON cc.category = json_extract(f.value, '$.category') "
        "WHERE mr.created_at > COALESCE(cc.created_at, cc.recorded_at) "
        f"AND mr.{ORIGINAL_READ_SQL} ORDER BY mr.created_at ASC",
    )
    hits: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        hits[str(row["category"])].append(dict(row))
    defects = await _source_defects(db) if hits else {}
    breached = [
        {
            "category": rule["category"],
            "check_ref": rule["check_ref"],
            "rule_created_at": rule["created_at"] or "",
            "breaches": len(hits[rule["category"]]),
            "task_ids": sorted({h["task_id"] for h in hits[rule["category"]]})[
                :_MAX_TASK_IDS
            ],
            "last_breach_at": hits[rule["category"]][-1]["created_at"],
            "source_defects": [
                _defect_view(d) for d in defects.get(rule["category"], [])
            ],
        }
        for rule in catalogue
        if hits.get(rule["category"])
    ]
    return {"rules_total": len(catalogue), "breached": breached}
