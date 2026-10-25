"""#2057: намерение заказать машинное ревью - durable, в одной транзакции со сдачей.

Одна строка на (задача, поколение сдачи). ``pending`` -> ``done`` (заказ лёг,
отказ политики, сдача устарела) | ``lost`` (попытки исчерпаны, один алерт в
ленте). ``claimed_at`` - бронь обработчика: условный UPDATE, одна обработка
на строку; протухшая бронь (рестарт хаба посреди заказа) берётся заново.
``next_attempt_at`` - откат между попытками. Единственный исполнитель заказа -
свип ``review_order.sweep_review_orders``; HTTP-маршрут лишь подталкивает его.
"""

MIGRATIONS = [
    (
        "create_review_order_intents",
        """CREATE TABLE IF NOT EXISTS review_order_intents (
            task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            generation      INTEGER NOT NULL,
            state           TEXT    NOT NULL DEFAULT 'pending'
                            CHECK (state IN ('pending', 'done', 'lost')),
            attempts        INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT    NOT NULL DEFAULT (datetime('now')),
            claimed_at      TEXT,
            last_error      TEXT    NOT NULL DEFAULT '',
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (task_id, generation)
        )""",
    ),
    (
        "index_review_order_intents_pending",
        "CREATE INDEX IF NOT EXISTS idx_review_order_intents_pending "
        "ON review_order_intents(next_attempt_at) WHERE state = 'pending'",
    ),
]
