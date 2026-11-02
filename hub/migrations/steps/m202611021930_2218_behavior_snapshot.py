"""#2218: снимок дельты на сдаче, список id больше не колонка.

Список id, записанный до дельты, отбрасывается: контекст считает его из
записей ``behavior_delta`` и колонку не читает. Снимок живёт на поколении
сдачи и по умолчанию пуст — пустая строка значит «снимок не создавался».
Флаг ``behavior_approval_void`` отделяет черновик следующей дельты от уже
зафиксированного снимка: старое одобрение задачу не завершает.
"""

MIGRATIONS = [
    (
        "drop_tasks_requirement_ids",
        "ALTER TABLE tasks DROP COLUMN requirement_ids",
    ),
    (
        "add_submissions_behavior_snapshot",
        "ALTER TABLE submissions ADD COLUMN behavior_snapshot TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_tasks_behavior_approval_void",
        "ALTER TABLE tasks ADD COLUMN behavior_approval_void INTEGER NOT NULL DEFAULT 0",
    ),
]
