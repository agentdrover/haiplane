"""Keep the evidence and origin of a merge observed outside the delivery gate."""

MIGRATIONS = [
    (
        "pipeline_merge_reconciliation_evidence",
        "ALTER TABLE pipeline_merges ADD COLUMN reconciliation TEXT NOT NULL DEFAULT ''",
    ),
    (
        "pipeline_current_delivery_view",
        """
        CREATE VIEW current_pipeline_merges AS
        SELECT m.* FROM pipeline_merges m JOIN tasks t ON t.id = m.task_id
        WHERE (m.reconciliation = '' OR (
                m.pr_number = t.pr_number
                AND json_extract(m.reconciliation, '$.pin.branch') = t.branch
                AND json_extract(m.reconciliation, '$.pin.head') = t.submission_sha
                AND json_extract(m.reconciliation, '$.pin.generation') = t.submission_generation
                AND json_extract(m.reconciliation, '$.pin.verdict_revision') = t.review_verdict_revision
                AND t.review_verdict = 'approved'
                AND t.review_verdict_generation = t.submission_generation
                AND t.review_verdict_closed_generation IS NOT t.submission_generation
                AND t.behavior_approval_void = 0
                AND t.review_self_approved = 0
                AND EXISTS (
                    SELECT 1 FROM projects p
                    WHERE p.id = m.project_id
                      AND p.id = COALESCE(
                        (WITH RECURSIVE ancestors(project_id, parent_id, depth) AS (
                            SELECT t.project_id, t.parent_id, 0
                            UNION ALL
                            SELECT parent.project_id, parent.parent_id, a.depth + 1
                            FROM ancestors a JOIN tasks parent ON parent.id = a.parent_id
                            WHERE a.project_id IS NULL AND a.depth < 19
                         )
                         SELECT project_id FROM ancestors WHERE project_id IS NOT NULL LIMIT 1),
                        (SELECT id FROM projects WHERE slug = 'default')
                      )
                      AND p.status = 'active'
                      AND p.forge = 'github'
                      AND p.repo = json_extract(m.reconciliation, '$.pin.repository')
                      AND p.default_branch = json_extract(m.reconciliation, '$.pin.base')
                )
            ))
        """,
    ),
]
