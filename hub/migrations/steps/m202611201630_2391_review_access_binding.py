"""Remember the original reviewer code ID across provider reconciliation."""

MIGRATIONS = [
    (
        "epic_plan_reviewer_code_2391",
        "ALTER TABLE epic_plan_work ADD COLUMN reviewer_code_id INTEGER",
    ),
]
