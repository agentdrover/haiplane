"""An isolated one-shot executor probe with neither a run nor a redeemable code."""

MIGRATIONS = [
    (
        "create_executor_quota_probe",
        """CREATE TABLE IF NOT EXISTS executor_quota_probe (
            singleton INTEGER PRIMARY KEY CHECK (singleton=1),
            order_id TEXT NOT NULL UNIQUE,
            authority_ref TEXT NOT NULL,
            binding_json TEXT NOT NULL,
            host TEXT NOT NULL CHECK (host='k8') REFERENCES executor_hosts(name),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            before_redeem_at TEXT,
            before_exec_at TEXT CHECK (before_exec_at IS NULL OR before_redeem_at IS NOT NULL),
            recorded_at TEXT CHECK (recorded_at IS NULL OR before_exec_at IS NOT NULL),
            revoked_at TEXT
        )""",
    ),
]
