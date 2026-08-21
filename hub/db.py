from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

from hub.config import HUB_DB_PATH

log = logging.getLogger("hub.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT    NOT NULL,
    description     TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'open',
    runtime         TEXT    NOT NULL DEFAULT 'auto',
    source          TEXT    NOT NULL DEFAULT 'human',
    assigned_agent  TEXT    NOT NULL DEFAULT '',
    rationale       TEXT    NOT NULL DEFAULT '',
    job_id          TEXT,
    exit_code       INTEGER,
    result_text     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_updates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL REFERENCES tasks(id),
    agent      TEXT    NOT NULL DEFAULT '',
    kind       TEXT    NOT NULL DEFAULT 'status',
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS activity_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,
    summary   TEXT NOT NULL,
    detail    TEXT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_MIGRATIONS: list[tuple[str, str]] = [
    (
        "add_source_column",
        "ALTER TABLE tasks ADD COLUMN source TEXT NOT NULL DEFAULT 'human'",
    ),
    (
        "add_assigned_agent_column",
        "ALTER TABLE tasks ADD COLUMN assigned_agent TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_rationale_column",
        "ALTER TABLE tasks ADD COLUMN rationale TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_review_cycle_column",
        "ALTER TABLE tasks ADD COLUMN review_cycle INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "add_auto_review_column",
        "ALTER TABLE tasks ADD COLUMN auto_review INTEGER NOT NULL DEFAULT 1",
    ),
    ("add_review_job_id_column", "ALTER TABLE tasks ADD COLUMN review_job_id TEXT"),
    ("add_branch_column", "ALTER TABLE tasks ADD COLUMN branch TEXT"),
    ("add_pr_number_column", "ALTER TABLE tasks ADD COLUMN pr_number INTEGER"),
    (
        "add_ci_fix_cycle_column",
        "ALTER TABLE tasks ADD COLUMN ci_fix_cycle INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "add_task_type_column",
        "ALTER TABLE tasks ADD COLUMN task_type TEXT NOT NULL DEFAULT 'task'",
    ),
    (
        "add_parent_id_column",
        "ALTER TABLE tasks ADD COLUMN parent_id INTEGER REFERENCES tasks(id)",
    ),
    (
        "add_position_column",
        "ALTER TABLE tasks ADD COLUMN position INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "add_priority_column",
        "ALTER TABLE tasks ADD COLUMN priority TEXT NOT NULL DEFAULT 'medium'",
    ),
    # Indexes for frequent queries
    (
        "idx_tasks_parent_id",
        "CREATE INDEX IF NOT EXISTS idx_tasks_parent_id ON tasks(parent_id)",
    ),
    (
        "idx_tasks_status",
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
    ),
    (
        "idx_tasks_type_status",
        "CREATE INDEX IF NOT EXISTS idx_tasks_type_status ON tasks(task_type, status)",
    ),
    (
        "idx_task_updates_task_id",
        "CREATE INDEX IF NOT EXISTS idx_task_updates_task_id ON task_updates(task_id)",
    ),
    # Structured task form (Kanban DoR) — see Epic #32.
    # Nullable / empty defaults so existing rows stay valid without backfill.
    (
        "add_work_type_column",
        "ALTER TABLE tasks ADD COLUMN work_type TEXT NOT NULL DEFAULT 'feature'",
    ),
    (
        "add_class_of_service_column",
        "ALTER TABLE tasks ADD COLUMN class_of_service TEXT NOT NULL DEFAULT 'standard'",
    ),
    ("add_size_column", "ALTER TABLE tasks ADD COLUMN size TEXT"),
    ("add_wip_tag_column", "ALTER TABLE tasks ADD COLUMN wip_tag TEXT"),
    ("add_due_date_column", "ALTER TABLE tasks ADD COLUMN due_date TEXT"),
    (
        "add_user_story_column",
        "ALTER TABLE tasks ADD COLUMN user_story TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_problem_statement_column",
        "ALTER TABLE tasks ADD COLUMN problem_statement TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_business_value_column",
        "ALTER TABLE tasks ADD COLUMN business_value TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_scope_in_column",
        "ALTER TABLE tasks ADD COLUMN scope_in TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "add_scope_out_column",
        "ALTER TABLE tasks ADD COLUMN scope_out TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "add_affected_areas_column",
        "ALTER TABLE tasks ADD COLUMN affected_areas TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "add_technical_hints_column",
        "ALTER TABLE tasks ADD COLUMN technical_hints TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_constraints_column",
        "ALTER TABLE tasks ADD COLUMN constraints TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "add_assumptions_column",
        "ALTER TABLE tasks ADD COLUMN assumptions TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "add_validation_commands_column",
        "ALTER TABLE tasks ADD COLUMN validation_commands TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "add_out_of_scope_for_review_column",
        "ALTER TABLE tasks ADD COLUMN out_of_scope_for_review TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "add_risks_column",
        "ALTER TABLE tasks ADD COLUMN risks TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "add_readiness_score_column",
        "ALTER TABLE tasks ADD COLUMN readiness_score INTEGER",
    ),
    ("add_dor_passed_column", "ALTER TABLE tasks ADD COLUMN dor_passed INTEGER"),
    ("add_ready_at_column", "ALTER TABLE tasks ADD COLUMN ready_at TEXT"),
    ("add_started_at_column", "ALTER TABLE tasks ADD COLUMN started_at TEXT"),
    ("add_completed_at_column", "ALTER TABLE tasks ADD COLUMN completed_at TEXT"),
    (
        "create_acceptance_criteria_table",
        """CREATE TABLE IF NOT EXISTS acceptance_criteria (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            ac_id           TEXT    NOT NULL,
            given           TEXT    NOT NULL,
            when_clause     TEXT    NOT NULL,
            then_clause     TEXT    NOT NULL,
            verifiable_by   TEXT    NOT NULL,
            test_ref        TEXT,
            position        INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (task_id, ac_id)
        )""",
    ),
    (
        "idx_acceptance_criteria_task_id",
        "CREATE INDEX IF NOT EXISTS idx_acceptance_criteria_task_id ON acceptance_criteria(task_id)",
    ),
    (
        "add_human_owner_column",
        "ALTER TABLE tasks ADD COLUMN human_owner TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_human_reviewer_column",
        "ALTER TABLE tasks ADD COLUMN human_reviewer TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_review_checklist_column",
        "ALTER TABLE tasks ADD COLUMN review_checklist TEXT NOT NULL DEFAULT '[]'",
    ),
    # ---- Admin section (Stage 4) ----
    (
        "create_principals_table",
        """CREATE TABLE IF NOT EXISTS principals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK(kind IN ('human','agent','service')),
            username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled','locked')),
            notes TEXT NOT NULL DEFAULT '',
            created_by INTEGER REFERENCES principals(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen_at TEXT
        )""",
    ),
    (
        "create_roles_table",
        """CREATE TABLE IF NOT EXISTS roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            system INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "create_principal_roles_table",
        """CREATE TABLE IF NOT EXISTS principal_roles (
            principal_id INTEGER NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
            role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
            granted_by INTEGER REFERENCES principals(id),
            granted_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (principal_id, role_id)
        )""",
    ),
    (
        "create_role_permissions_table",
        """CREATE TABLE IF NOT EXISTS role_permissions (
            role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
            permission TEXT NOT NULL,
            PRIMARY KEY (role_id, permission)
        )""",
    ),
    (
        "create_password_credentials_table",
        """CREATE TABLE IF NOT EXISTS password_credentials (
            principal_id INTEGER PRIMARY KEY REFERENCES principals(id) ON DELETE CASCADE,
            password_hash TEXT NOT NULL,
            hash_algorithm TEXT NOT NULL DEFAULT 'argon2id',
            password_changed_at TEXT NOT NULL DEFAULT (datetime('now')),
            must_rotate INTEGER NOT NULL DEFAULT 0,
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT,
            last_login_at TEXT
        )""",
    ),
    (
        "create_api_keys_table",
        """CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            principal_id INTEGER NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            key_prefix TEXT NOT NULL,
            key_hash TEXT NOT NULL UNIQUE,
            scopes TEXT NOT NULL DEFAULT '[]',
            expires_at TEXT,
            last_used_at TEXT,
            revoked_at TEXT,
            created_by INTEGER REFERENCES principals(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "create_browser_sessions_table",
        """CREATE TABLE IF NOT EXISTS browser_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            principal_id INTEGER NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
            session_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            last_seen_at TEXT,
            revoked_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            ip_hash TEXT NOT NULL DEFAULT '',
            user_agent TEXT NOT NULL DEFAULT ''
        )""",
    ),
    (
        "create_admin_audit_log_table",
        """CREATE TABLE IF NOT EXISTS admin_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_principal_id INTEGER REFERENCES principals(id),
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL,
            detail TEXT,
            ip_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "idx_principals_kind_status",
        "CREATE INDEX IF NOT EXISTS idx_principals_kind_status ON principals(kind, status)",
    ),
    (
        "idx_api_keys_principal_id",
        "CREATE INDEX IF NOT EXISTS idx_api_keys_principal_id ON api_keys(principal_id)",
    ),
    (
        "idx_api_keys_prefix",
        "CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys(key_prefix)",
    ),
    (
        "idx_browser_sessions_principal_id",
        "CREATE INDEX IF NOT EXISTS idx_browser_sessions_principal_id ON browser_sessions(principal_id)",
    ),
    (
        "idx_admin_audit_actor",
        "CREATE INDEX IF NOT EXISTS idx_admin_audit_actor ON admin_audit_log(actor_principal_id)",
    ),
    (
        "idx_admin_audit_target",
        "CREATE INDEX IF NOT EXISTS idx_admin_audit_target ON admin_audit_log(target_type, target_id)",
    ),
    (
        "add_tasks_archived_column",
        "ALTER TABLE tasks ADD COLUMN archived INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "add_prepared_by_column",
        "ALTER TABLE tasks ADD COLUMN prepared_by TEXT NOT NULL DEFAULT ''",
    ),
    ("add_prepared_at_column", "ALTER TABLE tasks ADD COLUMN prepared_at TEXT"),
    (
        "idx_tasks_archived",
        "CREATE INDEX IF NOT EXISTS idx_tasks_archived ON tasks(archived)",
    ),
    ("add_claimed_by_column", "ALTER TABLE tasks ADD COLUMN claimed_by TEXT"),
    (
        "add_claim_session_id_column",
        "ALTER TABLE tasks ADD COLUMN claim_session_id TEXT",
    ),
    ("add_claimed_at_column", "ALTER TABLE tasks ADD COLUMN claimed_at TEXT"),
    # ---- Universal Review Gate (#305): review submission generations ----
    (
        "add_submission_generation_column",
        "ALTER TABLE tasks ADD COLUMN submission_generation INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "add_review_verdict_column",
        "ALTER TABLE tasks ADD COLUMN review_verdict TEXT",
    ),
    (
        "add_review_verdict_generation_column",
        "ALTER TABLE tasks ADD COLUMN review_verdict_generation INTEGER",
    ),
    # ---- Universal Review Gate (#308): structured findings of the latest verdict
    (
        "add_review_findings_column",
        "ALTER TABLE tasks ADD COLUMN review_findings TEXT NOT NULL DEFAULT '[]'",
    ),
    # ---- Projects V1.1 (#335): multi-project foundation ----
    (
        "create_projects_table",
        """CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            repo TEXT NOT NULL DEFAULT '',
            workspace_path TEXT NOT NULL DEFAULT '',
            default_branch TEXT NOT NULL DEFAULT 'develop',
            default_branch_policy TEXT NOT NULL DEFAULT '{}',
            archived INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "add_tasks_project_id_column",
        "ALTER TABLE tasks ADD COLUMN project_id INTEGER",
    ),
    (
        "idx_tasks_project_id",
        "CREATE INDEX IF NOT EXISTS idx_tasks_project_id ON tasks(project_id)",
    ),
    (
        "add_projects_status_column",
        "ALTER TABLE projects ADD COLUMN status TEXT NOT NULL DEFAULT 'active'",
    ),
    # ---- Separation of duties (#320): implementer identity as a principal.
    # Plain INTEGER on purpose (no FK): the value is an identity snapshot for
    # the self-review comparison, and it must survive principal deletion and
    # non-DB token sources without integrity errors.
    (
        "add_implementer_principal_id_column",
        "ALTER TABLE tasks ADD COLUMN implementer_principal_id INTEGER",
    ),
    # ---- Events feed (#349): typed, cursor-addressable transition events.
    # task_id/project_id are plain INTEGERs (no FK) for the same snapshot
    # semantics as implementer_principal_id: an event must outlive its task.
    (
        "create_events_table",
        "CREATE TABLE IF NOT EXISTS events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "kind TEXT NOT NULL, "
        "task_id INTEGER, "
        "project_id INTEGER, "
        "actor TEXT NOT NULL DEFAULT '', "
        "payload TEXT NOT NULL DEFAULT '{}', "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')))",
    ),
    (
        "idx_events_created_at",
        "CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at)",
    ),
    # ---- Skills library (#380): versioned prompts/checklists for agents.
    # Every INSERT is a new (name, version) row; the live one is the highest
    # version with status='active' — history is immutable by construction.
    (
        "create_skills_table",
        "CREATE TABLE IF NOT EXISTS skills ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL, "
        "kind TEXT NOT NULL DEFAULT 'prompt', "
        "version INTEGER NOT NULL, "
        "content TEXT NOT NULL, "
        "tags TEXT NOT NULL DEFAULT '[]', "
        "project_id INTEGER, "
        "status TEXT NOT NULL DEFAULT 'draft', "
        "created_by TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "UNIQUE(name, version))",
    ),
    (
        "idx_skills_name_status",
        "CREATE INDEX IF NOT EXISTS idx_skills_name_status ON skills(name, status)",
    ),
    # ---- Machine review policy (#382): project default + task override.
    (
        "add_projects_machine_review_column",
        "ALTER TABLE projects ADD COLUMN machine_review TEXT NOT NULL DEFAULT 'auto'",
    ),
    (
        "add_tasks_machine_review_override_column",
        "ALTER TABLE tasks ADD COLUMN machine_review_override TEXT",
    ),
    # ---- Machine review reports (#381): structured multi-agent review
    # outcomes bound to a submission generation; metrics fields (#384)
    # are nullable — clients that can't count tokens still report.
    (
        "create_machine_reviews_table",
        "CREATE TABLE IF NOT EXISTS machine_reviews ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id INTEGER NOT NULL, "
        "submission_generation INTEGER NOT NULL, "
        "harness_skill TEXT NOT NULL DEFAULT '', "
        "harness_version INTEGER, "
        "agent_count INTEGER, "
        "tokens_spent INTEGER, "
        "duration_ms INTEGER, "
        "orchestrator TEXT NOT NULL DEFAULT '', "
        "model TEXT NOT NULL DEFAULT '', "
        "raw_count INTEGER NOT NULL DEFAULT 0, "
        "findings_confirmed TEXT NOT NULL DEFAULT '[]', "
        "findings_rejected TEXT NOT NULL DEFAULT '[]', "
        "submitted_by TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')))",
    ),
    (
        "idx_machine_reviews_task",
        "CREATE INDEX IF NOT EXISTS idx_machine_reviews_task "
        "ON machine_reviews(task_id, submission_generation)",
    ),
    # ---- Workspace provisioning (#347): clone state lives on the project.
    (
        "add_projects_provision_status_column",
        "ALTER TABLE projects ADD COLUMN provision_status TEXT NOT NULL DEFAULT 'none'",
    ),
    (
        "add_projects_provision_detail_column",
        "ALTER TABLE projects ADD COLUMN provision_detail TEXT NOT NULL DEFAULT ''",
    ),
    # ---- Audited solo mode (#434): verdicts accepted only because of
    # OPENCLAW_REVIEW_SELF_APPROVE=allow stay distinguishable in hindsight.
    (
        "add_review_self_approved_column",
        "ALTER TABLE tasks ADD COLUMN review_self_approved INTEGER NOT NULL DEFAULT 0",
    ),
    # ---- Durable poller state (#416): orchestration clocks and CI retry
    # budget move out of process memory into the row, so a hub restart no
    # longer resets grace periods or retry counts. status_entered_at is the
    # clock a status transition sets (F2 deadlines read it); ci_check_started_at
    # replaces the in-memory push time; ci_no_pr_attempts replaces the
    # in-memory retry counter. Existing rows get status_entered_at backfilled to
    # migration time so they receive a full grace window, never an instant
    # escalation.
    (
        "add_status_entered_at_column",
        "ALTER TABLE tasks ADD COLUMN status_entered_at TEXT",
    ),
    (
        "add_ci_check_started_at_column",
        "ALTER TABLE tasks ADD COLUMN ci_check_started_at TEXT",
    ),
    (
        "add_ci_no_pr_attempts_column",
        "ALTER TABLE tasks ADD COLUMN ci_no_pr_attempts INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "backfill_status_entered_at",
        "UPDATE tasks SET status_entered_at = datetime('now') "
        "WHERE status_entered_at IS NULL",
    ),
    # ---- Bounded recovery for missing jobs (#417): the durable clock that
    # marks when a headless dispatch/review job was first seen missing, so the
    # grace-then-escalate decision survives a restart. NULL means the job is
    # present (or the task is not headless).
    (
        "add_job_missing_since_column",
        "ALTER TABLE tasks ADD COLUMN job_missing_since TEXT",
    ),
    # ---- At-most-once arbiter dispatch (#421): the arbiter fact used to be
    # inferred from an agent-written update, so a repeat poll or restart could
    # re-dispatch a paid arbiter job. These columns make the dispatch a durable
    # conditional claim per submission generation: state dispatching→running→
    # finished, with the job id and the dispatch clock. Existing rows start with
    # NULL state (no arbiter in flight).
    (
        "add_arbiter_generation_column",
        "ALTER TABLE tasks ADD COLUMN arbiter_generation INTEGER",
    ),
    ("add_arbiter_state_column", "ALTER TABLE tasks ADD COLUMN arbiter_state TEXT"),
    ("add_arbiter_job_id_column", "ALTER TABLE tasks ADD COLUMN arbiter_job_id TEXT"),
    (
        "add_arbiter_dispatch_at_column",
        "ALTER TABLE tasks ADD COLUMN arbiter_dispatch_at TEXT",
    ),
    (
        "create_task_idempotency_keys_table",
        """
        CREATE TABLE IF NOT EXISTS task_idempotency_keys (
            client_request_id TEXT PRIMARY KEY,
            task_id INTEGER NOT NULL REFERENCES tasks(id),
            request_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
    ),
    # ---- Verifiable SDD (#507): pass/fail of each verifiable_by=test AC's
    # bound test, stamped with the submission_generation it was run for. One row
    # per (task, ac) — upserted on each run; a result counts as current only
    # while its generation matches the task's submission_generation.
    (
        "create_ac_test_results_table",
        """
        CREATE TABLE IF NOT EXISTS ac_test_results (
            task_id INTEGER NOT NULL REFERENCES tasks(id),
            ac_id TEXT NOT NULL,
            submission_generation INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (task_id, ac_id)
        )
        """,
    ),
    # ---- Verifiable SDD (#509): result of running task.validation_commands,
    # stamped with the submission_generation. One result per task (the commands
    # are a single set); current only while the generation matches.
    (
        "add_validation_generation_column",
        "ALTER TABLE tasks ADD COLUMN validation_generation INTEGER",
    ),
    (
        "add_validation_status_column",
        "ALTER TABLE tasks ADD COLUMN validation_status TEXT",
    ),
    ("add_validation_log_column", "ALTER TABLE tasks ADD COLUMN validation_log TEXT"),
    # ---- Machine-review honest incompleteness (#549). The harness core rule is
    # that a missing voice never equals a missing defect, but the report schema
    # carried no way to say so — the skill told authors to write it as prose in
    # the first finding. Nullable on purpose: rows written before this column
    # made no completeness claim, and defaulting them to 0 would assert one.
    (
        "add_machine_reviews_incomplete_column",
        "ALTER TABLE machine_reviews ADD COLUMN incomplete INTEGER",
    ),
    (
        "add_machine_reviews_unresolved_column",
        "ALTER TABLE machine_reviews ADD COLUMN unresolved TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "add_machine_reviews_lost_dimensions_column",
        "ALTER TABLE machine_reviews ADD COLUMN lost_dimensions TEXT "
        "NOT NULL DEFAULT '[]'",
    ),
    (
        "add_task_updates_principal_id",
        "ALTER TABLE task_updates ADD COLUMN principal_id INTEGER",
    ),
    (
        # The DB default stamps HISTORY: every row that existed before this
        # migration is honestly marked as written before the field existed.
        # New rows never take it — they always come through add_task_update,
        # whose own default is "hub" (#559). Without this split a NULL/blank
        # would have meant three different things at once: predates the field,
        # written by the hub itself, or written with no authentication. That
        # collapse is the defect class this task exists to remove.
        "add_task_updates_author_kind",
        "ALTER TABLE task_updates ADD COLUMN author_kind TEXT "
        "NOT NULL DEFAULT 'legacy'",
    ),
    (
        "add_outcome_metric_column",
        "ALTER TABLE tasks ADD COLUMN outcome_metric TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_outcome_indicator_column",
        "ALTER TABLE tasks ADD COLUMN outcome_indicator TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_outcome_deadline_column",
        "ALTER TABLE tasks ADD COLUMN outcome_deadline TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_outcome_revisit_condition_column",
        "ALTER TABLE tasks ADD COLUMN outcome_revisit_condition TEXT NOT NULL DEFAULT ''",
    ),
    (
        # Nullable on purpose: this column holds an enum, and NULL is the
        # only honest "not chosen". A NOT NULL DEFAULT '' would make the
        # empty string a third state that is neither a valid choice nor
        # absent, and TaskView would fail to coerce it (#331).
        "add_redesign_decision_column",
        "ALTER TABLE tasks ADD COLUMN redesign_decision TEXT",
    ),
    (
        "add_redesign_rationale_column",
        "ALTER TABLE tasks ADD COLUMN redesign_rationale TEXT NOT NULL DEFAULT ''",
    ),
    (
        # Nullable on purpose: this column holds an enum, and NULL is the
        # only honest "not chosen". A NOT NULL DEFAULT '' would make the
        # empty string a third state that is neither a valid choice nor
        # absent, and TaskView would fail to coerce it (#331).
        "add_agent_fit_column",
        "ALTER TABLE tasks ADD COLUMN agent_fit TEXT",
    ),
    (
        # Bring the rows written before #594 to the same shape as their
        # neighbours. datetime() rather than string surgery on purpose: it
        # applies the offset, so a value stored as +03:00 converts correctly
        # instead of shifting by three hours. Every row on production carries
        # +00:00 today — checked, not assumed — but the migration must not
        # depend on that staying true.
        #
        # The value does not change, only its representation: julianday()
        # returns an identical number before and after. Idempotent — rows
        # already in the target shape carry no 'T' and are skipped.
        "normalize_ready_at_format",
        "UPDATE tasks SET ready_at = datetime(ready_at) "
        "WHERE ready_at IS NOT NULL AND ready_at LIKE '%T%'",
    ),
    (
        # Nullable on purpose: NULL is "not stated", which must stay
        # distinguishable from the explicit choice "implementation". A NOT
        # NULL DEFAULT '' would make the empty string a third state that is
        # neither (#595).
        "add_ac_expectation_source_column",
        "ALTER TABLE acceptance_criteria ADD COLUMN expectation_source TEXT",
    ),
    (
        # One row per drift commit already reported. The UNIQUE key is what
        # makes a repeated check silent instead of noisy (#534 AC-5), and
        # what keeps a case a human has accepted from reopening.
        # Merges the hub performed itself. Without this the only evidence a
        # commit came through the pipeline is a "(#N)" in its subject, which
        # anyone can type — a direct push named "hotfix (#42)" would pass as
        # legitimate. Found in review of submission #1 (#534).
        "create_pipeline_merges",
        """CREATE TABLE IF NOT EXISTS pipeline_merges (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER,
            pr_number   INTEGER NOT NULL,
            task_id     INTEGER,
            merged_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (project_id, pr_number)
        )""",
    ),
    (
        # Where each project's base stood when the guard first looked. History
        # written before the hub started recording its own merges cannot be
        # judged — treating it as drift would bury the operator in alerts
        # about work that went through the pipeline correctly (#534).
        "add_projects_drift_baseline",
        "ALTER TABLE projects ADD COLUMN drift_baseline_sha TEXT",
    ),
    (
        # The commit the merge produced. A pull-request number lives in the
        # subject, which anyone can type: a direct push titled
        # "hotfix (#42)" passed as legitimate even after the number had to be
        # one the hub really merged, because the number is still just text.
        # A SHA is not text the pusher controls (#534, review of #2).
        "add_pipeline_merges_sha",
        "ALTER TABLE pipeline_merges ADD COLUMN merge_sha TEXT",
    ),
    (
        "create_base_branch_drift",
        """CREATE TABLE IF NOT EXISTS base_branch_drift (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            sha         TEXT    NOT NULL,
            branch      TEXT    NOT NULL,
            subject     TEXT    NOT NULL DEFAULT '',
            author      TEXT    NOT NULL DEFAULT '',
            detected_at TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (project_id, sha)
        )""",
    ),
    (
        # The verdict binds to the submission NUMBER; this binds it to the
        # CODE. Between submission and verdict the branch can move, and the
        # number alone lets an APPROVED silently cover commits the reviewer
        # never saw — reproduced on #547, then twice more on #601 and #532
        # (#572). Empty means "recorded before this existed, or the tip could
        # not be resolved": both degrade to today's behaviour, never to a
        # refusal.
        "add_tasks_submission_sha",
        "ALTER TABLE tasks ADD COLUMN submission_sha TEXT NOT NULL DEFAULT ''",
    ),
    (
        # #546: the run evidence CI reports back. Keyed by COMMIT, not by
        # submission generation, because the two orders both happen in real
        # life: CI usually runs when the PR opens (before any submission, when
        # the generation is still 0), and again after a resubmission. A commit
        # is the one identifier that exists in both moments and that the
        # reporter does not get to choose — the hub decides whether that commit
        # is the one it pinned. UNIQUE(task_id, head_sha) makes a re-reported
        # run an update rather than a second opinion.
        "create_ci_run_reports",
        """CREATE TABLE IF NOT EXISTS ci_run_reports (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id           INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            head_sha          TEXT    NOT NULL,
            ac_results        TEXT    NOT NULL DEFAULT '{}',
            validation_status TEXT    NOT NULL DEFAULT '',
            validation_log    TEXT    NOT NULL DEFAULT '',
            reason            TEXT    NOT NULL DEFAULT '',
            reported_by       TEXT    NOT NULL DEFAULT '',
            reported_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (task_id, head_sha)
        )""",
    ),
    (
        # Nullable on purpose: NULL is the only honest "not computed".
        # R0 means "computed and found harmless" — collapsing NULL into it
        # would silently mark every uncounted task as safe the moment the
        # class starts gating anything (#581; same defect class that
        # author_kind closed in #559). A NOT NULL DEFAULT '' would add a
        # third state that is neither a class nor absence (#331).
        "add_risk_class_column",
        "ALTER TABLE tasks ADD COLUMN risk_class TEXT",
    ),
    (
        # The observable features the class was derived from (#582), JSON
        # list of strings like the other list columns. '[]' — not NULL — is
        # correct here: an empty list means "computed, nothing triggered",
        # while "not computed" is already carried by risk_class IS NULL.
        "add_risk_class_reasons_column",
        "ALTER TABLE tasks ADD COLUMN risk_class_reasons TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        # Per-project gate policy (#743, feature #738): {"dor": "human"|"auto",
        # "verdict": "human"|"auto"}. '{}' is honest here — "no policy set"
        # simply means the default (every gate human), there is no third
        # state to distinguish, unlike risk_class where NULL had to stay
        # separate from a computed value. Inert until #744 reads it.
        "add_projects_gate_policy",
        "ALTER TABLE projects ADD COLUMN gate_policy TEXT NOT NULL DEFAULT '{}'",
    ),
    (
        # Autopilot daily digests (#739): one row per project per UTC day
        # that saw autopilot activity. The UNIQUE key is what makes the
        # poller's generation idempotent — the same rule the merge ledger
        # follows (#605). payload carries the day's facts as JSON: approvals
        # with their grounds, escalations, deliveries, and the audit sample.
        "create_autopilot_digests",
        """CREATE TABLE IF NOT EXISTS autopilot_digests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            digest_date TEXT    NOT NULL,
            payload     TEXT    NOT NULL DEFAULT '{}',
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (project_id, digest_date)
        )""",
    ),
    (
        # Model diversity (#758): which model wrote the submitted code,
        # declared by the submitter. '' means "not declared" — and the
        # auto-verdict treats missing data as NOT diverse, the same
        # principle as raw_count=0 and risk_class NULL.
        "add_tasks_submission_model",
        "ALTER TABLE tasks ADD COLUMN submission_model TEXT NOT NULL DEFAULT ''",
    ),
    (
        # Hub-dispatched cross-model reviews (#757): one row per dispatched
        # cloud reviewer run. status: active → done | failed. The poller
        # walks 'active' rows; a run that finished without a report for the
        # dispatched generation fails LOUDLY, never silently.
        "create_review_dispatches",
        """CREATE TABLE IF NOT EXISTS review_dispatches (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id               INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            submission_generation INTEGER NOT NULL,
            agent_id              TEXT    NOT NULL,
            run_id                TEXT    NOT NULL DEFAULT '',
            model                 TEXT    NOT NULL DEFAULT '',
            status                TEXT    NOT NULL DEFAULT 'active',
            created_at            TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        # Agent session registry (#771, feature #770): the session becomes an
        # address other sessions can write to. principal_id is a plain INTEGER
        # (no FK) for the same identity-snapshot reason as
        # implementer_principal_id — a session record must outlive the
        # principal row. There is deliberately NO `online` column: presence is
        # derived from last_seen_at at read time, because an agent dies without
        # saying goodbye and a stored online=true would be a green light over a
        # check that never ran (#725).
        "create_agent_sessions",
        """CREATE TABLE IF NOT EXISTS agent_sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      TEXT    NOT NULL UNIQUE,
            principal_id    INTEGER,
            agent           TEXT    NOT NULL DEFAULT '',
            model           TEXT    NOT NULL DEFAULT '',
            host            TEXT    NOT NULL DEFAULT '',
            workspace       TEXT    NOT NULL DEFAULT '',
            current_task_id INTEGER,
            started_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            last_seen_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "idx_agent_sessions_last_seen",
        "CREATE INDEX IF NOT EXISTS idx_agent_sessions_last_seen "
        "ON agent_sessions(last_seen_at)",
    ),
    (
        # Agent messages (#773, feature #770): the coordination channel.
        # The address is one (to_kind, to_ref) pair rather than four nullable
        # columns — the shape itself says "exactly one addressee", and no NULL
        # can quietly come to mean "everyone". to_ref stays TEXT with no FK for
        # the same snapshot reason events use: a message must outlive the task,
        # project or session it points at. The body is stored as written and
        # never interpreted: what a message says is data for its reader, not an
        # instruction for the hub.
        "create_agent_messages",
        """CREATE TABLE IF NOT EXISTS agent_messages (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id         TEXT    NOT NULL DEFAULT '',
            from_principal_id INTEGER,
            from_session_id   TEXT    NOT NULL DEFAULT '',
            from_agent        TEXT    NOT NULL DEFAULT '',
            from_model        TEXT    NOT NULL DEFAULT '',
            to_kind           TEXT    NOT NULL,
            to_ref            TEXT    NOT NULL,
            kind              TEXT    NOT NULL DEFAULT 'note',
            body              TEXT    NOT NULL,
            related_task_id   INTEGER,
            created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "idx_agent_messages_address",
        "CREATE INDEX IF NOT EXISTS idx_agent_messages_address "
        "ON agent_messages(to_kind, to_ref, id)",
    ),
    (
        # MCP usage telemetry (#780, epic #776): what every Agent API call
        # cost and how it ended, so the core surface is chosen from data
        # instead of taste.
        #
        # The privacy property is the shape of this table, not a rule someone
        # remembers to follow: there is no column an argument value, a token,
        # a message body or a response payload could be written into. A leak
        # here would have to be an ALTER TABLE, which a review can see, rather
        # than one careless call site nobody reads again.
        #
        # task_id is the single exception to "no argument values" and it is
        # INTEGER for exactly that reason — a task reference is the one piece
        # of routing worth having in a usage report, and a number cannot carry
        # a secret. error_reason holds a slug produced by the hub itself
        # (validated at the call site), never an error message, which is where
        # argument values would otherwise surface.
        #
        # principal_id is a plain INTEGER with no FK for the same
        # identity-snapshot reason agent_sessions gives: the record of a call
        # must outlive the key that made it.
        "create_mcp_call_events",
        """CREATE TABLE IF NOT EXISTS mcp_call_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            tool           TEXT    NOT NULL,
            profile        TEXT    NOT NULL DEFAULT 'v1',
            principal_id   INTEGER,
            principal_role TEXT    NOT NULL DEFAULT '',
            status         TEXT    NOT NULL DEFAULT 'ok',
            error_reason   TEXT    NOT NULL DEFAULT '',
            latency_ms     INTEGER NOT NULL DEFAULT 0,
            response_chars INTEGER NOT NULL DEFAULT 0,
            task_id        INTEGER,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        # The report always reads a time window first and groups by tool
        # second, so the index leads with created_at.
        "idx_mcp_call_events_window",
        "CREATE INDEX IF NOT EXISTS idx_mcp_call_events_window "
        "ON mcp_call_events(created_at, tool)",
    ),
    (
        # Review profiles (#807): which profile a dispatched run was launched
        # with. Written by the hub at dispatch time, never by the report about
        # itself — the #750 lesson: a client's own claim about its run is not
        # evidence of the run.
        "add_review_dispatches_profile",
        "ALTER TABLE review_dispatches ADD COLUMN profile TEXT NOT NULL DEFAULT ''",
    ),
    (
        # What the PROVIDER billed for this review run (#828). The harness
        # reports its own token count, and on the first live cross-model run
        # the two disagreed by 34x: 175 000 reported against 6 013 569 billed
        # (#818). Until now the provider number was fetched only to raise a
        # mismatch alert and then thrown away, so the practice economics kept
        # being computed from what the reviewed party said about itself.
        #
        # NULL means the provider was never asked or did not answer; 0 would
        # mean it answered zero. Different facts, never collapsed (#549).
        "add_machine_reviews_provider_tokens",
        "ALTER TABLE machine_reviews ADD COLUMN provider_tokens INTEGER",
    ),
    (
        # The profile the report was produced under, copied from the dispatch
        # of the same generation. Empty means "no dispatch behind this report"
        # — distinguishable from 'lite', because "we do not know how this was
        # reviewed" and "it was reviewed cheaply" are different facts.
        "add_machine_reviews_profile",
        "ALTER TABLE machine_reviews ADD COLUMN profile TEXT NOT NULL DEFAULT ''",
    ),
    (
        # ---- Outcome answers (#819): where the check of an outcome_metric
        # lands. #766 shipped the debt list read-only to find out whether these
        # metrics can be answered at all; #810 answered that on a live case, so
        # this is the storage that slice deliberately left out.
        #
        # A log, not a column on tasks: an outcome_deadline routinely names more
        # than one moment ("right after release, again in two weeks"), and a
        # second check that overwrites the first destroys the only evidence that
        # anyone came back.
        #
        # answered_by is a name snapshot, no FK — the record of who checked must
        # outlive the key that made it, same reasoning as agent_sessions.
        # measured_value is NOT NULL and refused when blank at the call site: an
        # answer without a number or an observation is an opinion, and a log of
        # opinions would close the loop only in appearance.
        "create_outcome_answers_table",
        """CREATE TABLE IF NOT EXISTS outcome_answers (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id        INTEGER NOT NULL,
            verdict        TEXT    NOT NULL,
            measured_value TEXT    NOT NULL,
            note           TEXT    NOT NULL DEFAULT '',
            answered_by    TEXT    NOT NULL DEFAULT '',
            answered_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        # The debt read joins answers per task and shows the latest first.
        "idx_outcome_answers_task",
        "CREATE INDEX IF NOT EXISTS idx_outcome_answers_task "
        "ON outcome_answers(task_id, answered_at)",
    ),
    (
        # Live-check evidence (#813, feature #811): did anyone actually watch
        # this work behave after it shipped? Three defects on 21.08.2026
        # (#801, #802, #803) passed review and green CI and were found only by
        # running against production — and there was nowhere to record that a
        # run had happened, so "checked" and "never checked" looked identical.
        #
        # Keyed by task AND sha, not by task alone: evidence belongs to the
        # deployment it was taken against, and a later deploy does not inherit
        # an earlier observation. Several rows per task are expected — repeat
        # checks and different shas accumulate rather than overwrite.
        "create_live_checks",
        """CREATE TABLE IF NOT EXISTS live_checks (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id        INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            sha            TEXT    NOT NULL DEFAULT '',
            outcome        TEXT    NOT NULL DEFAULT 'done',
            probe          TEXT    NOT NULL DEFAULT '',
            observation    TEXT    NOT NULL DEFAULT '',
            reason         TEXT    NOT NULL DEFAULT '',
            recorded_by    INTEGER,
            recorded_agent TEXT    NOT NULL DEFAULT '',
            created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "idx_live_checks_task",
        "CREATE INDEX IF NOT EXISTS idx_live_checks_task ON live_checks(task_id, id)",
    ),
    (
        # First-class task dependencies (#482, epic #478). Until now the order
        # of work lived outside the hub — in a chat, in an agent's memory, in a
        # sentence inside somebody's constraints. Four statements currently
        # carry the words "проверяется глазами, depends_on в хабе нет" (#584,
        # #585, #806, #830), and on 21.08.2026 that gap stopped work already
        # under way: #830 passed DoR, was approved and pair-started before
        # anyone noticed the code it needs sits in an unmerged PR.
        #
        # Columns are named task_id / depends_on_task_id rather than the
        # from/to of the original statement: an edge has a direction, and
        # "from" reads as either end a month later. The name is the only
        # documentation a schema carries into every query written against it.
        #
        # CHECK on the self-edge is a schema invariant, not the cycle
        # detection of #483: a cycle of length one needs no graph walk, and
        # this way it cannot be written even by hand from a SQL prompt.
        "create_task_dependencies",
        """CREATE TABLE IF NOT EXISTS task_dependencies (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id            INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            depends_on_task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (task_id, depends_on_task_id),
            CHECK (task_id != depends_on_task_id)
        )""",
    ),
    (
        # Both directions are walked: "who blocks me" when a task is about to
        # start, "whom do I unblock" when one finishes. One index would make
        # the second walk a table scan.
        "idx_task_dependencies_task",
        "CREATE INDEX IF NOT EXISTS idx_task_dependencies_task "
        "ON task_dependencies(task_id)",
    ),
    (
        "idx_task_dependencies_depends_on",
        "CREATE INDEX IF NOT EXISTS idx_task_dependencies_depends_on "
        "ON task_dependencies(depends_on_task_id)",
    ),
    (
        # Who a message addressed to an AGENT was actually meant for (#821).
        # A fleet under one token shares one agent name, so to_kind='agent' is
        # a broadcast: on 21.08.2026 an answer meant for one session landed in
        # another's inbox saying "AC-1 can be considered closed". The field
        # changes no addressing — every session of that agent still reads the
        # message — it only lets the sender say who it is for, which is what
        # the sender knew and had no way to write down.
        # #837: was this observation checked against what production runs?
        # '' for rows written before the check existed, 'in_prod' when the
        # task's merge was verifiably deployed, 'unknown' when the hub could
        # not tell. The third is stored rather than refused: absence of
        # delivery facts is not a violation, and blocking on it would turn
        # ignorance into a gate.
        "add_live_checks_deploy_state",
        "ALTER TABLE live_checks ADD COLUMN deploy_state TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_agent_messages_for_session",
        "ALTER TABLE agent_messages ADD COLUMN for_session TEXT NOT NULL DEFAULT ''",
    ),
    (
        # What is actually RUNNING, as opposed to what was merged (#839).
        # The hub already records its own merges (pipeline_merges, #534), and
        # a merge was read as delivery — on 21.08.2026 a task sat completed
        # with its PR merged into develop while the deploy job was skipped,
        # because deployment runs from main. "Merged" and "running" had no way
        # to be different facts, so they were the same one, wrongly.
        #
        # status is kept for failed deploys too: a deploy that fell over is
        # evidence about the pipeline, and dropping it would leave the failure
        # looking like it never happened. Readers ask for the last SUCCESSFUL
        # release, so a failure never becomes the state of production.
        "create_releases",
        """CREATE TABLE IF NOT EXISTS releases (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id   INTEGER,
            deployed_sha TEXT    NOT NULL,
            ref          TEXT    NOT NULL DEFAULT '',
            status       TEXT    NOT NULL DEFAULT 'success',
            source       TEXT    NOT NULL DEFAULT '',
            deployed_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        # Reads are always "the newest row for this project", never a scan.
        "idx_releases_project",
        "CREATE INDEX IF NOT EXISTS idx_releases_project ON releases(project_id, id)",
    ),
    (
        # Snapshot of the hypothesis that was answered (#576). Without it,
        # refining outcome_metric after an answer leaves "confirmed" pointing
        # at a different promise. NULL is the legacy default: pre-#576 rows
        # must read as answered, not as revised — the same move as author_kind
        # in #559.
        "add_outcome_answers_hypothesis_snapshot",
        "ALTER TABLE outcome_answers ADD COLUMN hypothesis_snapshot TEXT",
    ),
]


def serialize_str_list(items: list[str] | None) -> str:
    """Serialize list[str] for storage in a TEXT column.

    None or empty list -> '[]' for stable defaults and predictable diffs.
    """
    if not items:
        return "[]"
    return json.dumps(list(items), ensure_ascii=False)


def deserialize_str_list(raw: str | None) -> list[str]:
    """Deserialize list[str] from a TEXT column.

    Returns [] for None, empty string, malformed JSON, or non-list payloads —
    callers can treat the column as "always a list" without guarding.
    """
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("deserialize_str_list: invalid JSON %r, returning []", raw)
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def serialize_risks(risks: list[dict[str, Any]] | None) -> str:
    """Serialize list[dict] (TaskRisk payloads) for storage."""
    if not risks:
        return "[]"
    return json.dumps(list(risks), ensure_ascii=False)


def deserialize_risks(raw: str | None) -> list[dict[str, Any]]:
    """Deserialize list[dict] from TEXT column. Drops non-dict items."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("deserialize_risks: invalid JSON %r, returning []", raw)
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


# --- Pydantic <-> DB mapping for structured task form ---

# All list[str] columns serialized as JSON in TEXT.
LIST_STR_COLUMNS = frozenset(
    {
        "scope_in",
        "scope_out",
        "affected_areas",
        "constraints",
        "assumptions",
        "validation_commands",
        "out_of_scope_for_review",
        "review_checklist",
        "risk_class_reasons",
    }
)

# Subset of Pydantic field names that map 1:1 to DB columns for structured
# task data on tasks table. Ordering kept stable for deterministic SQL.
STRUCTURED_TASK_FIELDS: tuple[str, ...] = (
    "work_type",
    "class_of_service",
    "size",
    "wip_tag",
    "due_date",
    "user_story",
    "problem_statement",
    "business_value",
    "scope_in",
    "scope_out",
    "affected_areas",
    "technical_hints",
    "constraints",
    "assumptions",
    "validation_commands",
    "out_of_scope_for_review",
    "review_checklist",
    "risks",
    "prepared_by",
    "prepared_at",
    # Discovery block (#331). These names carry the read path too:
    # structured_fields_to_db writes whatever the model has, but
    # structured_fields_from_row — and therefore TaskView — only returns
    # what is listed here. A field added to the model and forgotten here
    # is stored and never seen again.
    "outcome_metric",
    "outcome_indicator",
    "outcome_deadline",
    "outcome_revisit_condition",
    "redesign_decision",
    "redesign_rationale",
    "agent_fit",
    # Risk class (#581) is read-path only: it is listed here so
    # structured_fields_from_row / TaskView surface it, but it is
    # deliberately absent from TaskCreate and TaskRefine — the class is
    # derived from observable facts (#582), never declared by the author.
    "risk_class",
    # The features that produced the class (#582): "R3, потому что миграция"
    # can be argued with; a bare "R3" cannot. Read-path only, same as above.
    "risk_class_reasons",
)


def structured_fields_to_db(
    model: Any, *, exclude_unset: bool = False
) -> dict[str, Any]:
    """Convert a TaskCreate or TaskRefine model to DB column kwargs.

    - enums -> their string values (via Pydantic mode='json')
    - list[str] / list[TaskRisk] -> JSON-encoded TEXT
    - acceptance_criteria is intentionally skipped: AC live in their
      own table and are written via the AC CRUD helpers.

    Pass ``exclude_unset=True`` for PATCH-style refine to leave omitted
    fields untouched.
    """
    data = model.model_dump(mode="json", exclude_unset=exclude_unset)
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key == "acceptance_criteria":
            continue
        if key == "project":
            # Virtual refine field (#338): binds an epic to a project via
            # slug in the service layer; there is no 'project' column.
            continue
        if key == "risks":
            if value is None:
                continue
            out["risks"] = serialize_risks(value)
        elif key in LIST_STR_COLUMNS:
            if value is None:
                continue
            out[key] = serialize_str_list(value)
        else:
            out[key] = value
    return out


def structured_fields_from_row(row: Any) -> dict[str, Any]:
    """Extract structured fields from a tasks row into Pydantic-ready dict.

    Inverse of ``structured_fields_to_db``: deserializes JSON columns back
    into Python lists, returns enum values as raw strings (Pydantic will
    coerce to enum on TaskView construction).
    """
    out: dict[str, Any] = {}
    keys = row.keys() if hasattr(row, "keys") else []
    for field in STRUCTURED_TASK_FIELDS:
        if field not in keys:
            continue
        value = row[field]
        if field == "risks":
            out[field] = deserialize_risks(value)
        elif field in LIST_STR_COLUMNS:
            out[field] = deserialize_str_list(value)
        else:
            out[field] = value
    for ts_field in (
        "readiness_score",
        "dor_passed",
        "ready_at",
        "started_at",
        "completed_at",
    ):
        if ts_field in keys:
            value = row[ts_field]
            if ts_field == "dor_passed" and value is not None:
                out[ts_field] = bool(value)
            else:
                out[ts_field] = value
    return out


def ac_to_row_kwargs(ac: Any) -> dict[str, Any]:
    """Convert an AcceptanceCriterion model to DB column kwargs.

    Maps Pydantic ``when``/``then`` to ``when_clause``/``then_clause``
    because WHEN/THEN are SQLite reserved words.
    """
    return {
        "ac_id": ac.id,
        "given": ac.given,
        "when_clause": ac.when,
        "then_clause": ac.then,
        "verifiable_by": ac.verifiable_by.value,
        "test_ref": ac.test_ref,
        # Writing and reading go through different functions, so a field added
        # to one and forgotten in the other is stored and never surfaces —
        # the shape that bit this project in #331. Both sides here.
        "expectation_source": getattr(
            getattr(ac, "expectation_source", None), "value", None
        ),
    }


def row_to_ac_kwargs(row: Any) -> dict[str, Any]:
    """Convert a DB row to kwargs ready for AcceptanceCriterion(**kwargs)."""
    return {
        "id": row["ac_id"],
        "given": row["given"],
        "when": row["when_clause"],
        "then": row["then_clause"],
        "verifiable_by": row["verifiable_by"],
        "test_ref": row["test_ref"],
        "expectation_source": (
            row["expectation_source"] if "expectation_source" in row.keys() else None
        ),
    }


async def _column_exists(db: aiosqlite.Connection, table: str, column: str) -> bool:
    """Check if a column exists in a table via PRAGMA table_info."""
    rows = await db.execute_fetchall(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in rows)


async def _migrate(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))"
    )
    applied = {
        row[0] for row in await db.execute_fetchall("SELECT name FROM _migrations")
    }
    for name, sql in _MIGRATIONS:
        if name not in applied:
            if sql.startswith("ALTER TABLE") and "ADD COLUMN" in sql:
                parts = sql.split()
                table = parts[2]
                col = parts[5]
                if await _column_exists(db, table, col):
                    await db.execute(
                        "INSERT OR IGNORE INTO _migrations (name) VALUES (?)",
                        (name,),
                    )
                    continue
            # Strict-by-default: a failed migration MUST NOT be marked as
            # applied, otherwise the next start silently skips it and the
            # schema stays diverged forever (review I2). Re-raise so the
            # operator sees the failure at boot.
            try:
                await db.execute(sql)
            except Exception:
                log.exception("Migration %s failed", name)
                await db.rollback()
                raise
            await db.execute(
                "INSERT OR IGNORE INTO _migrations (name) VALUES (?)", (name,)
            )
    await db.commit()

    if await _table_exists(db, "proposals"):
        await _migrate_proposals(db)


async def _table_exists(db: aiosqlite.Connection, table: str) -> bool:
    rows = await db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return len(rows) > 0


async def _migrate_proposals(db: aiosqlite.Connection) -> None:
    """Migrate old proposals table into tasks with source='agent'."""
    rows = await db.execute_fetchall("SELECT * FROM proposals")
    for r in rows:
        d = dict(r)
        status_map = {"pending": "draft", "approved": "open", "rejected": "rejected"}
        new_status = status_map.get(d.get("status", ""), "draft")
        existing = await db.execute_fetchall(
            "SELECT id FROM tasks WHERE title=? AND source='agent' AND description=?",
            (d["title"], d.get("description", "")),
        )
        if existing:
            continue
        await db.execute(
            "INSERT INTO tasks (title, description, status, source, assigned_agent, rationale, created_at, updated_at) "
            "VALUES (?, ?, ?, 'agent', ?, ?, ?, ?)",
            (
                d["title"],
                d.get("description", ""),
                new_status,
                d.get("agent", ""),
                d.get("rationale", ""),
                d.get("created_at", ""),
                d.get("updated_at", ""),
            ),
        )
    await db.execute("DROP TABLE IF EXISTS proposals")
    await db.commit()


async def get_db() -> aiosqlite.Connection:
    HUB_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(HUB_DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await db.executescript(_SCHEMA)
    await _migrate(db)
    await _fix_orphaned_parents(db)
    from hub.services.lifecycle import repair_stale_parent_completions

    await repair_stale_parent_completions(db)
    if await _table_exists(db, "roles"):
        await seed_system_roles(db)
    if await _table_exists(db, "projects"):
        await seed_default_project(db)
    if await _table_exists(db, "skills"):
        await seed_default_skills(db)
    return db


async def _fix_orphaned_parents(db: aiosqlite.Connection) -> None:
    """Nullify parent_id references that point to nonexistent tasks."""
    orphans = await db.execute_fetchall(
        "SELECT t.id FROM tasks t "
        "LEFT JOIN tasks p ON t.parent_id = p.id "
        "WHERE t.parent_id IS NOT NULL AND p.id IS NULL",
    )
    if orphans:
        ids = [r[0] for r in orphans]
        log.warning("Fixing %d orphaned parent_id references: %s", len(ids), ids)
        for oid in ids:
            await db.execute("UPDATE tasks SET parent_id=NULL WHERE id=?", (oid,))
        await db.commit()


async def validate_hierarchy(
    db: aiosqlite.Connection,
    task_type: str,
    parent_id: int | None,
) -> str | None:
    """Validate parent-child relationship. Returns error message or None if valid."""
    from hub.models import HIERARCHY_RULES, TaskType

    tt = TaskType(task_type)
    required_parent = HIERARCHY_RULES[tt]

    if tt == TaskType.epic:
        if parent_id is not None:
            return "Epics cannot have a parent"
        return None

    if required_parent is None:
        if parent_id is not None:
            return f"{task_type} cannot have a parent"
        return None

    if tt == TaskType.task and parent_id is None:
        return None

    if parent_id is None:
        return f"{task_type} requires a parent of type {required_parent.value}"

    rows = await db.execute_fetchall(
        "SELECT task_type FROM tasks WHERE id=?", (parent_id,)
    )
    if not rows:
        return f"Parent task #{parent_id} not found"
    parent_type = rows[0][0]

    if tt == TaskType.task and parent_type in (
        TaskType.feature.value,
        TaskType.epic.value,
    ):
        return None
    if parent_type != required_parent.value:
        return f"{task_type} requires parent of type {required_parent.value}, got {parent_type}"
    return None


async def get_breadcrumb(
    db: aiosqlite.Connection,
    task_id: int,
) -> list[dict[str, Any]]:
    """Walk up the parent chain and return breadcrumb list (root first)."""
    crumbs: list[dict[str, Any]] = []
    current_id: int | None = task_id
    seen: set[int] = set()
    while current_id is not None:
        if current_id in seen:
            break
        seen.add(current_id)
        rows = await db.execute_fetchall(
            "SELECT id, title, task_type, parent_id FROM tasks WHERE id=?",
            (current_id,),
        )
        if not rows:
            break
        row = dict(rows[0])
        crumbs.append(
            {
                "id": row["id"],
                "title": row["title"],
                "task_type": row["task_type"],
            }
        )
        current_id = row["parent_id"]
    crumbs.reverse()
    return crumbs


async def get_children(
    db: aiosqlite.Connection,
    task_id: int,
) -> list[dict[str, Any]]:
    """Get direct children of a task, ordered by position then id."""
    rows = await db.execute_fetchall(
        "SELECT id, title, task_type, status, priority FROM tasks "
        "WHERE parent_id=? AND archived=0 ORDER BY position ASC, id ASC",
        (task_id,),
    )
    return [dict(r) for r in rows]


async def get_progress(
    db: aiosqlite.Connection,
    task_id: int,
) -> dict[str, int]:
    """Calculate progress for a parent task based on direct children."""
    from hub.models import ACTIVE_STATUSES

    rows = await db.execute_fetchall(
        "SELECT status FROM tasks WHERE parent_id=? AND archived=0", (task_id,)
    )
    total = len(rows)
    if total == 0:
        return {"total": 0, "completed": 0, "failed": 0, "active": 0, "percent": 0}

    completed = sum(1 for r in rows if r[0] == "completed")
    failed = sum(1 for r in rows if r[0] in ("failed", "rejected"))
    active = sum(1 for r in rows if r[0] in {s.value for s in ACTIVE_STATUSES})
    percent = round(completed * 100 / total) if total > 0 else 0

    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "active": active,
        "percent": percent,
    }


async def build_tree(
    db: aiosqlite.Connection,
    task_id: int,
) -> dict[str, Any] | None:
    """Build a recursive tree from a task downward."""
    rows = await db.execute_fetchall(
        "SELECT id, title, task_type, status, priority, assigned_agent FROM tasks WHERE id=?",
        (task_id,),
    )
    if not rows:
        return None

    task = dict(rows[0])
    children_rows = await db.execute_fetchall(
        "SELECT id FROM tasks WHERE parent_id=? AND archived=0 "
        "ORDER BY position ASC, id ASC",
        (task_id,),
    )
    children = []
    for cr in children_rows:
        child_tree = await build_tree(db, cr[0])
        if child_tree:
            children.append(child_tree)

    progress = await get_progress(db, task_id) if children else None
    return {
        "id": task["id"],
        "title": task["title"],
        "task_type": task["task_type"],
        "status": task["status"],
        "priority": task["priority"],
        "assigned_agent": task.get("assigned_agent", ""),
        "progress": progress,
        "children": children,
    }


async def log_activity(
    db: aiosqlite.Connection, kind: str, summary: str, detail: str | None = None
) -> None:
    await db.execute(
        "INSERT INTO activity_log (kind, summary, detail) VALUES (?, ?, ?)",
        (kind, summary, detail),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Admin section: system roles, permissions, seed
# ---------------------------------------------------------------------------

ALL_PERMISSIONS: tuple[str, ...] = (
    "admin.read",
    "admin.users.write",
    "admin.agents.write",
    "admin.roles.write",
    "admin.credentials.write",
    "admin.audit.read",
    "tasks.read",
    "tasks.create",
    "tasks.refine",
    "tasks.update",
    "tasks.human_gate",
    "tasks.agent_report",
    "tasks.decision",
    "tasks.archive",
    "tasks.delete",
    # #546: report the outcome of a run, and nothing else. Deliberately absent
    # from the agent and human defaults in config.py: a token that lives in a
    # CI secret must not be able to move a task or write a verdict, so it can
    # only be granted through a DB-backed principal holding the ci_runner role.
    "tasks.ci_report",
    # #495: record what was deployed, and nothing else. Same reasoning as
    # tasks.ci_report above — this token lives in a GitHub secret, so it gets
    # the one verb it needs. Separate from ci_report because the facts are
    # different: one is "the tests ran on this commit", the other is "this
    # commit is what production is running".
    "deploys.record",
    "integrations.vast.manage",
    "system.settings.write",
)

# #614: which of the permissions above actually decide anything.
#
# Handing a permission out in a role, showing it in the admin UI, and checking
# it in code are three different things, and until now only the first two were
# visible. Nine of the eighteen were consulted by nothing at all — so a role
# looked narrow while its narrowness was decorative. That is not an open door
# (human gates are held by require_human_or_admin, _reject_agent_authored_source
# and the review gate, not by these strings), but it is a promise the system
# does not keep: in #613 the ci_runner role was described to the owner as unable
# to do anything but report, and the CI token could in fact file drafts, because
# tasks.create is asked by nobody.
#
# Kept rather than deleted: the vocabulary is worth having when a gate is
# actually wanted. Enforcing any of these is a separate decision with its own
# blast radius — enforcing tasks.create today would break #611, whose audit
# reporter files drafts with exactly the ci_runner role.
#
# tests/test_auth.py derives the real answer FROM THE CODE and compares it with
# these two tuples, failing in both directions: a permission missing from both,
# and a "declared only" permission that has started to gate something. Two
# hand-written lists agreeing with each other and both wrong is the defect this
# task exists to remove.
ENFORCED_PERMISSIONS: tuple[str, ...] = (
    "admin.read",
    "admin.users.write",
    "admin.roles.write",
    "admin.credentials.write",
    "admin.audit.read",
    "tasks.archive",
    "tasks.delete",
    "tasks.ci_report",
    "deploys.record",
    # Consulted indirectly: config.py reads it to answer is_human, and
    # require_human_or_admin is built on that — so it does gate, via one hop.
    "tasks.human_gate",
)

DECLARED_ONLY_PERMISSIONS: tuple[str, ...] = (
    "admin.agents.write",
    "tasks.read",
    "tasks.create",
    "tasks.refine",
    "tasks.update",
    "tasks.agent_report",
    "tasks.decision",
    "integrations.vast.manage",
    "system.settings.write",
)


SYSTEM_ROLES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "super_admin",
        "Super Admin",
        "Full access, bootstrap-only, cannot be deleted",
        ALL_PERMISSIONS,
    ),
    (
        "admin",
        "Admin",
        "Manage users, agents, keys, settings",
        (
            "admin.read",
            "admin.users.write",
            "admin.agents.write",
            "admin.roles.write",
            "admin.credentials.write",
            "admin.audit.read",
            "tasks.read",
            "tasks.create",
            "tasks.refine",
            "tasks.update",
            "tasks.human_gate",
            "tasks.decision",
            "tasks.archive",
            "tasks.delete",
            "integrations.vast.manage",
        ),
    ),
    (
        "operator",
        "Operator",
        "Human gates, task dispatch, decisions, force-complete",
        (
            "tasks.read",
            "tasks.create",
            "tasks.refine",
            "tasks.update",
            "tasks.human_gate",
            "tasks.decision",
            "tasks.archive",
            "tasks.delete",
            "integrations.vast.manage",
        ),
    ),
    (
        "developer",
        "Developer",
        "Create and manage tasks without admin access",
        (
            "tasks.read",
            "tasks.create",
            "tasks.refine",
            "tasks.update",
            "tasks.archive",
        ),
    ),
    (
        "viewer",
        "Viewer",
        "Read-only dashboard and tasks",
        ("tasks.read",),
    ),
    (
        "agent",
        "Agent",
        "AI agent: propose, update, question, report done",
        (
            "tasks.read",
            "tasks.create",
            "tasks.refine",
            "tasks.update",
            "tasks.agent_report",
        ),
    ),
    (
        "reviewer_agent",
        "Reviewer Agent",
        "Agent with review/report permissions",
        (
            "tasks.read",
            "tasks.create",
            "tasks.refine",
            "tasks.update",
            "tasks.agent_report",
        ),
    ),
    (
        "security_admin",
        "Security Admin",
        "Audit and security settings only",
        ("admin.read", "admin.audit.read"),
    ),
    (
        # #546: the identity a CI runner authenticates as. Read a task's plan,
        # report what the run produced — that is the whole job. No update, no
        # agent_report, no human gate: this token lives in a GitHub secret, so
        # its blast radius is the one thing it needs to do.
        "ci_runner",
        "CI Runner",
        "Read tasks and report run results; cannot change task state",
        ("tasks.read", "tasks.ci_report", "deploys.record"),
    ),
)


async def seed_default_project(db: aiosqlite.Connection) -> None:
    """Seed the 'default' project from env and backfill epics (#335).

    Idempotent: an existing default row is reused, and only epics without a
    project are assigned. Until consumers land (V1.2+), this changes no
    behavior — it only gives every existing task a resolvable project.
    """
    from hub import config as cfg

    rows = await db.execute_fetchall("SELECT id FROM projects WHERE slug='default'")
    if rows:
        default_id = rows[0][0]
    else:
        cur = await db.execute(
            "INSERT INTO projects (slug, name, repo, workspace_path, "
            "default_branch) VALUES ('default', 'Default', ?, ?, ?)",
            (
                cfg.REPO_NAME,
                str(cfg.WORKSPACE_REPO_LINK),
                cfg.PAIR_BASE_BRANCH,
            ),
        )
        default_id = cur.lastrowid
    await db.execute(
        "UPDATE tasks SET project_id=? WHERE task_type='epic' AND project_id IS NULL",
        (default_id,),
    )
    await db.commit()


MULTI_AGENT_REVIEW_SKILL = """\
# Multi-agent review harness (v1)

Прогони ревью диффа через несколько независимых агентов по схеме
«измерения → адверсариальная верификация → единогласие».

## Фаза 1 — ревьюверы по измерениям (параллельно, по одному агенту на роль)
Каждому: узкий мандат, контекст задачи (постановка + ограничения из хаба),
схема ответа findings[] = {title, file, line, severity: high|medium|low,
detail, category}.

1. security — только реально эксплуатируемое: инъекции, XSS, обход
   авторизации/гейтов, утечки. Запрещено флагать паттерн, который кодовая
   база уже принимает, если дифф не делает хуже.
2. correctness — реальные баги с конкретным сценарием отказа: краевые
   значения, семантика пустых/отсутствующих полей, гонки, потеря состояния.
   Требование: «опиши вход и наблюдаемый неправильный результат».
3. consistency — сравнение с конвенциями ЭТОГО репозитория, не с
   абстрактными best practices; нарушение ограничений из постановки —
   всегда находка.
4. tests — только пробелы, при которых регрессия проходит зелёной; не
   требовать тестов на поведение фреймворка.

Всем: «Верни только находки, которые готов защищать перед автором.
Каждая привязана к file:line. Лучше 2 настоящих, чем 10 предположительных».

## Фаза 2 — адверсариальная верификация (на КАЖДУЮ находку, 2 агента)
Без барьера: находки измерения уходят на проверку сразу.
- Опровергатель: «Попробуй ОПРОВЕРГНУТЬ: прочитай реальный код, проверь
  достижимость и вред, не принят ли паттерн в репо. По умолчанию
  refuted=true, если вред спекулятивен, недостижим или конвенционален».
- Валидатор: «Независимо воспроизведи рассуждение по коду. refuted=true,
  если не можешь указать точные строки, делающие проблему реальной».
Схема ответа: {refuted: bool, reasoning}.
Находка подтверждена ТОЛЬКО единогласно (оба refuted=false).

## Фаза 3 — итог
confirmed[] (severity, file:line, detail, цитаты голосов), rejected[]
(title + причина), строка «N сырых → K подтверждено, M опровергнуто».
Отклонённые не выбрасывать: иногда это долг вне скоупа диффа.

## Правила
- Агенты работают с реальными файлами и командами, не с пересказом диффа.
- Конвенция репо сильнее общего best practice.
- Находок > ~20 — дедупликация по file+суть между фазами.
- После прогона: исправить confirmed, прогнать тесты, сдать отчёт в хаб
  (machine-review), затем submit_for_review.
"""


MACHINE_REVIEW_CYCLE_SKILL = """\
# Machine-review cycle (v1) — контракт для любого агента-клиента

Как выполнить machine-review задачи в OpenClaw Hub без контекста чужих
сессий. Оркестратор любой (Claude Code Workflow, Cursor, свой скрипт) —
контракт один.

## Когда обязателен
Политика (#382): каскад override задачи > политика проекта (off|auto|always)
> автоправила (docs/chore/spike и размеры XS/S — нет; refactor и
feature/bug M+ — да; риск high или security — всегда). Хаб сам сообщает:
`lifecycle_hint` в ответе `hub_submit_for_review`, предупреждение в панели
ревью, событие `machine_review_requested` в фиде. Режим
OPENCLAW_MACHINE_REVIEW=require блокирует человеческий вердикт без
актуального отчёта; дефолт warn — только предупреждает.

## Шаги
1. `hub_get_skill("multi-agent-review")` — актуальная версия промта-харнесса
   (измерения → пара опровергатель+валидатор на находку → единогласие).
2. Прогнать харнесс над диффом задачи СВОИМ оркестратором: измерения
   параллельно, на каждую находку два независимых верификатора
   («default to refuted»), подтверждение только единогласное.
3. Исправить confirmed-находки, прогнать тесты заново (exit code проверять
   отдельным echo, не через пайп).
4. `hub_submit_machine_review(task_id, raw_count, incomplete,
   findings_confirmed, findings_rejected, unresolved, lost_dimensions,
   harness_skill, harness_version, agent_count, tokens_spent, duration_ms,
   orchestrator, model)` — метрики опциональны, но токены/время питают
   экономику практики (#384). Отчёт привязывается к текущему
   submission_generation: пересдача работы делает его stale.

   `incomplete` **обязателен и без дефолта** (#549): пропуск даёт 422. Молча
   подставленный `false` — это то, как прогон с умершими агентами читается
   как чистый. `unresolved` — находки, которые никто не смог рассудить; они
   НЕ идут в `findings_rejected`, потому что «никто не голосовал» и «кто-то
   опроверг» — противоположные исходы. `lost_dimensions` — измерения, не
   вернувшие результат.
5. `hub_submit_for_review` — человеческий вердикт остаётся финальным гейтом;
   отчёт его информирует, не заменяет.

## Формат находок
confirmed: {title, severity high|medium|low, category slug, file, line,
detail}; rejected: {title, category, reason}; unresolved: {title, why}.
category питает метрики повторяемости — используй устойчивые слаги
(security, correctness, consistency, tests, …).

Объяснение у `unresolved` называется `why`, а не `reason`: `reason`
принадлежит `findings_rejected`. Лишние ключи отвергаются, а не отбрасываются
молча (#553) — перепутанное имя раньше давало сохранённую находку с пустым
объяснением.
"""


async def seed_default_skills(db: aiosqlite.Connection) -> None:
    """Seed built-in skills (#380, #383).

    Idempotent per skill: only inserts when the name has no versions at
    all, so operator edits and newer versions are never overwritten.
    """
    seeds = (
        (
            "multi-agent-review",
            "prompt",
            MULTI_AGENT_REVIEW_SKILL,
            '["review", "quality", "workflow"]',
        ),
        (
            "machine-review-cycle",
            "skill",
            MACHINE_REVIEW_CYCLE_SKILL,
            '["review", "workflow", "contract"]',
        ),
    )
    for name, kind, content, tags in seeds:
        rows = await db.execute_fetchall(
            "SELECT id FROM skills WHERE name=? LIMIT 1", (name,)
        )
        if rows:
            continue
        await db.execute(
            "INSERT INTO skills (name, kind, version, content, tags, status, "
            "created_by) VALUES (?, ?, 1, ?, ?, 'active', 'seed')",
            (name, kind, content, tags),
        )
    await db.commit()


async def seed_system_roles(db: aiosqlite.Connection) -> None:
    """Ensure system roles and their permissions exist.

    Idempotent: skips roles that already exist, adds missing permissions.
    """
    for slug, name, description, permissions in SYSTEM_ROLES:
        rows = await db.execute_fetchall("SELECT id FROM roles WHERE slug = ?", (slug,))
        if rows:
            role_id = rows[0][0]
        else:
            cursor = await db.execute(
                "INSERT INTO roles (slug, name, description, system) VALUES (?, ?, ?, 1)",
                (slug, name, description),
            )
            role_id = cursor.lastrowid
        for perm in permissions:
            await db.execute(
                "INSERT OR IGNORE INTO role_permissions (role_id, permission) VALUES (?, ?)",
                (role_id, perm),
            )
    await db.commit()


async def has_active_admin(db: aiosqlite.Connection) -> bool:
    """Check if at least one active principal with admin or super_admin role exists."""
    rows = await db.execute_fetchall(
        """SELECT 1 FROM principals p
           JOIN principal_roles pr ON p.id = pr.principal_id
           JOIN roles r ON pr.role_id = r.id
           WHERE p.status = 'active'
             AND r.slug IN ('super_admin', 'admin')
           LIMIT 1"""
    )
    return len(rows) > 0
