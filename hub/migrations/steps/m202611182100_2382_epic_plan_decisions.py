"""Append-only plan versions with a single human activation and terminal state."""

MIGRATIONS = [
    (
        "epic_plan_decisions_2382",
        """CREATE TABLE IF NOT EXISTS epic_plan_decisions (
            id INTEGER PRIMARY KEY,
            epic_id INTEGER NOT NULL,
            version TEXT NOT NULL,
            snapshot TEXT NOT NULL,
            approved_principal_id INTEGER NOT NULL,
            approved_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            terminal_state TEXT CHECK(terminal_state IN ('stopped', 'revoked')),
            terminal_principal_id INTEGER,
            terminal_at TEXT,
            terminal_reason TEXT,
            UNIQUE(epic_id, version)
        )""",
    ),
]
