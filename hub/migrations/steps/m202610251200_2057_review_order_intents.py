"""#2057: намерение заказать машинное ревью - durable, в одной транзакции со сдачей.

Одна строка на (задача, поколение сдачи). Состояния:

* ``pending`` - платного вызова провайдеру не было; заказать может свип/подталкивание;
* ``ordering`` - перед POST записаны метка и владелец (условный UPDATE, commit);
  автоматически НЕ повторяется никогда: исход неизвестен, пока не записана
  строка заказа;
* ``done`` - строка заказа есть, либо сдача устарела, либо отказ политики;
* ``unknown`` - заказ мог уйти к провайдеру, а строки нет; один алерт, дальше
  решает человек или осознанная пересдача;
* ``failed`` - до POST не дошли за ``MAX_ATTEMPTS`` попыток; один алерт.

``claimed_by`` - токен владельца обработки (uuid на обработку): переход в
``ordering`` и запись исхода ограждены им, потерянное владение = никакого POST.
``ordering_marker`` - метка заказа провайдера, привязанная к ЭТОМУ намерению.
"""

MIGRATIONS = [
    (
        "create_review_order_intents",
        """CREATE TABLE IF NOT EXISTS review_order_intents (
            task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            generation      INTEGER NOT NULL,
            state           TEXT    NOT NULL DEFAULT 'pending'
                            CHECK (state IN ('pending', 'ordering', 'done',
                                             'unknown', 'failed')),
            attempts        INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT    NOT NULL DEFAULT (datetime('now')),
            claimed_at      TEXT,
            claimed_by      TEXT    NOT NULL DEFAULT '',
            ordering_marker TEXT    NOT NULL DEFAULT '',
            ordering_at     TEXT,
            last_error      TEXT    NOT NULL DEFAULT '',
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (task_id, generation)
        )""",
    ),
    (
        "index_review_order_intents_open",
        "CREATE INDEX IF NOT EXISTS idx_review_order_intents_open "
        "ON review_order_intents(next_attempt_at) "
        "WHERE state IN ('pending', 'ordering')",
    ),
]
