"""T11 review fixes: legacy alias owner and the operation's resource id."""

MIGRATIONS = [
    (
        "2241_task_idempotency_keys_principal_0",
        "ALTER TABLE task_idempotency_keys ADD COLUMN principal_ref TEXT",
    ),
    (
        "2241_operation_keys_resource_0",
        "ALTER TABLE operation_keys ADD COLUMN resource_id INTEGER",
    ),
    # Rows written before this step already carry the task snapshot; the id is
    # the part replay needs to re-check the CURRENT binding (review, major 4).
    (
        "2241_operation_keys_resource_backfill_0",
        """UPDATE operation_keys
        SET resource_id = CAST(json_extract(response_body, '$.id') AS INTEGER)
        WHERE resource_id IS NULL AND response_body IS NOT NULL""",
    ),
    # Rows dual-written before this step have no owner column. Recover it from
    # the alias + operation + task snapshot triple; the 151 historical keys do
    # not match any operation_aliases row and stay NULL on purpose (they had no
    # principal when they were written).
    (
        "2241_task_idempotency_keys_principal_backfill_0",
        """UPDATE task_idempotency_keys
        SET principal_ref = (
            SELECT a.principal_ref
            FROM operation_aliases a
            JOIN operation_keys k
              ON k.principal_ref = a.principal_ref
             AND k.operation_id = a.operation_id
            WHERE a.alias = task_idempotency_keys.client_request_id
              AND k.operation = 'POST /api/tasks'
              AND k.response_body IS NOT NULL
              AND CAST(json_extract(k.response_body, '$.id') AS INTEGER)
                  = task_idempotency_keys.task_id
        )
        WHERE principal_ref IS NULL""",
    ),
]
