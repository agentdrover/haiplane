"""#2091: журнал «проверка снята, результат неизвестен».

Только добавляет таблицы. Не новый ``TaskStatus`` и не ответ: ``tasks`` и
``outcome_answers`` не меняются, ничего не снимается само.

``outcome_retirements`` - решение владельца по одному обещанию: причина,
identity, время, batch (токен предпросмотра), cutoff и снимок контекста
(``context_snapshot`` + ``context_hash``: метрика, completed_at, первая
поставка фикса). Отмена - пометка ``cancelled_*``; строка остаётся в истории.
Уникальность только среди неотменённых: повторное снятие после отмены - новая
строка, а двойное снятие одного и того же - ошибка базы, не дубль.

``outcome_retirement_previews`` - сохранённый предпросмотр: применение берёт
из него когорту и отпечатки, а не доверяет клиенту. Токен применяется один
раз: ``applied_at``/``applied_result`` хранят итог, повтор возвращает его и
ничего не пишет (иначе запоздавший повтор переиграл бы последующую отмену).
"""

MIGRATIONS = [
    (
        "create_outcome_retirements",
        """CREATE TABLE IF NOT EXISTS outcome_retirements (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id          INTEGER NOT NULL,
            batch_id         TEXT    NOT NULL,
            reason           TEXT    NOT NULL,
            retired_by       TEXT    NOT NULL DEFAULT '',
            principal_id     INTEGER,
            retired_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            cutoff           TEXT    NOT NULL DEFAULT '',
            context_snapshot TEXT    NOT NULL DEFAULT '{}',
            context_hash     TEXT    NOT NULL DEFAULT '',
            cancelled_at     TEXT,
            cancelled_by     TEXT    NOT NULL DEFAULT '',
            cancel_reason    TEXT    NOT NULL DEFAULT ''
        )""",
    ),
    (
        "idx_outcome_retirements_active_task",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_outcome_retirements_active_task "
        "ON outcome_retirements(task_id) WHERE cancelled_at IS NULL",
    ),
    (
        "idx_outcome_retirements_batch",
        "CREATE INDEX IF NOT EXISTS idx_outcome_retirements_batch "
        "ON outcome_retirements(batch_id)",
    ),
    (
        "create_outcome_retirement_previews",
        """CREATE TABLE IF NOT EXISTS outcome_retirement_previews (
            token      TEXT PRIMARY KEY,
            cutoff     TEXT NOT NULL,
            projects   TEXT NOT NULL DEFAULT '[]',
            entries    TEXT NOT NULL DEFAULT '[]',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            applied_at TEXT,
            applied_result TEXT
        )""",
    ),
]
