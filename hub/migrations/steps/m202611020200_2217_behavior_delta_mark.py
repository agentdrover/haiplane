"""#2217: отметка поведенческой дельты задачи.

``tasks.behavior_delta`` - три состояния: ``unspecified`` (не сказано),
``delta``, ``no_delta``. NOT NULL с умолчанием ``unspecified``: пустота не
бывает четвёртым состоянием, а уже существующие задачи читаются «не сказано»,
не «дельты нет». Шаг трогает только ``tasks`` и не зависит от других шагов
эпика: применяется на ветке без схемы индекса спецификации.
"""

MIGRATIONS = [
    (
        "add_tasks_behavior_delta_2217",
        "ALTER TABLE tasks ADD COLUMN behavior_delta TEXT NOT NULL DEFAULT 'unspecified'",
    ),
]
