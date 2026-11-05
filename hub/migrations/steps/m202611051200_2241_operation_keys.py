MIGRATIONS = [
    (
        "2241_operation_keys_0",
        """CREATE TABLE IF NOT EXISTS operation_keys (
    principal_ref   TEXT NOT NULL,
    operation_id    TEXT NOT NULL,
    operation       TEXT NOT NULL,
    args_hash       TEXT NOT NULL,
    args_hash_version INTEGER NOT NULL,
    scope_ref       TEXT,
    actor_ref       TEXT,
    status          TEXT NOT NULL DEFAULT 'completed'
                    CHECK (status IN ('completed','expired')),
    response_status INTEGER,
    response_body   TEXT,
    response_headers TEXT,
    response_bytes  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT NOT NULL,
    key_expires_at  TEXT NOT NULL,
    PRIMARY KEY (principal_ref, operation_id),
    CHECK (
        (status='completed' AND response_status IS NOT NULL)
        OR (status='expired' AND response_body IS NULL)
    )
);""",
    ),
    (
        "2241_operation_keys_1",
        """CREATE INDEX IF NOT EXISTS idx_operation_keys_expiry ON operation_keys(key_expires_at);""",
    ),
    (
        "2241_operation_keys_2",
        """CREATE INDEX IF NOT EXISTS idx_operation_keys_result_expiry ON operation_keys(expires_at) WHERE status='completed';""",
    ),
    (
        "2241_operation_keys_3",
        """CREATE INDEX IF NOT EXISTS idx_operation_keys_id ON operation_keys(operation_id);""",
    ),
    (
        "2241_operation_aliases_0",
        """CREATE TABLE IF NOT EXISTS operation_aliases (
    principal_ref TEXT NOT NULL,
    alias         TEXT NOT NULL,
    operation_id  TEXT NOT NULL,
    operation     TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (principal_ref, alias),
    FOREIGN KEY (principal_ref, operation_id)
        REFERENCES operation_keys(principal_ref, operation_id) ON DELETE CASCADE
);""",
    ),
    (
        "2241_effect_outbox_0",
        """CREATE TABLE IF NOT EXISTS effect_outbox (
    effect_id     TEXT PRIMARY KEY,
    operation_id  TEXT,
    principal_ref TEXT,
    effect_kind   TEXT NOT NULL,
    effect_key    TEXT NOT NULL,
    ordering_key  TEXT NOT NULL DEFAULT '',
    payload_json  TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'pending'
                  CHECK (state IN ('pending','running','done','failed','unknown','dead')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_at       TEXT NOT NULL DEFAULT (datetime('now')),
    lease_owner   TEXT,
    lease_until   TEXT,
    last_error    TEXT,
    resolution    TEXT,
    resolved_at   TEXT,
    resolved_by   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT,
    UNIQUE (effect_kind, effect_key)
);""",
    ),
    (
        "2241_effect_outbox_1",
        """CREATE INDEX IF NOT EXISTS idx_effect_outbox_ready ON effect_outbox(state, next_at);""",
    ),
    (
        "2241_effect_outbox_2",
        """CREATE INDEX IF NOT EXISTS idx_effect_outbox_order ON effect_outbox(ordering_key, created_at);""",
    ),
    (
        "2241_effect_outbox_3",
        """CREATE INDEX IF NOT EXISTS idx_effect_outbox_operation ON effect_outbox(operation_id);""",
    ),
]
