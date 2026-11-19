"""Durable observations, decision and one read permit for the #2406 pilot."""

MIGRATIONS = [
    (
        "create_roadmap_observation_pilots",
        """CREATE TABLE roadmap_observation_pilots (
            approval_id INTEGER PRIMARY KEY,
            grant_id INTEGER NOT NULL UNIQUE REFERENCES roadmap_grants(id),
            activated_at TEXT NOT NULL DEFAULT (datetime('now')),
            batch_id INTEGER NOT NULL UNIQUE,
            order_id INTEGER UNIQUE REFERENCES role_orders(id),
            decision_reason TEXT,
            decided_at TEXT,
            effect_id TEXT UNIQUE,
            claimed_at TEXT,
            effect_outcome TEXT,
            effect_version TEXT,
            reported_at TEXT
        )""",
    ),
    (
        "create_roadmap_pilot_observations",
        """CREATE TABLE roadmap_pilot_observations (
            pilot_id INTEGER NOT NULL REFERENCES roadmap_observation_pilots(approval_id),
            sequence INTEGER NOT NULL CHECK(sequence IN (1, 2)),
            observation_id TEXT NOT NULL,
            observed_at TEXT NOT NULL DEFAULT (datetime('now')),
            codex_ready INTEGER,
            openai_ready INTEGER,
            repository_read INTEGER,
            PRIMARY KEY (pilot_id, sequence),
            UNIQUE (pilot_id, observation_id)
        )""",
    ),
]
