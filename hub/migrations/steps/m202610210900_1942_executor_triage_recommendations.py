"""#1942: рекомендация разбора неудачного прогона k8 (тень, без запуска).

* ``executor_orders.statement_generation`` - неизменяемое поколение постановки
  задачи на момент выпуска заказа. NULL = заказ выпущен до колонки: поколение
  неизвестно, и серия по нему не считается (рекомендация «к владельцу»).
  ``executor_orders.generation`` - поколение СДАЧИ, это другое число.
* ``executor_triage_recommendations`` - одна строка на заказ. UNIQUE(order_id)
  держит «одна рекомендация на заказ» при гонке, рестарте и после очистки
  событий (они живут 14 дней). ``owner_verdict`` пишет кнопка #1943,
  ``followup`` - свип хаба; ``child_floor`` - наибольший id подзадачи на момент
  рекомендации (подзадача человека с большим id = «разделение»). ``unclear`` -
  действие без долговечной метки времени (архив, правка без заказа, агентский
  ребёнок): вне знаменателя замера; совпадение считается по ним в docs/agent-context.
"""

MIGRATIONS: list[tuple[str, str]] = [
    (
        "add_executor_orders_statement_generation",
        "ALTER TABLE executor_orders ADD COLUMN statement_generation INTEGER",
    ),
    (
        "create_executor_triage_recommendations",
        """CREATE TABLE IF NOT EXISTS executor_triage_recommendations (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id         INTEGER NOT NULL UNIQUE
                             REFERENCES executor_orders(id) ON DELETE CASCADE,
            task_id          INTEGER NOT NULL
                             REFERENCES tasks(id) ON DELETE CASCADE,
            triage_class     INTEGER NOT NULL,
            class_name       TEXT    NOT NULL DEFAULT '',
            action           TEXT    NOT NULL CHECK (action IN (
                                 'retry_after_status', 'refine_hints_and_retry',
                                 'retry_once', 'rewrite_or_split', 'to_owner')),
            reason           TEXT    NOT NULL DEFAULT '',
            created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            owner_verdict    TEXT    CHECK (owner_verdict IS NULL
                                 OR owner_verdict IN ('accepted', 'rejected')),
            owner_verdict_at TEXT,
            followup         TEXT    CHECK (followup IS NULL OR followup IN (
                                 'new_order', 'statement_changed', 'split',
                                 'submitted', 'none_72h', 'unclear')),
            followup_at      TEXT,
            resolved_by      TEXT    NOT NULL DEFAULT '',
            child_floor      INTEGER NOT NULL DEFAULT 0
        )""",
    ),
    (
        # Момент включения разбора: одна строка (как owner_minutes_state). Заказы,
        # закрытые до него, - история и рекомендации не получают.
        "create_executor_triage_state",
        """CREATE TABLE IF NOT EXISTS executor_triage_state (
            id           INTEGER PRIMARY KEY CHECK (id = 1),
            activated_at TEXT    NOT NULL
        )""",
    ),
    (
        "idx_executor_triage_recommendations_task",
        "CREATE INDEX IF NOT EXISTS idx_executor_triage_recommendations_task "
        "ON executor_triage_recommendations(task_id, created_at)",
    ),
]
