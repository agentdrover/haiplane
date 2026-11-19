"""Persist the current answer that is waiting for its executor claim to clear."""

MIGRATIONS = [
    (
        "create_executor_answer_reopens",
        """CREATE TABLE IF NOT EXISTS executor_answer_reopens (
            task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            answer_update_id INTEGER NOT NULL REFERENCES task_updates(id) ON DELETE CASCADE,
            claim_fingerprint TEXT NOT NULL
        )""",
    ),
]
