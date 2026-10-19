"""#1874: журнал начислений минут владельца и состояние его обработчика.

Начисление - оценка по нормативу на одно решение человека на гейте; строит его
поллер из события гейта. ``source_event_id`` UNIQUE и без внешнего ключа: повтор
паса не даёт дубля, а удаление задачи или очистка events журнал не трогает.
Состояние - одна строка (``id = 1``): когда учёт начался и до какого события
``events.id`` пас уже дочитал. Строку создаёт сам пас (однократная
инициализация: курсор = MAX(events.id)), миграция её не сеет.
"""

MIGRATIONS = [
    (
        "create_owner_minute_accruals",
        """CREATE TABLE IF NOT EXISTS owner_minute_accruals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id         INTEGER NOT NULL,
            principal_id    INTEGER NOT NULL,
            kind            TEXT NOT NULL,
            occurred_at     TEXT NOT NULL,
            seconds         INTEGER NOT NULL,
            norms_version   INTEGER NOT NULL,
            source_event_id INTEGER NOT NULL UNIQUE,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "idx_owner_minute_accruals_task",
        "CREATE INDEX IF NOT EXISTS idx_owner_minute_accruals_task "
        "ON owner_minute_accruals(task_id)",
    ),
    (
        "idx_owner_minute_accruals_principal_day",
        "CREATE INDEX IF NOT EXISTS idx_owner_minute_accruals_principal_day "
        "ON owner_minute_accruals(principal_id, occurred_at)",
    ),
    (
        "create_owner_minutes_state",
        """CREATE TABLE IF NOT EXISTS owner_minutes_state (
            id              INTEGER PRIMARY KEY CHECK (id = 1),
            tracking_since  TEXT NOT NULL,
            cursor_event_id INTEGER NOT NULL
        )""",
    ),
]
