"""#1611: отпечаток входов, из которых посчитана сохранённая готовность задачи.

``readiness_evidence`` - ``DorSnapshot.fingerprint()`` (#1610) того снимка, из
которого записаны ``dor_passed``/``readiness_score``/``ready_at``. Пустая строка -
строка записана до этой колонки: ей нельзя верить при автоодобрении, пока чтение
готовности не пересчитает и не допишет отпечаток.
"""

MIGRATIONS = [
    (
        "add_tasks_readiness_evidence",
        "ALTER TABLE tasks ADD COLUMN readiness_evidence TEXT NOT NULL DEFAULT ''",
    ),
]
