"""#2054: долговечный дедуп пробуждения «CI стал verified».

``ci_verified_wakes`` - одна строка на (задача, поколение, закреплённый коммит):
автопилота будят по подтверждённому CI один раз, строку занимает ``INSERT OR
IGNORE`` (rowcount 1 - занял). Не чистится: события чистит ``prune_events``, а
дедуп на них повторял бы пробуждение через две недели.

``steward_judgements.transient_retry_at`` - метка «повтор применения после
подтверждённого CI уже был»: ставится в той же записи, что возвращает ``failed``
в очередь поллера, и суждение с меткой больше не повторяется. NULL - повтора не
было (и все строки до выката).
"""

MIGRATIONS = [
    (
        "create_ci_verified_wakes",
        """CREATE TABLE IF NOT EXISTS ci_verified_wakes (
            task_id    INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            head_sha   TEXT    NOT NULL,
            created_at TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (task_id, generation, head_sha)
        )""",
    ),
    (
        "add_steward_judgements_transient_retry_at",
        "ALTER TABLE steward_judgements ADD COLUMN transient_retry_at TEXT",
    ),
]
