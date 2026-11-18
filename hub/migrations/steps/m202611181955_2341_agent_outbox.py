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
            created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
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
    (
        "agent_outbox_wait_principal_2341",
        """CREATE INDEX IF NOT EXISTS agent_outbox_wait_principal
        ON agent_outbox(principal_id) WHERE kind='question' AND invalidated_reason=''""",
    ),
    (
        "agent_outbox_invalidate_principal_2341",
        """CREATE TRIGGER IF NOT EXISTS agent_outbox_invalidate_principal
        AFTER UPDATE OF status ON principals WHEN OLD.status='active' AND NEW.status<>'active'
        BEGIN
            UPDATE agent_outbox SET invalidated_reason='principal_inactive'
            WHERE (principal_id=NEW.id OR owner_id=NEW.id)
              AND kind='question' AND invalidated_reason='';
        END""",
    ),
    (
        "agent_outbox_invalidate_host_2341",
        """CREATE TRIGGER IF NOT EXISTS agent_outbox_invalidate_host
        AFTER UPDATE OF enabled ON executor_hosts WHEN OLD.enabled AND NOT NEW.enabled
        BEGIN
            UPDATE agent_outbox SET invalidated_reason='host_disabled'
            WHERE principal_id=NEW.principal_id AND host=NEW.name
              AND kind='question' AND invalidated_reason='';
        END""",
    ),
    (
        "agent_outbox_invalidate_archive_2341",
        """CREATE TRIGGER IF NOT EXISTS agent_outbox_invalidate_archive
        AFTER UPDATE OF archived ON tasks WHEN NOT OLD.archived AND NEW.archived
        BEGIN
            UPDATE agent_outbox SET invalidated_reason='task_unavailable'
            WHERE task_id=NEW.id AND kind='question' AND invalidated_reason='';
        END""",
    ),
    (
        "agent_outbox_invalidate_project_2341",
        """CREATE TRIGGER IF NOT EXISTS agent_outbox_invalidate_project
        AFTER UPDATE OF status ON projects WHEN OLD.status='active' AND NEW.status<>'active'
        BEGIN
            UPDATE agent_outbox SET invalidated_reason='project_unavailable'
            WHERE project_id=NEW.id AND kind='question' AND invalidated_reason='';
        END""",
    ),
    (
        "agent_outbox_invalidate_task_binding_2341",
        """CREATE TRIGGER IF NOT EXISTS agent_outbox_invalidate_task_binding
        AFTER UPDATE OF project_id, parent_id ON tasks
        WHEN OLD.project_id IS NOT NEW.project_id OR OLD.parent_id IS NOT NEW.parent_id
        BEGIN
            UPDATE agent_outbox SET invalidated_reason='task_project_changed'
            WHERE kind='question' AND invalidated_reason='' AND task_id IN (
                WITH RECURSIVE affected_task(id) AS (
                    SELECT NEW.id
                    UNION
                    SELECT tasks.id FROM tasks JOIN affected_task ON tasks.parent_id=affected_task.id
                )
                SELECT id FROM affected_task
            );
        END""",
    ),
]
