"""#2052: итог, который ПИШЕТ тот, кто сложил с себя вердикт сдачи.

``tasks.verdict_owner`` - кто вынесет вердикт текущей сдачи, когда это записал
актор (``human`` - стюард, советник, автопилот или сторож отдали её человеку);
пусто - никто не записал, маршрут вердикта только прогнозирует.
``verdict_owner_reason`` - код и причина, ``verdict_owner_since`` - когда записано,
``verdict_owner_generation`` - поколение сдачи, к которому запись относится.
Запись действует, только пока оно равно ``submission_generation``: пересдача
делает владельца пустым без отдельного сброса.
"""

MIGRATIONS = [
    (
        "add_tasks_verdict_owner",
        "ALTER TABLE tasks ADD COLUMN verdict_owner TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_tasks_verdict_owner_reason",
        "ALTER TABLE tasks ADD COLUMN verdict_owner_reason TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_tasks_verdict_owner_since",
        "ALTER TABLE tasks ADD COLUMN verdict_owner_since TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_tasks_verdict_owner_generation",
        "ALTER TABLE tasks ADD COLUMN verdict_owner_generation "
        "INTEGER NOT NULL DEFAULT 0",
    ),
]
