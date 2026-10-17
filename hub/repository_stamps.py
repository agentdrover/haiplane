"""SQL fragments that stamp ``tasks.started_at`` (#1790).

The moment a task FIRST entered ``running`` is written by the same primitives
that write ``status`` (``repository.create_task``, ``create_task_full``,
``update_task``, ``transition_status_if``, ``record_state_decision``), inside
the same statement. No entry point (pair-start, dispatch, CI-fix, return from
review, an answer, a state verdict) can forget it, and a lost race writes
nothing.

``start_history_unknown`` rows were in work before the stamp existed: their
start cannot be known, and today's date must not stand in for it (#810). They
are never stamped. Both fragments bind ONE placeholder, to the new status.
"""

from __future__ import annotations

#: Value for the ``started_at`` column of an INSERT.
STARTED_AT_ON_INSERT_SQL = "CASE WHEN ? = 'running' THEN datetime('now') END"

#: ``SET`` clause of an UPDATE: the first entry into ``running``, never again.
STARTED_AT_ON_RUNNING_SQL = (
    "started_at = CASE WHEN ? = 'running' AND started_at IS NULL "
    "AND start_history_unknown = 0 THEN datetime('now') ELSE started_at END"
)
