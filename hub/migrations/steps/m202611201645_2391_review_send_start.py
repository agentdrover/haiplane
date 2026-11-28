"""Persist the single start transition of metered pilot review sends."""

MIGRATIONS = [
    (
        "epic_plan_review_send_start_2391",
        "ALTER TABLE epic_plan_work ADD COLUMN review_send_started_at TEXT",
    ),
]
