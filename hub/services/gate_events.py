"""Human-gate events: one vocabulary, one actor filter (#1009).

The touch metric (touches per delivered task) and ``_human_gate_metrics``
must not each keep their own copy of the kind list. A new gate that is
added in one query and forgotten in the other is how a denominator starts
measuring a different practice than the numerator (#518).

``steward`` is named as a non-human actor here even though steward events
themselves arrive in #1023: the filter is the contract, the events are the
payload. Splitting them would be a second list.
"""

from __future__ import annotations

HUMAN_GATE_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "task_approved",
        "task_rejected",
        "review_verdict_recorded",
        "task_decided",
        "audit_result",
        "disposition_recorded",
        "steward_judgement",
        "steward_applied",
        "steward_escalated",
        "steward_judgement_refused",
    }
)

# Actors that never count as a human click. Same set for override-rate and
# for touches-per-delivered — otherwise the two metrics disagree about who
# the human was.
NON_HUMAN_GATE_ACTORS: frozenset[str] = frozenset({"hub", "policy", "steward"})


def is_reserved_actor_name(name: str | None) -> bool:
    """Имя автора, которым внешний вердикт не может назваться.

    Нормализация та же, что даёт actor события (strip); регистр не важен.
    Запрет ставят маршруты внешнего входа, не модель и не lifecycle: внутренние
    пути пишут вердикт под этими именами по праву (#2147).
    """
    return (name or "").strip().lower() in NON_HUMAN_GATE_ACTORS


def reserved_actor_detail(name: str | None) -> dict[str, str]:
    """Тело 422 reserved_actor_name для маршрутов внешнего входа."""
    return {
        "reason": "reserved_actor_name",
        "message": "reserved_actor_name: agent "
        f"{(name or '').strip()!r} is reserved for hub automation",
        "hint": "Use your own name in 'agent' (hub, policy and steward are "
        "reserved for the hub's automatic verdicts); nothing was recorded.",
    }


DISPOSITION_RECORDED = "disposition_recorded"
STEWARD_JUDGEMENT = "steward_judgement"
STEWARD_APPLIED = "steward_applied"
STEWARD_ESCALATED = "steward_escalated"
#: #1924: судья прислал approve/возврат без оснований или уверенности — 422.
STEWARD_JUDGEMENT_REFUSED = "steward_judgement_refused"
STEWARD_ROUTE_REFUSED = "steward_route_refused"
WATCHER_ROUTE_REFUSED = "watcher_route_refused"
STEWARD_OVERRIDE_WINDOW_DAYS = 7


def sql_in(values: frozenset[str]) -> tuple[str, tuple[str, ...]]:
    """Placeholders and a stable bind order for a closed IN-list.

    The values are a module constant, never request input; callers still
    bind them as parameters so the SQL stays a shape, not a string of
    literals that a second query can drift from.
    """
    ordered = tuple(sorted(values))
    if not ordered:
        raise ValueError("an empty IN list is invalid SQL")
    return ",".join("?" * len(ordered)), ordered
