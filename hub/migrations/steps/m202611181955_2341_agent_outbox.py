"""Durable acceptance, delivery and answer state for a role notification."""

MIGRATIONS = [
    (
        "agent_outbox_2341",
        """CREATE TABLE IF NOT EXISTS agent_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            principal_id INTEGER NOT NULL,
            host TEXT NOT NULL,
            project_id INTEGER NOT NULL,
            project_slug TEXT NOT NULL,
            role TEXT NOT NULL,
            task_id INTEGER NOT NULL,
            order_id INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            run INTEGER NOT NULL,
            continuation TEXT,
            delivery_key TEXT NOT NULL,
            digest TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('result','question','silent')),
            body TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending','sent','unknown','failed')),
            reason TEXT NOT NULL DEFAULT '',
            invalidated_reason TEXT NOT NULL DEFAULT '',
            owner_id INTEGER,
            message_id INTEGER UNIQUE,
            answer_message_id INTEGER UNIQUE,
            answer_event_id INTEGER UNIQUE,
            answer_digest TEXT,
            answer TEXT,
            answered_at INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            lease TEXT,
            lease_until INTEGER NOT NULL DEFAULT 0,
            next_attempt INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT (unixepoch()),
            UNIQUE (principal_id, project_id, delivery_key),
            UNIQUE (order_id, run, continuation)
        )""",
    ),
    (
        "agent_outbox_intake_2341",
        "CREATE INDEX IF NOT EXISTS agent_outbox_intake ON agent_outbox(principal_id, created_at)",
    ),
    (
        "agent_outbox_pending_2341",
        """CREATE INDEX IF NOT EXISTS agent_outbox_due
        ON agent_outbox(next_attempt, lease_until, id) WHERE state='pending'""",
    ),
    (
        "agent_outbox_wait_admission_2341",
        """CREATE INDEX IF NOT EXISTS agent_outbox_wait_admission
        ON agent_outbox(host,role,project_id) WHERE kind='question' AND invalidated_reason=''""",
    ),
    (
        "agent_outbox_wait_owner_2341",
        """CREATE INDEX IF NOT EXISTS agent_outbox_wait_owner
        ON agent_outbox(task_id) WHERE kind='question' AND invalidated_reason=''""",
    ),
    (
        "agent_outbox_invalidate_admission_2341",
        """CREATE TRIGGER IF NOT EXISTS agent_outbox_invalidate_admission
        AFTER DELETE ON role_host_admissions
        BEGIN
            UPDATE agent_outbox SET invalidated_reason='admission_revoked'
            WHERE host=OLD.host AND role=OLD.role AND project_id=OLD.project_id
              AND kind='question' AND invalidated_reason='';
        END""",
    ),
    (
        "agent_outbox_invalidate_owner_2341",
        """CREATE TRIGGER IF NOT EXISTS agent_outbox_invalidate_owner
        AFTER UPDATE OF human_owner ON tasks WHEN OLD.human_owner IS NOT NEW.human_owner
        BEGIN
            UPDATE agent_outbox SET invalidated_reason='owner_changed'
            WHERE task_id=NEW.id AND kind='question' AND invalidated_reason='';
        END""",
    ),
]
