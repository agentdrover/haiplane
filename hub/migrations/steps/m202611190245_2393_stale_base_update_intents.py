"""Persist the exact branch-update lease through a lost forge response."""

MIGRATIONS = [
    (
        "stale_base_update_intents_2393",
        """CREATE TABLE IF NOT EXISTS stale_base_update_intents (
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            generation INTEGER NOT NULL,
            pr_number INTEGER NOT NULL,
            from_sha TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            head_ref TEXT NOT NULL,
            base_ref TEXT NOT NULL,
            confirmed INTEGER NOT NULL DEFAULT 0 CHECK (confirmed IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(task_id, generation, pr_number, from_sha)
        )""",
    ),
]
