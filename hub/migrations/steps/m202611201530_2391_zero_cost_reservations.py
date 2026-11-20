"""Allow an explicitly zero additional charge on a subscription order."""

MIGRATIONS = [
    (
        "epic_zero_create_2391",
        """CREATE TABLE epic_plan_work_2391 (
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
            ceiling_cents INTEGER NOT NULL CHECK(ceiling_cents >= 0),
            last_charge_check TEXT,
            charged_cents INTEGER CHECK(charged_cents >= 0),
            repair_causes TEXT NOT NULL DEFAULT '[]',
            executor_order_id INTEGER UNIQUE,
            review_dispatch_id INTEGER UNIQUE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(task_id, phase, generation, attempt)
        )""",
    ),
    (
        "epic_zero_copy_2391",
        """INSERT INTO epic_plan_work_2391 (
            id, task_id, plan_version, phase, generation, sha, attempt, host, model,
            principal_id, actor_principal_id, authority_source, authority_ref,
            grant_expires_at, send_count, ceiling_cents, last_charge_check,
            charged_cents, repair_causes, executor_order_id, review_dispatch_id, created_at
        )
       SELECT id, task_id, plan_version, phase, generation, sha, attempt, host, model,
            principal_id, actor_principal_id, authority_source, authority_ref,
            grant_expires_at, send_count, ceiling_cents, last_charge_check,
            charged_cents, repair_causes, executor_order_id, review_dispatch_id, created_at
        FROM epic_plan_work""",
    ),
    ("epic_zero_drop_2391", "DROP TABLE epic_plan_work"),
    (
        "epic_zero_restore_2391",
        "ALTER TABLE epic_plan_work_2391 RENAME TO epic_plan_work",
    ),
]
