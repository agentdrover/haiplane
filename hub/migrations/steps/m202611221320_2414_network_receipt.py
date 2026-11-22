"""One approved network receipt, separate from the model executor queue."""

MIGRATIONS = [
    (
        "create_network_receipt_mandates",
        """CREATE TABLE IF NOT EXISTS network_receipt_mandates (
            approval_id INTEGER PRIMARY KEY,
            runner_id INTEGER REFERENCES principals(id),
            ready_key_id INTEGER, profile_sha TEXT, ready_at TEXT, revoked_at TEXT
        )""",
    ),
    (
        "create_network_receipt_orders",
        """CREATE TABLE IF NOT EXISTS network_receipt_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            approval_id INTEGER NOT NULL UNIQUE REFERENCES network_receipt_mandates(approval_id),
            task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id),
            receipt_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            head_sha TEXT, measured_at TEXT, commit_sha TEXT, published_at TEXT,
            submitted_generation INTEGER, steward_key_id INTEGER
        )""",
    ),
]
