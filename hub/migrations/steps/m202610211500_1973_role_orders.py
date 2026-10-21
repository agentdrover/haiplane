"""#1973: заказы ролей k8 (role_orders), курсор событий и допуск хоста к ролям.

``role_orders`` - отдельная сущность, не ``executor_orders``: заказ роли не
несёт кода implementer, не создаёт прогонов и привязан к событию, а не к задаче
как к броне слота. ``source_key`` UNIQUE навсегда: одно событие - один заказ за
всю жизнь, повтор после ``done`` его не воскрешает.

``role_orders_cursor`` - единственная строка: последнее событие ленты, которое
свип источника разобрал. Очистка событий не трогает хвост за этим курсором.
Строку заводит первый же свип на текущем конце ленты (``_read_cursor``): старые
``needs_decision`` задним числом роль не вызывают. Пока строки нет, очистка
событий ведёт себя как прежде (так же устроен ``owner_minutes_state``).

``role_host_admissions`` - какой хост к какой роли и проекту допущен. Нет строки -
хост заказа не получает.
"""

MIGRATIONS = [
    (
        "create_role_orders",
        """CREATE TABLE IF NOT EXISTS role_orders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            role            TEXT    NOT NULL CHECK (role IN ('roadmap')),
            source_key      TEXT    NOT NULL UNIQUE,
            source_event_id INTEGER,
            task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            project_id      INTEGER,
            generation      INTEGER NOT NULL DEFAULT 0,
            entered_at      TEXT    NOT NULL DEFAULT '',
            state           TEXT    NOT NULL DEFAULT 'open'
                            CHECK (state IN
                                   ('open', 'leased', 'started', 'done',
                                    'failed', 'expired')),
            attempt         INTEGER NOT NULL DEFAULT 1,
            lease_id        TEXT,
            host            TEXT,
            leased_at       TEXT,
            lease_deadline  TEXT,
            started_at      TEXT,
            closed_at       TEXT,
            outcome         TEXT    NOT NULL DEFAULT '',
            note            TEXT    NOT NULL DEFAULT '',
            report_digest   TEXT    NOT NULL DEFAULT '',
            created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "index_role_orders_state",
        "CREATE INDEX IF NOT EXISTS idx_role_orders_state ON role_orders(state, id)",
    ),
    (
        "index_role_orders_task",
        "CREATE INDEX IF NOT EXISTS idx_role_orders_task ON role_orders(task_id)",
    ),
    (
        "create_role_orders_cursor",
        """CREATE TABLE IF NOT EXISTS role_orders_cursor (
            id              INTEGER PRIMARY KEY CHECK (id = 1),
            cursor_event_id INTEGER NOT NULL
        )""",
    ),
    (
        "create_role_host_admissions",
        """CREATE TABLE IF NOT EXISTS role_host_admissions (
            host       TEXT    NOT NULL REFERENCES executor_hosts(name),
            role       TEXT    NOT NULL CHECK (role IN ('roadmap')),
            project_id INTEGER NOT NULL,
            created_at TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (host, role, project_id)
        )""",
    ),
]
