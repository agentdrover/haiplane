"""#1999: автоповтор доставки принятой (completed) задачи после base_updated.

``accepted_delivery_retries`` - одна строка на (задача, поколение сдачи). Строку
ставит ``deliver_on_disposition``, когда доставка принятой задачи получила
``base_updated``; свип доставки повторяет ту же доставку после зелёного CI.
Лимиты (число попыток, срок от ``armed_at``) живут в строке и не зависят от
ленты событий (retention 14 дней) и от рестарта. Переходы состояния
(armed -> reserved -> armed | closed) - условные UPDATE. Строка живёт, пока
живёт задача: каскад только от неё.
"""

MIGRATIONS = [
    (
        "create_accepted_delivery_retries",
        """CREATE TABLE IF NOT EXISTS accepted_delivery_retries (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id               INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            submission_generation INTEGER NOT NULL,
            pr_number             INTEGER NOT NULL DEFAULT 0,
            project_id            INTEGER,
            via                   TEXT    NOT NULL DEFAULT '',
            state                 TEXT    NOT NULL DEFAULT 'armed',
            attempts              INTEGER NOT NULL DEFAULT 0,
            armed_at              TEXT    NOT NULL DEFAULT (datetime('now')),
            reserved_at           TEXT,
            token                 TEXT    NOT NULL DEFAULT '',
            last_checked_at       TEXT,
            closed_at             TEXT,
            outcome               TEXT    NOT NULL DEFAULT '',
            history               TEXT    NOT NULL DEFAULT '[]',
            UNIQUE (task_id, submission_generation)
        )""",
    ),
    (
        "create_accepted_delivery_retries_open_idx",
        "CREATE INDEX IF NOT EXISTS idx_accepted_delivery_retries_open "
        "ON accepted_delivery_retries(state) WHERE state != 'closed'",
    ),
]
