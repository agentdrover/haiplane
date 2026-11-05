"""#2220: долг по спецификации, когда PR слит мимо проверенного снимка.

Строка - один id снимка (пусто, если в снимке id нет: долг на весь проект),
слитый PR, одобренный head_sha и слитый head. Закрытие долга - действие
человека (#2219): до него ``closed_at`` пуст, и индекс не подхватывает блок
этого id. Уникальность по задаче, PR и id делает повторное наблюдение того же
слитого PR безвредным.
"""

MIGRATIONS = [
    (
        "create_spec_archive_debt",
        """CREATE TABLE IF NOT EXISTS spec_archive_debt (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id     INTEGER NOT NULL,
            task_id        INTEGER NOT NULL,
            requirement_id TEXT NOT NULL,
            pr_number      INTEGER NOT NULL,
            approved_sha   TEXT NOT NULL,
            merged_sha     TEXT NOT NULL,
            reason         TEXT NOT NULL,
            created_at     TEXT NOT NULL DEFAULT (datetime('now')),
            closed_at      TEXT
        )""",
    ),
    (
        "create_spec_archive_debt_unique",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_spec_archive_debt_pr_req "
        "ON spec_archive_debt (task_id, pr_number, requirement_id)",
    ),
]
