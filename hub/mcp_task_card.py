"""Compact view of a task for hub_task_status (#1613).

REST stays canonical: it returns the whole TaskView with the whole feed. This
module only decides what an agent reads BY DEFAULT — the newest window of the
feed, excerpts of long fields and a card in structuredContent — and says, for
everything left out, how much was shown, how much exists and where the rest is.
Silence about a cut reads as "everything is here" (#519, #810, #834).

Nothing here talks to the hub: pure functions over the dict REST returned.
"""

from __future__ import annotations

from typing import Any

# Long free-text fields: how much of them the default view keeps.
FIELD_EXCERPT_CHARS = 1500
# One feed entry. A recovery check needs to recognise its own record, not to
# re-read it in full; full=true has the whole text.
UPDATE_EXCERPT_CHARS = 800

# Fields the card carries from the task as they are (REST equality, AC-3).
CARD_FIELDS: tuple[str, ...] = (
    "id",
    "title",
    "status",
    "task_type",
    "work_type",
    "size",
    "source",
    "runtime",
    "assigned_agent",
    "job_id",
    "exit_code",
    "auto_review",
    "review_cycle",
    "submission_generation",
    "submission_sha",
    "branch",
    "pr_number",
    "review_verdict",
    "review_approved_current",
    "latest_review",
    "verdict_route",
    "dependencies",
    "worktree_path",
    "created_at",
    "updated_at",
)

# Lists that the default view reduces to a count.
_COUNTED_LISTS: tuple[str, ...] = ("scope_in", "scope_out", "validation_commands")


def excerpt(text: str, limit: int) -> tuple[str, int]:
    """First ``limit`` characters of ``text`` and the number left out."""
    if len(text) <= limit:
        return text, 0
    return text[:limit].rstrip() + f"… [+{len(text) - limit} chars]", len(text) - limit


def window_updates(updates: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """The NEWEST ``count`` entries, oldest first. -1 is the whole feed, 0 none."""
    if count < 0:
        return list(updates)
    if count == 0:
        return []
    return list(updates[-count:])


def _compact_update(update: dict[str, Any]) -> tuple[dict[str, Any], int]:
    content, omitted = excerpt(str(update.get("content") or ""), UPDATE_EXCERPT_CHARS)
    compact: dict[str, Any] = {
        key: update[key]
        for key in ("id", "created_at", "kind", "agent")
        if key in update
    }
    compact["content"] = content
    return compact, omitted


def _updates_bound(task_id: int, shown: int, total: int) -> dict[str, Any]:
    return {
        "field": "updates",
        "shown": shown,
        "total": total,
        "read_more": (
            f"hub_task_status(task_id={task_id}, updates=-1) or full=true or "
            f"GET /api/tasks/{task_id}/updates"
        ),
        "note": (
            "the newest entries are shown; if your own write is not among them "
            "it may still be in the older part — read updates=-1 BEFORE "
            "repeating a write"
        ),
    }


def _excerpt_bound(field: str, kept: int, total: int) -> dict[str, Any]:
    return {"field": field, "shown": kept, "total": total, "read_more": "full=true"}


def _text_fields(task: dict[str, Any]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Excerpts of the long text fields and a bound for each one cut."""
    shown: dict[str, str] = {}
    bounds: list[dict[str, Any]] = []
    for name in ("description", "technical_hints", "result_text"):
        value = str(task.get(name) or "")
        if not value:
            continue
        cut, omitted = excerpt(value, FIELD_EXCERPT_CHARS)
        shown[name] = cut
        if omitted:
            bounds.append(_excerpt_bound(name, FIELD_EXCERPT_CHARS, len(value)))
    return shown, bounds


def _counted_bounds(task: dict[str, Any]) -> list[dict[str, Any]]:
    bounds = []
    for name in _COUNTED_LISTS:
        total = len(task.get(name) or [])
        if total:
            bounds.append(_excerpt_bound(name, 0, total))
    criteria = len(task.get("acceptance_criteria") or [])
    if criteria:
        bounds.append(
            _excerpt_bound("acceptance_criteria.given_when_then", 0, criteria)
        )
    return bounds


def build_compact_view(task: dict[str, Any], count: int) -> dict[str, Any]:
    """Everything the default view shows, as plain data for both halves.

    Returns ``card`` (the structuredContent task), ``texts`` (field excerpts),
    ``updates`` (the compact window), and ``bounds`` (what was left out).
    """
    task_id = int(task.get("id") or 0)
    all_updates = task.get("updates") or []
    window = window_updates(all_updates, count)
    compact_updates: list[dict[str, Any]] = []
    cut_entries = 0
    for update in window:
        compact, omitted = _compact_update(update)
        compact_updates.append(compact)
        cut_entries += 1 if omitted else 0
    texts, bounds = _text_fields(task)
    if len(window) < len(all_updates):
        bounds.insert(0, _updates_bound(task_id, len(window), len(all_updates)))
    if cut_entries:
        bounds.append(
            {
                "field": "updates.content",
                "shown": UPDATE_EXCERPT_CHARS,
                "total": cut_entries,
                "read_more": "full=true",
                "note": f"{cut_entries} entries cut to {UPDATE_EXCERPT_CHARS} chars",
            }
        )
    bounds.extend(_counted_bounds(task))
    card = {name: task[name] for name in CARD_FIELDS if name in task}
    card["acceptance_criteria_ids"] = [
        ac.get("id") for ac in task.get("acceptance_criteria") or []
    ]
    card["updates"] = compact_updates
    card["updates_total"] = len(all_updates)
    card["compact"] = True
    card["bounds"] = bounds
    return {
        "card": card,
        "texts": texts,
        "updates": compact_updates,
        "bounds": bounds,
    }


def bounds_lines(bounds: list[dict[str, Any]]) -> list[str]:
    """The text half's statement of what was left out — one line per cut."""
    lines = []
    for bound in bounds:
        line = (
            f"[bounded] {bound['field']} {bound['shown']}/{bound['total']}"
            f" — full: {bound['read_more']}"
        )
        if bound.get("note"):
            line += f". {bound['note']}"
        lines.append(line)
    return lines
