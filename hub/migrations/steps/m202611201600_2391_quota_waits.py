"""Persist subscription quota waits across dispatcher restarts."""

MIGRATIONS = [
    (
        "epic_plan_quota_waits_2391",
        """CREATE TABLE IF NOT EXISTS epic_plan_quota_waits (
            binding_key TEXT PRIMARY KEY,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            reset_at TEXT
        ) WITHOUT ROWID""",
    ),
]
