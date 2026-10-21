"""#1972: состояние заказа ``waiting_memory`` и его колонки.

SQLite не меняет CHECK на месте, поэтому таблица перестраивается: новая рядом,
перенос строк, замена, индексы. Колонки ``waiting_since`` / ``last_seen_at`` /
``need_mib`` / ``free_mib`` пишет только ``memory_wait`` (фаза хоста до выкупа).

Раннер (``hub.db._migrate``) исполняет операторы по одному на соединении с
``isolation_level=IMMEDIATE``: DDL транзакцию не закрывает, записи в
``_migrations`` лежат в той же транзакции, а единственный commit - в конце
прогона, поэтому сбой посередине откатывает шаги вместе с отметками. Первый
оператор прогона может выполниться до открытия транзакции, поэтому операторы
терпят повтор (``IF NOT EXISTS`` / ``OR IGNORE`` / ``IF EXISTS``); RENAME повтор
не терпит и на него не рассчитан: после отката он выполняется на чистой таблице.
"""

MIGRATIONS = [
    (
        "create_executor_orders_mw_1972",
        """CREATE TABLE IF NOT EXISTS executor_orders_mw (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            host            TEXT    NOT NULL REFERENCES executor_hosts(name),
            task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            generation      INTEGER NOT NULL,
            executor_run_id INTEGER NOT NULL
                            REFERENCES executor_runs(id) ON DELETE CASCADE,
            pair_code_id    INTEGER,
            code_ciphertext TEXT    NOT NULL DEFAULT '',
            order_text      TEXT    NOT NULL DEFAULT '',
            state           TEXT    NOT NULL DEFAULT 'pending'
                            CHECK (state IN
                                   ('pending', 'delivered', 'waiting_memory',
                                    'redeemed', 'closed')),
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            expires_at      TEXT    NOT NULL,
            delivered_at    TEXT,
            redeemed_at     TEXT,
            closed_at       TEXT,
            close_reason    TEXT    NOT NULL DEFAULT '',
            ack_result      TEXT    NOT NULL DEFAULT '',
            acked_at        TEXT,
            report_outcome  TEXT    NOT NULL DEFAULT '',
            report_sha      TEXT    NOT NULL DEFAULT '',
            report_branch   TEXT    NOT NULL DEFAULT '',
            report_note     TEXT    NOT NULL DEFAULT '',
            reported_at     TEXT,
            code_hash       TEXT    NOT NULL DEFAULT '',
            first_redeemed_at TEXT,
            retry_until     TEXT,
            session_id      INTEGER,
            redeem_rotations INTEGER NOT NULL DEFAULT 0,
            phase           TEXT    NOT NULL DEFAULT '',
            phase_at        TEXT,
            report_reason_code TEXT NOT NULL DEFAULT '',
            report_reason   TEXT    NOT NULL DEFAULT '',
            final_entry_at  TEXT,
            run_seconds     INTEGER NOT NULL DEFAULT 7200,
            waiting_since   TEXT,
            last_seen_at    TEXT,
            need_mib        INTEGER,
            free_mib        INTEGER,
            statement_generation INTEGER
        )""",
    ),
    (
        "copy_executor_orders_to_mw_1972",
        "INSERT OR IGNORE INTO executor_orders_mw (id, host, task_id, generation, "
        "executor_run_id, pair_code_id, code_ciphertext, order_text, state, "
        "created_at, expires_at, delivered_at, redeemed_at, closed_at, close_reason, "
        "ack_result, acked_at, report_outcome, report_sha, report_branch, "
        "report_note, reported_at, code_hash, first_redeemed_at, retry_until, "
        "session_id, redeem_rotations, phase, phase_at, report_reason_code, "
        "report_reason, final_entry_at, run_seconds, statement_generation) "
        "SELECT id, host, task_id, generation, executor_run_id, pair_code_id, "
        "code_ciphertext, order_text, state, created_at, expires_at, delivered_at, "
        "redeemed_at, closed_at, close_reason, ack_result, acked_at, report_outcome, "
        "report_sha, report_branch, report_note, reported_at, code_hash, "
        "first_redeemed_at, retry_until, session_id, redeem_rotations, phase, "
        "phase_at, report_reason_code, report_reason, final_entry_at, run_seconds, "
        "statement_generation "
        "FROM executor_orders",
    ),
    (
        # AUTOINCREMENT не должен вернуть номер, уже выданный и позже снятый.
        "keep_executor_orders_sequence_1972",
        "UPDATE sqlite_sequence SET seq = max(seq, COALESCE((SELECT seq FROM "
        "sqlite_sequence WHERE name = 'executor_orders'), 0)) "
        "WHERE name = 'executor_orders_mw'",
    ),
    (
        # #1942: рекомендации ссылаются на заказ с ON DELETE CASCADE, а DROP TABLE
        # при включённых внешних ключах удалил бы их вместе со старой таблицей.
        # Копия живёт до восстановления после RENAME.
        "backup_executor_triage_recommendations_1972",
        "CREATE TABLE IF NOT EXISTS _executor_triage_recommendations_bak_1972 AS "
        "SELECT * FROM executor_triage_recommendations",
    ),
    (
        "drop_old_executor_orders_1972",
        "DROP TABLE IF EXISTS executor_orders",
    ),
    (
        "rename_executor_orders_mw_1972",
        "ALTER TABLE executor_orders_mw RENAME TO executor_orders",
    ),
    (
        "idx_executor_orders_one_open_per_host_1972",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_executor_orders_one_open_per_host "
        "ON executor_orders(host) WHERE state <> 'closed'",
    ),
    (
        "idx_executor_orders_task_1972",
        "CREATE INDEX IF NOT EXISTS idx_executor_orders_task "
        "ON executor_orders(task_id)",
    ),
    (
        "restore_executor_triage_recommendations_1972",
        "INSERT OR IGNORE INTO executor_triage_recommendations "
        "SELECT * FROM _executor_triage_recommendations_bak_1972",
    ),
    (
        "drop_executor_triage_recommendations_bak_1972",
        "DROP TABLE IF EXISTS _executor_triage_recommendations_bak_1972",
    ),
]
