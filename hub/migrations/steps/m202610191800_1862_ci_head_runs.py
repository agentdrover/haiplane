"""#1862: журнал запусков CI на голове ветки задачи до сдачи и журнал объявлений итога.

``ci_head_runs`` - одна строка на пару (задача, sha): когда хаб впервые увидел
голову, сколько раз за неё просили прогон и чем кончилось. ``seen_seq`` - порядок
последнего наблюдения (у текущей головы задачи он наибольший, и при возврате
A->B->A снова у A). Уникальный ключ
пары и есть гарантия «не больше одного запроса на коммит» между циклами,
рестартами и соединениями. ``ci_head_announced`` - одна строка на (задача, sha,
попытка): по ней итог прогона попадает в ленту один раз. ``ci_head_state`` -
сохраняемый курсор очереди объявлений.
"""

MIGRATIONS = [
    (
        "create_ci_head_runs",
        """CREATE TABLE IF NOT EXISTS ci_head_runs (
            task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            sha           TEXT    NOT NULL,
            branch        TEXT    NOT NULL DEFAULT '',
            state         TEXT    NOT NULL DEFAULT 'seen',
            attempts      INTEGER NOT NULL DEFAULT 0,
            seen_seq      INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT    NOT NULL DEFAULT (datetime('now')),
            reserved_at   TEXT,
            note          TEXT    NOT NULL DEFAULT '',
            PRIMARY KEY (task_id, sha)
        )""",
    ),
    (
        "create_ci_head_announced",
        """CREATE TABLE IF NOT EXISTS ci_head_announced (
            task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            sha          TEXT    NOT NULL,
            attempt_key  TEXT    NOT NULL,
            announced_at TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (task_id, sha, attempt_key)
        )""",
    ),
    (
        "create_ci_head_state",
        """CREATE TABLE IF NOT EXISTS ci_head_state (
            key   TEXT    PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        )""",
    ),
]
