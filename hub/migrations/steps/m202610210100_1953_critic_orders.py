"""#1953: заказы критика k8 для облачного исполнителя.

``critic_orders`` - отдельная таблица: заказы исполнителя (``executor_orders``) требуют
задачу и код implementer, а критик не получает ни кода, ни сессии хаба. Строка -
один заказ на (задача, sha); закрытие несёт исход и число находок. Частичный
уникальный индекс не даёт двух открытых заказов на один коммит, после
failed/expired заказ на тот же sha допустим. CHECK держит согласованность
сам: закрытый заказ имеет исход, passed - только без находок.
"""

MIGRATIONS = [
    (
        "create_critic_orders",
        """CREATE TABLE IF NOT EXISTS critic_orders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            executor_run_id INTEGER NOT NULL REFERENCES executor_runs(id) ON DELETE CASCADE,
            sha             TEXT    NOT NULL
                            CHECK (length(sha) = 40 AND sha NOT GLOB '*[^0-9a-f]*'),
            repo            TEXT    NOT NULL DEFAULT '',
            branch          TEXT    NOT NULL DEFAULT '',
            base            TEXT    NOT NULL DEFAULT '',
            host            TEXT    NOT NULL CHECK (length(host) BETWEEN 1 AND 100),
            state           TEXT    NOT NULL DEFAULT 'pending'
                            CHECK (state IN ('pending', 'delivered', 'closed')),
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            expires_at      TEXT    NOT NULL,
            outcome         TEXT
                            CHECK (outcome IS NULL
                                   OR outcome IN ('passed', 'findings', 'failed', 'expired')),
            findings_p1     INTEGER NOT NULL DEFAULT 0
                            CHECK (findings_p1 BETWEEN 0 AND 1000),
            findings_p2     INTEGER NOT NULL DEFAULT 0
                            CHECK (findings_p2 BETWEEN 0 AND 1000),
            summary         TEXT    NOT NULL DEFAULT '',
            host_ref        TEXT    NOT NULL DEFAULT '',
            report_digest   TEXT    NOT NULL DEFAULT '',
            reported_at     TEXT,
            closed_reason   TEXT    NOT NULL DEFAULT '',
            CHECK ((state = 'closed') = (outcome IS NOT NULL)),
            CHECK (outcome IS NOT 'passed' OR (findings_p1 = 0 AND findings_p2 = 0))
        )""",
    ),
    (
        "uq_critic_orders_open",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_critic_orders_open "
        "ON critic_orders(task_id, sha) WHERE state <> 'closed'",
    ),
    (
        "idx_critic_orders_task",
        "CREATE INDEX IF NOT EXISTS idx_critic_orders_task "
        "ON critic_orders(task_id, id)",
    ),
    (
        "idx_critic_orders_host_open",
        "CREATE INDEX IF NOT EXISTS idx_critic_orders_host_open "
        "ON critic_orders(host, state) WHERE state <> 'closed'",
    ),
    (
        "idx_critic_orders_expires",
        "CREATE INDEX IF NOT EXISTS idx_critic_orders_expires "
        "ON critic_orders(expires_at) WHERE state <> 'closed'",
    ),
]
