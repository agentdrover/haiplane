"""#2090: явный владелец обещанного исхода.

``tasks.outcome_owner_task_id`` - «моё обещание держит #N»: ссылка на предка
задачи (проверяется при записи и при каждом чтении). NULL - обещание
самостоятельное. Колонка ничего не говорит об ответе: журнал ``outcome_answers``
(#819) не затрагивается.
"""

MIGRATIONS = [
    (
        "add_tasks_outcome_owner_task_id",
        "ALTER TABLE tasks ADD COLUMN outcome_owner_task_id INTEGER",
    ),
]
