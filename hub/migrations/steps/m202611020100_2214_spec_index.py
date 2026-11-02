"""#2214: индекс поведенческой спецификации проекта.

Источник - файлы ``specs/<capability>/spec.md`` на ветке по умолчанию. Индекс
хранит копию, с которой можно сверить git: ревизию файла (SHA blob) и ревизию
каждого требования (sha256 канонического JSON блока). ``spec_requirements``
держит ``(project_id, req_id)`` первичным ключом: id уникален в проекте.
``scenarios`` - канонический JSON списка сценариев, тот самый, что хешируется.
Внешних ключей нет намеренно: индекс перестраивается целиком, а не правится
построчно. ``spec_index_state`` - одна строка на проект: от какого коммита и
когда индекс последний раз построен успешно.
"""

MIGRATIONS = [
    (
        "create_spec_index_state",
        """CREATE TABLE IF NOT EXISTS spec_index_state (
            project_id INTEGER PRIMARY KEY,
            ref        TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            indexed_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "create_spec_files",
        """CREATE TABLE IF NOT EXISTS spec_files (
            project_id INTEGER NOT NULL,
            path       TEXT NOT NULL,
            capability TEXT NOT NULL,
            revision   TEXT NOT NULL,
            PRIMARY KEY (project_id, path)
        )""",
    ),
    (
        "create_spec_requirements",
        """CREATE TABLE IF NOT EXISTS spec_requirements (
            project_id INTEGER NOT NULL,
            req_id     TEXT NOT NULL,
            path       TEXT NOT NULL,
            position   INTEGER NOT NULL,
            obligation TEXT NOT NULL,
            scenarios  TEXT NOT NULL,
            revision   TEXT NOT NULL,
            PRIMARY KEY (project_id, req_id)
        )""",
    ),
]
