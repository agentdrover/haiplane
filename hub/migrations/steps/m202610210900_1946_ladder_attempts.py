"""#1946: долговечная запись попытки добора и привязка отчёта к заказу.

``ladder_attempts`` - одна строка на попытку лестницы (#879) или второй оси
(#1243): после какого неполного отчёта (``after_review_id``), каким заказом
(``dispatch_id``) и чем кончилась (``outcome``: пусто - открыта, ``placed``,
``refused``). Строки НЕ чистит retention событий (14 дней): связь «попытка ->
заказ -> исход» раньше жила только в ленте событий.

``machine_reviews.dispatch_id`` - заказ, которому отвечает отчёт. Проставляется
при приёме по коду сессии ревьюера: ``chat_pair_codes.dispatch_id`` ->
``chat_pair_sessions.dispatch_id``. NULL - отчёт без привязки (история, ключ API,
открытый режим): сопоставление с заказом по прежнему порядку ступеней.
"""

MIGRATIONS = [
    (
        "create_ladder_attempts",
        """CREATE TABLE IF NOT EXISTS ladder_attempts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            generation      INTEGER NOT NULL,
            kind            TEXT    NOT NULL,
            after_review_id INTEGER,
            dispatch_id     INTEGER,
            outcome         TEXT    NOT NULL DEFAULT '',
            reason          TEXT    NOT NULL DEFAULT '',
            model           TEXT    NOT NULL DEFAULT '',
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            closed_at       TEXT
        )""",
    ),
    (
        "idx_ladder_attempts_task_generation",
        "CREATE INDEX IF NOT EXISTS idx_ladder_attempts_task_generation "
        "ON ladder_attempts(task_id, generation)",
    ),
    (
        "add_machine_reviews_dispatch_id",
        "ALTER TABLE machine_reviews ADD COLUMN dispatch_id INTEGER",
    ),
    (
        "add_chat_pair_codes_dispatch_id",
        "ALTER TABLE chat_pair_codes ADD COLUMN dispatch_id INTEGER",
    ),
    (
        "add_chat_pair_sessions_dispatch_id",
        "ALTER TABLE chat_pair_sessions ADD COLUMN dispatch_id INTEGER",
    ),
]
