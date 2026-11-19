"""Owner grants and explicit roadmap requests (#2397)."""

MIGRATIONS = [
    (
        "seed_roadmap_steward_role",
        "INSERT INTO roles (slug, name, description, system) "
        "VALUES ('roadmap_steward', 'Project roadmap steward', "
        "'Can read a specific roadmap grant and request advice under it', 1)",
    ),
    (
        "create_roadmap_grants",
        """CREATE TABLE roadmap_grants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id),
            plan_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            approval_id INTEGER NOT NULL,
            plan_generation INTEGER NOT NULL,
            task_generation INTEGER NOT NULL,
            steward_id INTEGER NOT NULL REFERENCES principals(id),
            roadmap_id INTEGER NOT NULL REFERENCES principals(id),
            signal_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT
        )""",
    ),
    (
        "index_active_roadmap_grant",
        """CREATE UNIQUE INDEX idx_roadmap_grants_active
           ON roadmap_grants(task_id) WHERE revoked_at IS NULL""",
    ),
    (
        "create_roadmap_requests",
        """CREATE TABLE roadmap_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            grant_id INTEGER NOT NULL REFERENCES roadmap_grants(id) ON DELETE CASCADE,
            request_id TEXT NOT NULL,
            question TEXT NOT NULL,
            source_order_id INTEGER,
            order_id INTEGER REFERENCES role_orders(id) ON DELETE CASCADE,
            UNIQUE(grant_id, request_id),
            UNIQUE(grant_id, source_order_id)
        )""",
    ),
    (
        "index_roadmap_order_request",
        "CREATE UNIQUE INDEX idx_roadmap_order_request ON roadmap_requests(order_id)",
    ),
]
