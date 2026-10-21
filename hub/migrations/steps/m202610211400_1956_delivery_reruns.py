"""#1956: бронь автоперезапуска упавших job CI на свежей базе.

``delivery_reruns`` - одна строка на (задача, поколение сдачи). ``armed`` ставит
гейт в момент слияния базы в ветку (при политике ``delivery_fresh_base_rerun=on``),
поэтому признак «базу обновил гейт» и расход лимита не зависят от ленты событий
(она чистится через 14 дней). Уникальный ключ и условные UPDATE состояния
(armed -> reserved -> requested -> closed) и есть гарантия «не больше одного POST
на поколение» между циклами, рестартами и соединениями. Строка живёт, пока живёт
задача: каскад только от неё.
"""

MIGRATIONS = [
    (
        "create_delivery_reruns",
        """CREATE TABLE IF NOT EXISTS delivery_reruns (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id               INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            submission_generation INTEGER NOT NULL,
            pr_number             INTEGER NOT NULL DEFAULT 0,
            base_from_sha         TEXT    NOT NULL DEFAULT '',
            state                 TEXT    NOT NULL DEFAULT 'armed',
            repo                  TEXT    NOT NULL DEFAULT '',
            workflow              TEXT    NOT NULL DEFAULT '',
            head_sha              TEXT    NOT NULL DEFAULT '',
            run_id                TEXT    NOT NULL DEFAULT '',
            attempt_before        INTEGER NOT NULL DEFAULT 0,
            armed_at              TEXT    NOT NULL DEFAULT (datetime('now')),
            requested_at          TEXT,
            closed_at             TEXT,
            outcome               TEXT    NOT NULL DEFAULT '',
            failed_jobs           TEXT    NOT NULL DEFAULT '[]',
            history               TEXT    NOT NULL DEFAULT '[]',
            UNIQUE (task_id, submission_generation)
        )""",
    ),
]
