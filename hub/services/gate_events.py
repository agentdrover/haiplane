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
    }
)

# Actors that never count as a human click. Same set for override-rate and
# for touches-per-delivered — otherwise the two metrics disagree about who
# the human was.
NON_HUMAN_GATE_ACTORS: frozenset[str] = frozenset({"hub", "policy", "steward"})

DISPOSITION_RECORDED = "disposition_recorded"


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
