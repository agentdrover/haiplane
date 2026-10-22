"""#1947: рекомендация «разбудить ВМ на доработку» в таблице разбора (#1942).

``executor_triage_recommendations.action`` ограничен CHECK списком из пяти
действий, а в SQLite ограничение снимается только пересборкой таблицы. Шаги идут
отдельными записями (``_migrate`` исполняет по одному оператору), транзакция у
них одна: упадёт любой - откат вернёт старую таблицу со строками. Первым шагом
идёт уборка копии, чтобы повтор после сбоя начинал с чистого места.

Новая таблица - та же плюс ``rework_on_host`` в CHECK и две колонки записи
рекомендации: ``verdict_revision`` (идентичность вердикта latest_review на момент
рекомендации) и ``findings_count`` (число его находок; NULL = не прочиталось, а не
ноль). Строки переносятся вместе с id, счётчик AUTOINCREMENT - тоже.
"""

MIGRATIONS: list[tuple[str, str]] = [
    (
        "rework_on_host_clear",
        "DROP TABLE IF EXISTS executor_triage_recommendations_v2",
    ),
    (
        "rework_on_host_create",
        """CREATE TABLE executor_triage_recommendations_v2 (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id         INTEGER NOT NULL UNIQUE
                             REFERENCES executor_orders(id) ON DELETE CASCADE,
            task_id          INTEGER NOT NULL
                             REFERENCES tasks(id) ON DELETE CASCADE,
            triage_class     INTEGER NOT NULL,
            class_name       TEXT    NOT NULL DEFAULT '',
            action           TEXT    NOT NULL CHECK (action IN (
                                 'retry_after_status', 'refine_hints_and_retry',
                                 'retry_once', 'rewrite_or_split', 'to_owner',
                                 'rework_on_host')),
            reason           TEXT    NOT NULL DEFAULT '',
            created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            owner_verdict    TEXT    CHECK (owner_verdict IS NULL
                                 OR owner_verdict IN ('accepted', 'rejected')),
            owner_verdict_at TEXT,
            followup         TEXT    CHECK (followup IS NULL OR followup IN (
                                 'new_order', 'statement_changed',
                                 'submitted', 'none_72h', 'unclear')),
            followup_at      TEXT,
            resolved_by      TEXT    NOT NULL DEFAULT '',
            child_floor      INTEGER NOT NULL DEFAULT 0,
            verdict_revision INTEGER,
            findings_count   INTEGER
        )""",
    ),
    (
        "rework_on_host_copy",
        "INSERT INTO executor_triage_recommendations_v2 (id, order_id, task_id, "
        "triage_class, class_name, action, reason, created_at, owner_verdict, "
        "owner_verdict_at, followup, followup_at, resolved_by, child_floor) "
        "SELECT id, order_id, task_id, triage_class, class_name, action, reason, "
        "created_at, owner_verdict, owner_verdict_at, followup, followup_at, "
        "resolved_by, child_floor FROM executor_triage_recommendations",
    ),
    (
        "rework_on_host_sequence",
        "UPDATE sqlite_sequence SET seq = MAX(seq, COALESCE((SELECT s.seq "
        "FROM sqlite_sequence s WHERE s.name = 'executor_triage_recommendations'), "
        "0)) WHERE name = 'executor_triage_recommendations_v2'",
    ),
    (
        "rework_on_host_drop",
        "DROP TABLE executor_triage_recommendations",
    ),
    (
        "rework_on_host_rename",
        "ALTER TABLE executor_triage_recommendations_v2 "
        "RENAME TO executor_triage_recommendations",
    ),
    (
        "rework_on_host_index",
        "CREATE INDEX IF NOT EXISTS idx_executor_triage_recommendations_task "
        "ON executor_triage_recommendations(task_id, created_at)",
    ),
]
