"""#1992: долговечный срок ожидания доставки (base_fetch_pending).

``delivery_wait_deadlines`` - одна строка на ожидание: (задача, поколение сдачи,
ревизия вердикта, вид). ``started_at`` пишется один раз при первом проходе
ожидания и переживает рестарт поллера; текст отказа в ключ не входит, поэтому
его смена срок не продлевает. Состояния: open -> closed (успех, другая причина)
или open -> notified (владельцу ушло уведомление, статус задачи не менялся). Каскад - от задачи.
"""

MIGRATIONS = [
    (
        "create_delivery_wait_deadlines",
        """CREATE TABLE IF NOT EXISTS delivery_wait_deadlines (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id               INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            submission_generation INTEGER NOT NULL,
            verdict_revision      INTEGER NOT NULL DEFAULT 0,
            kind                  TEXT    NOT NULL,
            state                 TEXT    NOT NULL DEFAULT 'open',
            started_at            TEXT    NOT NULL DEFAULT (datetime('now')),
            closed_at             TEXT,
            UNIQUE (task_id, submission_generation, verdict_revision, kind)
        )""",
    ),
]
