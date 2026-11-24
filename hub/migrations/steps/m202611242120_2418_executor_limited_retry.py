"""The retry cap and quota wait survive concurrent ticks and restarts."""

MIGRATIONS = [
    (
        "create_executor_limited_retry",
        """CREATE TABLE IF NOT EXISTS executor_limited_retry (
            source_order_id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            statement_generation INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            resume_after TEXT,
            order_id INTEGER UNIQUE,
            UNIQUE (task_id, statement_generation, generation),
            CHECK ((resume_after IS NULL) <> (order_id IS NULL))
        )""",
    ),
]
