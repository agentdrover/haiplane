"""An operator may attest one recovery order for a failed explicit request."""

MIGRATIONS = [
    (
        "create_roadmap_recoveries",
        """CREATE TABLE IF NOT EXISTS roadmap_recoveries (
            request_pk INTEGER PRIMARY KEY REFERENCES roadmap_requests(id) ON DELETE CASCADE,
            order_id INTEGER NOT NULL UNIQUE REFERENCES role_orders(id) DEFERRABLE INITIALLY DEFERRED,
            operator_id INTEGER NOT NULL REFERENCES principals(id),
            evidence_update_id INTEGER NOT NULL,
            evidence_sha256 TEXT NOT NULL,
            runtime_sha256 TEXT NOT NULL
        )""",
    ),
]
