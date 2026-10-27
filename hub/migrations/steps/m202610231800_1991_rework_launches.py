"""#1991: бронь запуска доработки по решению «на доработку».

``rework_launches`` - одна строка на решение rework по задаче в needs_decision.
Строка в состоянии ``reserved`` и есть потреблённый вход: пока она есть, никакое
другое решение по задаче (rework, accept, прогон слияния) не проходит. Гарантия
держится на частичном UNIQUE по ``task_id``: два соединения не могут завести две
активные брони, как бы ни разошлись их проверки. Сетевой заказ идёт уже после
коммита брони, а ``job_id`` и состояние ``launched``/``declined`` пишутся вместе.

Состояния: ``reserved`` -> ``launched`` (задание принято, job_id записан) |
``declined`` (диспетчер задания не дал, задача ушла в open) | ``needs_human``
(исход заказа установить нельзя, повторного запуска нет) | ``closed`` (задача
ушла из needs_decision другим путём). Строка живёт, пока живёт задача.

``nonce`` - случайный токен брони: уходит только в текст её заказа, и по нему
сверка отличает своё задание от чужого, цитирующего метку. ``boot_id`` - процесс
хаба, завёдший бронь: сверка трогает только брони прежнего процесса.
"""

MIGRATIONS = [
    (
        "create_rework_launches",
        """CREATE TABLE IF NOT EXISTS rework_launches (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            state       TEXT    NOT NULL DEFAULT 'reserved'
                        CHECK (state IN ('reserved', 'launched', 'declined',
                                         'needs_human', 'closed')),
            job_id      TEXT    NOT NULL DEFAULT '',
            reason      TEXT    NOT NULL DEFAULT '',
            reserved_at TEXT    NOT NULL DEFAULT (datetime('now')),
            resolved_at TEXT
        )""",
    ),
    (
        "index_rework_launches_one_reserved_per_task",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_rework_launches_one_reserved "
        "ON rework_launches(task_id) WHERE state = 'reserved'",
    ),
    (
        "add_rework_launches_nonce",
        "ALTER TABLE rework_launches ADD COLUMN nonce TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_rework_launches_boot_id",
        "ALTER TABLE rework_launches ADD COLUMN boot_id TEXT NOT NULL DEFAULT ''",
    ),
]
