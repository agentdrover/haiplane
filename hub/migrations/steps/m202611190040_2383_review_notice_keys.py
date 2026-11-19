"""Keep pilot review notice identity past event retention."""

MIGRATIONS = [
    (
        "epic_plan_review_notice_keys_2383",
        """CREATE TABLE IF NOT EXISTS epic_plan_review_notice_keys (
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            generation INTEGER NOT NULL,
            sha TEXT NOT NULL,
            reason TEXT NOT NULL,
            PRIMARY KEY(task_id,generation,sha,reason)
        ) WITHOUT ROWID""",
    ),
]
