"""A durable one-shot receipt for local execution of an approved roadmap mandate."""

MIGRATIONS = [
    (
        "create_roadmap_activation_receipts",
        """CREATE TABLE IF NOT EXISTS roadmap_activation_receipts (
            approval_id INTEGER PRIMARY KEY,
            grant_id INTEGER NOT NULL UNIQUE,
            approver_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            statement_fingerprint TEXT NOT NULL,
            executor_high_water INTEGER NOT NULL,
            executor_order_id INTEGER UNIQUE,
            activated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
]
