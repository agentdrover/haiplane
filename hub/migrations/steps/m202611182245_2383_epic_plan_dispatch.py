"""Durable pilot dispatch and bounded spending records."""

MIGRATIONS = [
    (
        "epic_plan_work_2383",
        """CREATE TABLE IF NOT EXISTS epic_plan_work (
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL,
            plan_version TEXT NOT NULL,
            phase TEXT NOT NULL CHECK(phase IN ('executor','review')),
            generation INTEGER NOT NULL,
            sha TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK(attempt > 0),
            host TEXT NOT NULL,
            model TEXT NOT NULL,
            principal_id INTEGER NOT NULL,
            actor_principal_id INTEGER NOT NULL,
            authority_source TEXT NOT NULL,
            authority_ref TEXT NOT NULL UNIQUE,
            grant_expires_at TEXT NOT NULL,
            send_count INTEGER NOT NULL DEFAULT 1 CHECK(send_count BETWEEN 1 AND 2),
            ceiling_cents INTEGER NOT NULL CHECK(ceiling_cents > 0),
            charged_cents INTEGER CHECK(charged_cents >= 0),
            repair_causes TEXT NOT NULL DEFAULT '[]',
            executor_order_id INTEGER UNIQUE,
            review_dispatch_id INTEGER UNIQUE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(task_id, phase, generation, attempt)
        )""",
    ),
    (
        "epic_plan_escalations_2383",
        """CREATE TABLE IF NOT EXISTS epic_plan_escalations (
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL,
            plan_version TEXT NOT NULL,
            generation INTEGER NOT NULL,
            sha TEXT NOT NULL,
            reason TEXT NOT NULL,
            resume_status TEXT NOT NULL CHECK(resume_status IN ('open','running','review')),
            event_id INTEGER,
            message_id INTEGER,
            answer_event_id INTEGER,
            UNIQUE(task_id,plan_version,generation,sha,reason)
        )""",
    ),
]
