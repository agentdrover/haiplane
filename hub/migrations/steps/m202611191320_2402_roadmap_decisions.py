"""Keep roadmap evidence and the authenticated steward decision separate."""

MIGRATIONS = [
    (
        "roadmap_grant_decision_checks",
        "ALTER TABLE roadmap_grants ADD COLUMN decision_checks_json TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "create_roadmap_results",
        """CREATE TABLE IF NOT EXISTS roadmap_results (
            order_id INTEGER PRIMARY KEY REFERENCES role_orders(id) ON DELETE CASCADE,
            request_pk INTEGER NOT NULL UNIQUE REFERENCES roadmap_requests(id) ON DELETE CASCADE,
            grant_id INTEGER NOT NULL REFERENCES roadmap_grants(id) ON DELETE CASCADE,
            context_json TEXT NOT NULL,
            result_json TEXT NOT NULL
        )""",
    ),
    (
        "index_roadmap_results_grant",
        "CREATE INDEX IF NOT EXISTS idx_roadmap_results_grant ON roadmap_results(grant_id, order_id)",
    ),
    (
        "create_roadmap_technical_decisions",
        """CREATE TABLE IF NOT EXISTS roadmap_technical_decisions (
            order_id INTEGER PRIMARY KEY REFERENCES roadmap_results(order_id) ON DELETE CASCADE,
            steward_id INTEGER NOT NULL REFERENCES principals(id),
            status TEXT NOT NULL CHECK(status IN ('accepted','owner_required','blocked')),
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
]
