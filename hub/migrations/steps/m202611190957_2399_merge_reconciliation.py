"""Keep the evidence and origin of a merge observed outside the delivery gate."""

MIGRATIONS = [
    (
        "pipeline_merge_reconciliation_evidence",
        "ALTER TABLE pipeline_merges ADD COLUMN reconciliation TEXT NOT NULL DEFAULT ''",
    )
]
