from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from collections.abc import Iterable
from typing import Any

import aiosqlite

from hub.config import CHAT_PAIR_AGENT, HUB_DB_PATH

log = logging.getLogger("hub.db")

# Сколько писатель ждёт занятую базу, прежде чем сдаться (#1065). По умолчанию
# SQLite не ждёт вовсе: конкурентная запись сразу возвращает "database is
# locked". Пока соединение было одно на процесс, конкурировать было некому —
# очередь держал asyncio.Lock в коде. С соединением на запрос очередь держит
# сама база, и ей нужно сказать, что ожидание законно.
BUSY_TIMEOUT_MS = 5000


async def fetchall(
    db: aiosqlite.Connection, sql: str, parameters: Iterable[Any] = ()
) -> list[aiosqlite.Row]:
    """Выполнить запрос и вернуть строки СПИСКОМ.

    aiosqlite объявляет execute_fetchall как ``Iterable[Row]``, хотя отдаёт
    список. Код хаба повсеместно индексирует результат и возвращает его как
    ``list[Row]``, то есть опирается на реализацию, а не на контракт
    библиотеки. Один list здесь дешевле, чем полторы сотни мест, каждое из
    которых держится на этом допущении молча (#847).
    """
    return list(await db.execute_fetchall(sql, parameters))


def inserted_id(cursor: aiosqlite.Cursor) -> int:
    """id строки, которую только что вставили.

    sqlite3 объявляет lastrowid как Optional — у курсора не-INSERT его нет, —
    поэтому вызывающий выбирал между враньём в аннотации и подавлением. Здесь
    отсутствие id значит, что вставка не состоялась, и это сказано вслух, а не
    уехало дальше нулём или None (#847).
    """
    if cursor.lastrowid is None:
        raise RuntimeError("INSERT не вернул rowid")
    return cursor.lastrowid


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
    # Who decided agents should READ this version, as opposed to who wrote it
    # (#1028). Activation is a human gate (#380) and leaves ``created_by``
    # untouched, so without this column a person activating a seeded draft is
    # indistinguishable from the seed itself — and the next deploy would
    # replace their decision believing it was its own.
    (
        "add_skills_activated_by_column",
        "ALTER TABLE skills ADD COLUMN activated_by TEXT NOT NULL DEFAULT ''",
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
    # HAIPLANE_REVIEW_SELF_APPROVE=allow stay distinguishable in hindsight.
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
        # #950: which release carried this merge out. Written at release-merge
        # time, because that is the only moment the fact is cheap and certain:
        # a release takes the base branch whole (#812), so every unreleased
        # merge of the project is carried by it. Read when ancestry cannot
        # answer — a squash release and a recreated base branch both cut the
        # line, and delivery_state was left saying "waiting for a release"
        # about code that was running (#949's live check, refused at #3496).
        "add_pipeline_merges_released_pr",
        "ALTER TABLE pipeline_merges ADD COLUMN released_pr INTEGER",
    ),
    (
        "add_pipeline_merges_released_sha",
        "ALTER TABLE pipeline_merges ADD COLUMN released_sha TEXT",
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
    (
        # What a machine-review finding turned out to be (#876, feature #871).
        # Until now the only measure of review quality was the review itself:
        # findings_confirmed / findings_rejected is one run's own adjudication,
        # and filtration_rate divides one by the other. Nobody recorded what
        # happened to a finding AFTER the gate, so precision by profile and by
        # model could not be computed at all, and tokens_per_confirmed priced
        # findings that may never have been fixed.
        #
        # Keyed by (review_id, finding_index): a report is immutable, so the
        # position in findings_confirmed identifies the finding. The title is
        # snapshotted beside it — a row that survives its report must still be
        # readable by a person, and the index alone is not.
        #
        # No default disposition, and NO BACKFILL for existing reports: "not
        # stated" is an answer, and writing one in for rows nobody judged is
        # exactly the substitution #549 exists to prevent.
        "create_finding_dispositions",
        """CREATE TABLE IF NOT EXISTS finding_dispositions (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id             INTEGER NOT NULL,
            task_id               INTEGER NOT NULL,
            submission_generation INTEGER NOT NULL,
            finding_index         INTEGER NOT NULL,
            finding_title         TEXT    NOT NULL DEFAULT '',
            disposition           TEXT    NOT NULL,
            note                  TEXT    NOT NULL DEFAULT '',
            decided_by            TEXT    NOT NULL DEFAULT '',
            decided_at            TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        # One disposition per finding: a second pass over the same gate
        # corrects the first rather than stacking a contradictory row beside
        # it. The reads are always "everything judged in this report".
        "idx_finding_dispositions_unique",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_finding_dispositions_unique "
        "ON finding_dispositions(review_id, finding_index)",
    ),
    (
        # The finding's own identity beside the slot it occupied (#1007).
        # NOT backfilled: rows filed before this column were addressed by
        # position and are still readable that way, and computing an id for
        # them now would claim they were judged under a scheme that did not
        # exist. The conflict target stays (review_id, finding_index) so those
        # rows keep being corrected in place rather than duplicated.
        "add_finding_uid_to_dispositions",
        "ALTER TABLE finding_dispositions ADD COLUMN finding_uid TEXT NOT NULL "
        "DEFAULT ''",
    ),
    (
        # A finding class that keeps coming back, and the check that ended it
        # (#878, feature #871). ``recurring_categories`` has counted repeats
        # since #384 and closed nothing: a class found in three tasks is still
        # hunted by a model, at full price, on the fourth.
        #
        # This is the only lever in the review economics that improves
        # monotonically. A defect turned into a lint rule or a CI check costs
        # zero tokens forever after; every other saving here is a one-off.
        #
        # ``check_ref`` is NOT NULL and refused when blank at the call site: a
        # category closed by a tick rather than by a named check is a category
        # nobody covered, and the debt list would shrink while the bill stayed
        # exactly where it was.
        "create_category_checks",
        """CREATE TABLE IF NOT EXISTS category_checks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            category    TEXT    NOT NULL,
            check_ref   TEXT    NOT NULL,
            note        TEXT    NOT NULL DEFAULT '',
            recorded_by TEXT    NOT NULL DEFAULT '',
            recorded_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        # One live check per category: recording a better one replaces the
        # claim rather than leaving two answers to "is this covered?".
        "idx_category_checks_unique",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_category_checks_unique "
        "ON category_checks(category)",
    ),
    (
        # Which deterministic checks ran on this commit, and how they ended
        # (#875, feature #870). JSON object: step name → pass | fail | skipped.
        #
        # Until now the hub learned only whether the TASK's own
        # validation_commands passed. The repository's toolchain — ruff, the
        # formatter, mypy, pytest, bandit, pip-audit — ran in CI and stopped
        # there, so the reviewer kept hunting, at model prices and with the
        # false positives that class is famous for, the exact defects a linter
        # had already proven absent minutes earlier.
        #
        # '{}' rather than NULL: an empty object says "this report named no
        # checks", which is what every report written before this column did.
        # It grants nothing, which is the point — see prepass_state.
        "add_ci_run_reports_checks",
        "ALTER TABLE ci_run_reports ADD COLUMN checks TEXT NOT NULL DEFAULT '{}'",
    ),
    (
        # Defect passport (#909, epic #900): the stage a defect was caught at.
        #
        # Until now the answer was reconstructed — ``escaped_defects`` infers a
        # leak from the ``completed_at`` of the nearest feature ancestor and says
        # in its own docstring that the result is mostly a measurement of which
        # fields got filled in. A reconstruction cannot be contradicted; a
        # recorded fact can.
        #
        # The default is 'unknown' and it is a real answer, not a placeholder to
        # be cleaned up later: the 69 bugs already on production were caught
        # somewhere nobody wrote down, and back-filling them from dates would
        # manufacture exactly the kind of number this column exists to replace.
        "add_found_in_column",
        "ALTER TABLE tasks ADD COLUMN found_in TEXT NOT NULL DEFAULT 'unknown'",
    ),
    (
        # The change this defect came from (#909). Nullable on purpose: an
        # unattributed defect is the normal state until somebody confirms the
        # link, and an empty column says that out loud. A guess written here
        # would read as a finding.
        "add_caused_by_task_id_column",
        "ALTER TABLE tasks ADD COLUMN caused_by_task_id INTEGER REFERENCES tasks(id)",
    ),
    (
        # When the defect was noticed and when it stopped hurting (#909).
        # Both are NULL until somebody records them, and neither is ever
        # derived from updated_at — that substitution is the defect #810
        # removed from cycle time, and repeating it here would put a median
        # on the metrics page that no fix could move.
        "add_detected_at_column",
        "ALTER TABLE tasks ADD COLUMN detected_at TEXT",
    ),
    (
        "add_resolved_at_column",
        "ALTER TABLE tasks ADD COLUMN resolved_at TEXT",
    ),
    (
        # Reads are "defects caught at stage X in this window" and "defects
        # blamed on change Y" — both scans without an index.
        "idx_tasks_found_in",
        "CREATE INDEX IF NOT EXISTS idx_tasks_found_in ON tasks(found_in)",
    ),
    (
        "idx_tasks_caused_by",
        "CREATE INDEX IF NOT EXISTS idx_tasks_caused_by ON tasks(caused_by_task_id)",
    ),
    (
        # Where a completed task's work actually stands (#897). On 21.08.2026
        # #878 and #885 sat ``completed`` for two hours with their PRs open:
        # the gate refused to deliver (CI still running), a human accepted the
        # task — a legitimate way out — and after that nobody owned the PR.
        # It was found by hand, comparing open PRs against the board.
        #
        # Answers are STORED, not recomputed on read, and that is the point:
        # the question costs a call to GitHub, and a card that pays it on every
        # render pays it hundreds of times for a fact that changes twice a day
        # (the same trade #883 made for deploys). The periodic sweep writes
        # here; every reader — inbox, REST, MCP — reads only this table.
        #
        # ``state`` is one of delivered | pr_open | pr_closed | unknown, and
        # ``reason`` is never empty for ``unknown``: "could not look" is an
        # answer with a cause, never a quiet "no" (#546, #572, #725).
        # ``disposition`` is what the owner said should happen to the PR when
        # they accepted the task by hand — a declaration, not a fact, and it
        # never removes a row: the list is driven by what the PR IS doing.
        "create_delivery_discrepancies",
        # ON DELETE CASCADE, not a bare reference: ``delete_task_subtree``
        # clears ``task_updates`` by hand and knows nothing about this table,
        # so a plain foreign key would have made deleting a task with an open
        # discrepancy fail outright. A note about a task that no longer exists
        # has nothing to say anyway.
        """CREATE TABLE IF NOT EXISTS delivery_discrepancies (
            task_id       INTEGER PRIMARY KEY REFERENCES tasks(id)
                          ON DELETE CASCADE,
            pr_number     INTEGER,
            state         TEXT    NOT NULL,
            reason        TEXT    NOT NULL DEFAULT '',
            delivery_path TEXT    NOT NULL DEFAULT '',
            disposition   TEXT    NOT NULL DEFAULT '',
            accepted_via  TEXT    NOT NULL DEFAULT '',
            alerted_state TEXT    NOT NULL DEFAULT '',
            first_seen_at TEXT    NOT NULL DEFAULT (datetime('now')),
            checked_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        # What each submission actually pinned (#880, feature #872).
        # ``tasks.submission_sha`` is OVERWRITTEN on every resubmission, so the
        # commit a previous generation was reviewed against existed nowhere
        # queryable — only as prose in a task update ("Branch tip at
        # submission: …"), which is not a source of truth. Without this ledger
        # a second review has nothing to diff against and pays for the whole
        # branch again, every round of fixes.
        #
        # base_branch travels with the sha because the delta is only valid
        # while the base is the same one: a project that repointed its default
        # branch makes "what changed since last time" a different question.
        #
        # UNIQUE(task_id, generation): one commit per submission. A resubmit
        # that somehow reuses a generation corrects the row rather than leaving
        # two answers to "what was reviewed then".
        "create_submissions",
        """CREATE TABLE IF NOT EXISTS submissions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            generation   INTEGER NOT NULL,
            sha          TEXT    NOT NULL DEFAULT '',
            base_branch  TEXT    NOT NULL DEFAULT '',
            submitted_at TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (task_id, generation)
        )""",
    ),
    (
        # Chat-pair (#961): a one-time code pasted into a chat, exchanged for a
        # short session. Deliberately NOT ``api_keys``: those live for days by
        # design, and stretching them down to minutes would have made every
        # other key's expiry a matter of interpretation.
        #
        # Only the hash is stored, and there is nowhere to put anything else —
        # the secret's privacy is a property of the schema, not a rule someone
        # has to remember. UNIQUE on the hash so one code cannot exist twice.
        "create_chat_pair_codes",
        """CREATE TABLE IF NOT EXISTS chat_pair_codes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            principal_id INTEGER NOT NULL REFERENCES principals(id)
                         ON DELETE CASCADE,
            code_hash    TEXT    NOT NULL UNIQUE,
            expires_at   TEXT    NOT NULL,
            redeemed_at  TEXT,
            created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "create_chat_pair_sessions",
        """CREATE TABLE IF NOT EXISTS chat_pair_sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            principal_id INTEGER NOT NULL REFERENCES principals(id)
                         ON DELETE CASCADE,
            token_hash   TEXT    NOT NULL UNIQUE,
            expires_at   TEXT    NOT NULL,
            revoked_at   TEXT,
            created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "idx_chat_pair_codes_principal",
        "CREATE INDEX IF NOT EXISTS idx_chat_pair_codes_principal "
        "ON chat_pair_codes(principal_id)",
    ),
    (
        "idx_chat_pair_sessions_principal",
        "CREATE INDEX IF NOT EXISTS idx_chat_pair_sessions_principal "
        "ON chat_pair_sessions(principal_id)",
    ),
    (
        # #957: a declared wait — WHAT the task is waiting for and UNTIL when.
        # The stale watchdog measured silence (the age of updated_at) and so
        # could not tell a task honestly waiting a day for its outcome check
        # (#927) from one abandoned for a week (#443). A wait is a claim, so
        # it is declared with a deadline and with a name on it: an open-ended
        # "waiting" would just be a legal way to go silent. The three columns
        # ship together.
        "add_tasks_waiting_for",
        "ALTER TABLE tasks ADD COLUMN waiting_for TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_tasks_waiting_until",
        "ALTER TABLE tasks ADD COLUMN waiting_until TEXT",
    ),
    (
        "add_tasks_waiting_declared_by",
        "ALTER TABLE tasks ADD COLUMN waiting_declared_by TEXT NOT NULL DEFAULT ''",
    ),
    (
        # Did the report's author review their own work (#728)? Decided from
        # the AUTHENTICATED identity at submission, never from submitted_by —
        # that field is written as `body.agent or identity.username`, i.e. the
        # caller names itself. Rows written before this column say 0, which
        # means "never established", not "independent": the question was not
        # asked then, and back-filling a verdict onto them would be inventing
        # evidence.
        "add_machine_reviews_self_reviewed",
        "ALTER TABLE machine_reviews ADD COLUMN self_reviewed INTEGER NOT NULL "
        "DEFAULT 0",
    ),
    (
        # Pair-start git location (#975). ``hub`` (default) is today's laptop
        # path: the hub host prepares and later restores the task branch.
        # ``remote`` records the canonical name and never touches git on the
        # hub host — the caller creates the branch in its own clone.
        "add_tasks_git_mode",
        "ALTER TABLE tasks ADD COLUMN git_mode TEXT NOT NULL DEFAULT 'hub'",
    ),
    (
        # Chat-pair implementer (#980): sibling kind on the same code table.
        # Intake rows stay kind=intake / bound_task_id NULL.
        "add_chat_pair_codes_kind",
        "ALTER TABLE chat_pair_codes ADD COLUMN kind TEXT NOT NULL DEFAULT 'intake'",
    ),
    (
        "add_chat_pair_codes_bound_task_id",
        "ALTER TABLE chat_pair_codes ADD COLUMN bound_task_id INTEGER "
        "REFERENCES tasks(id)",
    ),
    (
        "add_chat_pair_sessions_kind",
        "ALTER TABLE chat_pair_sessions ADD COLUMN kind TEXT NOT NULL DEFAULT 'intake'",
    ),
    (
        "add_chat_pair_sessions_bound_task_id",
        "ALTER TABLE chat_pair_sessions ADD COLUMN bound_task_id INTEGER "
        "REFERENCES tasks(id)",
    ),
    (
        "add_chat_pair_sessions_acting_principal_id",
        "ALTER TABLE chat_pair_sessions ADD COLUMN acting_principal_id INTEGER "
        "REFERENCES principals(id)",
    ),
    # #1025: who a review report and a dispatched run belong to, as facts from
    # the TOKEN — submitted_by is the caller naming itself and stays what it
    # was. Both nullable, no backfill: a NULL means "recorded before this
    # existed" and every reader falls back to the old rule on it.
    (
        "add_machine_reviews_principal_id",
        "ALTER TABLE machine_reviews ADD COLUMN principal_id INTEGER",
    ),
    (
        "add_review_dispatches_reviewer_principal_id",
        "ALTER TABLE review_dispatches ADD COLUMN reviewer_principal_id INTEGER",
    ),
    (
        # #1026: what the PROVIDER billed for this dispatched run, including
        # runs that never produced a report. machine_reviews.provider_tokens
        # still holds the bill for the report path; a failed dispatch has no
        # report row to stamp. NULL is "never asked or the API did not
        # answer"; 0 is "answered zero". Never collapsed (#549).
        "add_review_dispatches_provider_tokens",
        "ALTER TABLE review_dispatches ADD COLUMN provider_tokens INTEGER",
    ),
    (
        # #1015: how many argument names Pydantic extra=ignore dropped on this
        # call. A count, not the names and never the values — the table has
        # nowhere a payload can land (#780). NULL is "never measured" (rows
        # written before this column); 0 is "schema matched"; a positive
        # number is the discarded-field warning made countable. Never collapse
        # the two (#549).
        "add_mcp_call_events_unknown_arg_count",
        "ALTER TABLE mcp_call_events ADD COLUMN unknown_arg_count INTEGER",
    ),
    (
        # #1030: which submission's red CI has already been charged to the
        # pair fix budget. The delivery gate refuses once per poll cycle while
        # the CI stays red, and a per-refusal counter would spend a budget of
        # three in ninety seconds. The submission generation is the unit that
        # matches what is being counted — one attempt by the executor — so a
        # charge is idempotent until the work is submitted again. -1 is "never
        # charged": generation 0 is a real submission and must not read as one.
        "add_ci_fix_charged_generation_column",
        "ALTER TABLE tasks ADD COLUMN ci_fix_charged_generation "
        "INTEGER NOT NULL DEFAULT -1",
    ),
    (
        # Steward judgement is a stored structured record, not a transition
        # (#1022). Unique on (task_id, generation, kind) is the at-most-once
        # claim, same shape as claim_arbiter_dispatch (#421). No backfill:
        # there were no judgements before this table.
        "create_steward_judgements",
        """CREATE TABLE IF NOT EXISTS steward_judgements (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id            INTEGER NOT NULL,
            generation         INTEGER NOT NULL,
            kind               TEXT    NOT NULL,
            submitted_verdict  TEXT    NOT NULL,
            verdict            TEXT    NOT NULL,
            confidence         TEXT    NOT NULL DEFAULT '',
            escalate_reason    TEXT    NOT NULL DEFAULT '',
            grounds            TEXT    NOT NULL DEFAULT '[]',
            findings           TEXT    NOT NULL DEFAULT '[]',
            closures           TEXT    NOT NULL DEFAULT '[]',
            model              TEXT    NOT NULL DEFAULT '',
            tokens_spent       INTEGER,
            duration_ms        INTEGER,
            submitted_by       TEXT    NOT NULL DEFAULT '',
            principal_id       INTEGER,
            created_at         TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "idx_steward_judgements_unique",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_steward_judgements_unique "
        "ON steward_judgements(task_id, generation, kind)",
    ),
    (
        # #1018: did the AGENT claim this report, or did the hub transcribe it
        # from a dispatch log? Until now the two were the same row, so a text
        # picked out of a log by LENGTH opened the whole git tail — commit,
        # squash, push, create_pr — on work nobody said was finished.
        #
        # A phrase inside `content` would not do: it cannot be queried, and a
        # rule nobody can check is not a rule. Default 1 means "claimed by the
        # agent", so every row written before this column keeps the behaviour
        # it had — the flag marks the new, narrower case, never re-judges
        # history.
        "add_task_updates_agent_claimed",
        "ALTER TABLE task_updates ADD COLUMN agent_claimed INTEGER NOT NULL DEFAULT 1",
    ),
    # #1020: which rung of the human-queue ladder has already been rung. Its
    # own table rather than the events feed, which is pruned at 14 days — the
    # queue outlives that (a production needs_info has stood since 20 July),
    # so rungs recorded as events would quietly re-fire on a fortnightly
    # cycle. ``entered_at`` is part of the key on purpose: re-entering the
    # same status is a NEW wait and deserves a fresh ladder, while a task that
    # simply keeps standing never repeats a rung it has already had.
    (
        "add_human_queue_reminders",
        "CREATE TABLE IF NOT EXISTS human_queue_reminders ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  task_id INTEGER NOT NULL,"
        "  instance TEXT NOT NULL,"
        "  entered_at TEXT NOT NULL,"
        "  rung TEXT NOT NULL,"
        "  age_minutes INTEGER NOT NULL,"
        "  age_estimated INTEGER NOT NULL DEFAULT 0,"
        "  created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "  UNIQUE(task_id, instance, entered_at, rung)"
        ")",
    ),
    # What the IMPLEMENTER says became of each confirmed finding, recorded at
    # the moment of resubmission (#911). Deliberately NOT ``finding_dispositions``.
    #
    # Those two tables answer different questions and belong to different
    # actors. A disposition is a HUMAN's judgement of whether the finding was
    # real — it is the numerator of precision, and #876 makes naming it a human
    # act. An outcome is the AUTHOR's account of what they DID about it. Writing
    # the author's account into the dispositions table would be the cheapest
    # possible way to destroy the metric: ``_disposition_metrics`` selects from
    # it without filtering ``decided_by``, so every self-reported "fixed" would
    # count as a human confirming the finding was real, and precision would
    # start measuring an author's opinion of their own work.
    #
    # UNIQUE(review_id, finding_uid): one account per finding per report. A
    # second pass corrects the first rather than stacking a contradictory row
    # beside it. Addressed by uid, not by slot — a position belongs to the list,
    # not to the finding (#1007).
    (
        "create_finding_outcomes",
        "CREATE TABLE IF NOT EXISTS finding_outcomes ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  review_id INTEGER NOT NULL,"
        "  task_id INTEGER NOT NULL,"
        "  submission_generation INTEGER NOT NULL,"
        "  finding_uid TEXT NOT NULL,"
        "  finding_index INTEGER NOT NULL DEFAULT -1,"
        "  finding_title TEXT NOT NULL DEFAULT '',"
        "  outcome TEXT NOT NULL,"
        "  note TEXT NOT NULL DEFAULT '',"
        "  linked_task_id INTEGER,"
        "  reported_by TEXT NOT NULL DEFAULT '',"
        "  reported_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "  UNIQUE(review_id, finding_uid)"
        ")",
    ),
    (
        "idx_finding_outcomes_task",
        "CREATE INDEX IF NOT EXISTS idx_finding_outcomes_task "
        "ON finding_outcomes(task_id, submission_generation)",
    ),
    (
        # Steward run orders (#1073). The hub places them; the steward never
        # does — a judge that can order its own run is a judge nobody called.
        # status: open → judged | timeout | superseded. #1075 already reads
        # this table to decide whether the evidence packet may be handed out,
        # so ordering a run is literally what opens that door.
        "create_steward_runs",
        "CREATE TABLE IF NOT EXISTS steward_runs ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,"
        "  generation INTEGER NOT NULL,"
        "  kind TEXT NOT NULL,"
        "  status TEXT NOT NULL DEFAULT 'open',"
        "  model TEXT NOT NULL DEFAULT '',"
        "  project_id INTEGER,"
        "  deadline_at TEXT NOT NULL,"
        "  closed_reason TEXT NOT NULL DEFAULT '',"
        "  created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "  closed_at TEXT,"
        # At-most-once lives HERE rather than in a read-before-write check:
        # two poller ticks racing on the same generation is the ordinary case,
        # not the exotic one, and a second order costs a second paid run.
        "  UNIQUE(task_id, generation, kind)"
        ")",
    ),
    (
        "idx_steward_runs_open",
        "CREATE INDEX IF NOT EXISTS idx_steward_runs_open "
        "ON steward_runs(status, deadline_at)",
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

# Defect passport keys that arrive on a refine payload and are NOT written as
# plain columns (#910). They travel through ``repo.set_defect_passport`` so the
# causal link is resolved first; ``clear_caused_by`` is a verb, not a column.
PASSPORT_REFINE_FIELDS = frozenset(
    {"found_in", "caused_by_task_id", "detected_at", "clear_caused_by"}
)

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
    # Defect passport (#909). Read-path only for now: the columns are written
    # through ``repo.set_defect_passport``, which resolves the causal link
    # before it lands, and never through a bare refine payload. Listing them
    # here is what makes them visible in TaskView — a field stored and not
    # listed is stored and never seen again.
    "found_in",
    "caused_by_task_id",
    "detected_at",
    "resolved_at",
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
        if key in PASSPORT_REFINE_FIELDS:
            # Defect passport (#910): accepted on the refine payload, written
            # by ``repo.set_defect_passport``. Letting it through here would
            # store a causal link nobody resolved — the one thing #909 built
            # the validator to prevent.
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
    rows = await fetchall(db, f"PRAGMA table_info({table})")
    return any(row[1] == column for row in rows)


async def _migrate(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))"
    )
    applied = {row[0] for row in await fetchall(db, "SELECT name FROM _migrations")}
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
    rows = await fetchall(
        db, "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return len(rows) > 0


async def _migrate_proposals(db: aiosqlite.Connection) -> None:
    """Migrate old proposals table into tasks with source='agent'."""
    rows = await fetchall(db, "SELECT * FROM proposals")
    for r in rows:
        d = dict(r)
        status_map = {"pending": "draft", "approved": "open", "rejected": "rejected"}
        new_status = status_map.get(d.get("status", ""), "draft")
        existing = await fetchall(
            db,
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


async def connect(dsn: str | None = None) -> aiosqlite.Connection:
    """Открыть соединение с базой. Ни миграций, ни seed — только режимы.

    Раньше это делал get_db одной операцией: открыть, мигрировать, засеять.
    Пока соединение было одно на процесс, разницы не было. С соединением на
    запрос она принципиальная: миграции и seed — работа подъёма приложения, и
    гонять их на каждый запрос значит платить за них на каждом запросе и
    сериализовать всё об один файл.

    Режимы, а не умолчания (#1065):
    * WAL — читатели не ждут писателя. Без него любые два соединения к одному
      файлу выстраиваются в очередь на любой записи, и «соединение на запрос»
      оказалось бы медленнее общего.
    * busy_timeout — писатель ждёт занятую базу вместо мгновенного
      "database is locked". Ноль по умолчанию означает, что конкуренция
      превращается в ошибку, а не в задержку.
    * foreign_keys — как было.

    WAL к in-memory базе неприменим: она молча остаётся в режиме memory, и
    PRAGMA это не ошибка, а тихий отказ. Поэтому режим здесь запрашивается, а
    не утверждается — проверять его надо на файловой базе, что и делает
    tests/test_db_transactions.py.
    """
    # isolation_level="IMMEDIATE" — это и есть BEGIN IMMEDIATE из постановки,
    # и он снимает надобность править 165 мест с .commit(). Драйвер открывает
    # неявную транзакцию перед первым INSERT/UPDATE/DELETE; по умолчанию она
    # DEFERRED, то есть берёт write-лок в последний момент. Если к этому
    # моменту соединение уже читало, SQLite отдаёт SQLITE_BUSY НЕМЕДЛЕННО и не
    # зовёт busy-handler вовсе — так устроено избегание дедлока, и потому
    # busy_timeout на этом пути не помогает.
    #
    # Измерено, а не выведено: с DEFERRED двенадцать параллельных записей в
    # test_parallel_ac_writes_do_not_500 падали с "database is locked" при
    # busy_timeout=5000. IMMEDIATE берёт write-лок сразу, busy-handler
    # включается, и писатели выстраиваются в очередь вместо отказа.
    #
    # Чтения не затронуты: неявную транзакцию драйвер открывает только перед
    # изменяющим запросом, SELECT её не начинает.
    db = await aiosqlite.connect(
        dsn or str(HUB_DB_PATH), uri=True, isolation_level="IMMEDIATE"
    )
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return db


@asynccontextmanager
async def write_transaction(db: aiosqlite.Connection):
    """Блок «прочитать и записать» одной транзакцией, взятой сразу на запись.

    Заменяет ручной ``get_write_lock`` (#1065). Лок лежал НА соединении и имел
    смысл, пока соединение было одно на процесс: он не давал двум мутациям
    переплести SAVEPOINT и commit. С соединением на запрос у каждого запроса
    своё соединение, и сериализовать этому локу нечего — он молча перестал
    работать, оставшись в коде.

    Работу за него делает база. ``BEGIN IMMEDIATE`` берёт write-лок в начале,
    а не при первой записи, и это важно ровно для схемы «проверил — вставил»:
    без него два запроса проходят SELECT одновременно, и второй INSERT
    прилетает IntegrityError вместо обещанного 409. Ждать очереди законно —
    busy_timeout выставлен в connect(); на DEFERRED-пути SQLite не стал бы
    ждать вовсе.

    Вложенный вызов не открывает вторую транзакцию и не коммитит чужую:
    владелец блока — тот, кто его начал.
    """
    if db.in_transaction:
        yield
        return
    await db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        await db.rollback()
        raise
    else:
        await db.commit()


async def bootstrap(db: aiosqlite.Connection) -> None:
    """Схема, миграции и seed — ровно один раз, на подъёме приложения."""
    await db.executescript(_SCHEMA)
    await _migrate(db)
    await _fix_orphaned_parents(db)
    from hub.services.lifecycle import repair_stale_parent_completions

    await repair_stale_parent_completions(db)
    if await _table_exists(db, "roles"):
        await seed_system_roles(db)
    if await _table_exists(db, "principals"):
        await seed_chat_pair_agent(db)
    if await _table_exists(db, "projects"):
        await seed_default_project(db)
    if await _table_exists(db, "skills"):
        await seed_default_skills(db)


async def get_db() -> aiosqlite.Connection:
    """Соединение подъёма: открыть и привести базу в рабочее состояние.

    Остаётся точкой входа для старта приложения и для тех, кому нужна
    полностью готовая база одним вызовом.
    """
    HUB_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await connect()
    await bootstrap(db)
    return db


async def _fix_orphaned_parents(db: aiosqlite.Connection) -> None:
    """Nullify parent_id references that point to nonexistent tasks."""
    orphans = await fetchall(
        db,
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

    rows = await fetchall(db, "SELECT task_type FROM tasks WHERE id=?", (parent_id,))
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


async def validate_caused_by(
    db: aiosqlite.Connection,
    task_id: int,
    caused_by_task_id: int | None,
) -> str | None:
    """Validate a defect's causal link. Returns an error message or None.

    Mirrors ``validate_hierarchy``: the check lives next to the schema and
    answers in prose, so every caller (API, MCP, CLI) can refuse with the same
    sentence instead of inventing its own.

    Two ways to get this wrong are refused rather than stored:

    * a link to a task that does not exist — a dangling blame is worse than no
      blame, because reports would count it as an attributed defect;
    * a link to the defect itself — the change that introduced a defect is
      never the record of the defect.

    ``None`` is always valid: an unattributed defect is the normal state.
    """
    if caused_by_task_id is None:
        return None
    if caused_by_task_id == task_id:
        return f"Task #{task_id} cannot be its own caused_by_task_id"
    rows = await fetchall(db, "SELECT id FROM tasks WHERE id=?", (caused_by_task_id,))
    if not rows:
        return f"caused_by task #{caused_by_task_id} not found"
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
        rows = await fetchall(
            db,
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
    rows = await fetchall(
        db,
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

    rows = await fetchall(
        db, "SELECT status FROM tasks WHERE parent_id=? AND archived=0", (task_id,)
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
    rows = await fetchall(
        db,
        "SELECT id, title, task_type, status, priority, assigned_agent FROM tasks WHERE id=?",
        (task_id,),
    )
    if not rows:
        return None

    task = dict(rows[0])
    children_rows = await fetchall(
        db,
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
    # #1021: the steward principal's two verbs. Asked by the two allowed
    # routes; the rest of the surface is refused by the allowlist, not by
    # omitting these from some other role.
    "steward.evidence.read",
    "steward.judgement.write",
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
    "steward.evidence.read",
    "steward.judgement.write",
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
    (
        # #1021: judgement, not action. Two verbs only; chat-pair's four would
        # let this principal write the statement it then judges.
        "steward",
        "Steward",
        "Read the evidence pack and write a judgement; cannot change task state",
        ("steward.evidence.read", "steward.judgement.write"),
    ),
)


async def seed_default_project(db: aiosqlite.Connection) -> None:
    """Seed the 'default' project from env and backfill epics (#335).

    Idempotent: an existing default row is reused, and only epics without a
    project are assigned. Until consumers land (V1.2+), this changes no
    behavior — it only gives every existing task a resolvable project.
    """
    from hub import config as cfg

    rows = await fetchall(db, "SELECT id FROM projects WHERE slug='default'")
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
схема ответа findings[] = {title, file, locator, start_line, severity: high|medium|low,
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
Каждая называет своё место: locator=lines с file и start_line, либо
locator=file, либо честное locator=none. Лучше 2 настоящих, чем 10
предположительных».

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

Как выполнить machine-review задачи в Haiplane Hub без контекста чужих
сессий. Оркестратор любой (Claude Code Workflow, Cursor, свой скрипт) —
контракт один.

## Когда обязателен
Политика (#382): каскад override задачи > политика проекта (off|auto|always)
> автоправила (docs/chore/spike и размеры XS/S — нет; refactor и
feature/bug M+ — да; риск high или security — всегда). Хаб сам сообщает:
`lifecycle_hint` в ответе `hub_submit_for_review`, предупреждение в панели
ревью, событие `machine_review_requested` в фиде. Режим
HAIPLANE_MACHINE_REVIEW=require
блокирует человеческий вердикт без актуального отчёта; дефолт warn —
только предупреждает.

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
confirmed: {title, severity high|medium|low, locator, category slug, file,
start_line, end_line, detail}; rejected: {title, category, reason};
unresolved: {title, why}.

`locator` ОБЯЗАТЕЛЕН (#1007) и говорит, где находка сидит: `lines` — известны
файл и строки (file + start_line, end_line для диапазона); `file` — известен
модуль, строка нет; `none` — место определить не удалось. `none` это ОТВЕТ и он
принимается: харнесс, который не может указать место, всё равно сдаёт годный
отчёт. Пустой file ответом не является — его не отличить от забытого поля.
Идентификатор находки НЕ присылается: хаб выводит его из содержания —
category, file, нормализованный title и КАНОНИЧЕСКОЕ место — и возвращает на
каждом чтении отчёта. Каноническое значит выведенное из того, что известно
(есть строка → «строки», есть только файл → «файл», нет ничего → «нигде»), а не
скопированное из поля `locator`: одно и то же место, названное двумя способами,
обязано давать один id, иначе диспозиция с прошлого отчёта не найдётся (#1028).
Поэтому старая находка `{file, line}` и новая `{locator: lines, file,
start_line}` с теми же значениями — одна находка. `end_line` в id не входит:
диапазон это уточнение того же места, а не другое место.
category питает метрики повторяемости — используй устойчивые слаги
(security, correctness, consistency, tests, …).

Объяснение у `unresolved` называется `why`, а не `reason`: `reason`
принадлежит `findings_rejected`. Лишние ключи отвергаются, а не отбрасываются
молча (#553) — перепутанное имя раньше давало сохранённую находку с пустым
объяснением.
"""


async def seed_default_skills(db: aiosqlite.Connection) -> None:
    """Seed built-in skills, and keep the ACTIVE one current (#380, #383, #1028).

    The question this function asks is not "is the shipped text in the library"
    but "is the shipped text the one agents READ". ``hub_get_skill`` serves the
    ACTIVE version, so a library holding the new text as a draft is a library
    still teaching the old one. The first attempt (#1007) filed everything as a
    draft and left exactly that state in production: on 2026-08-28 the write
    path already refused reports without a ``locator`` while the active
    ``machine-review-cycle`` was v1 from July, teaching the format that gets a
    422 — every harness that honestly read the library walked into it.

    So the branches are cut by whose word is at stake, and only the active row
    decides:

    1. The active version already carries the shipped text — nothing to do.
    2. A person activated the current version — it stays, and the shipped text
       waits beside it as a draft. Nothing a human published is overwritten,
       which is the rule #380 set. That a draft already waits there is not a
       reason to stop looking: the answer to case 3 can change under it.
    3. Nobody's word is at stake — there is no active version at all, or the
       active one is the hub's own previous seed. The shipped text becomes
       active: promoted in place if some version already holds it, inserted
       otherwise. "No active version" belongs HERE and not in case 2, because
       an operator who published nothing has said nothing, and leaving another
       draft behind would keep ``hub_get_skill`` answering 404.

    Whose word it is cannot be read off ``created_by`` alone. Activation is its
    own act: a person who activates a seeded draft leaves ``created_by='seed'``
    on the row, and a seed keyed on that field would overrule them on the next
    deploy. ``activated_by`` records the act rather than the authorship, and
    only a row nobody but the seed activated is replaceable.

    Concurrency: ``get_db`` runs this on every connection, so two workers can
    read the same max version and both insert. ``UNIQUE(name, version)`` makes
    one of them lose, and losing is FINE — the winner wrote the same text. The
    insert says ``ON CONFLICT DO NOTHING`` rather than raising, because a
    swallowed ``IntegrityError`` leaves the transaction aborted and the next
    seed in the loop dies on a healthy statement (#1028).
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
        rows = await fetchall(
            db,
            "SELECT id, version, content, status, created_by, activated_by "
            "FROM skills WHERE name=? ORDER BY version DESC",
            (name,),
        )
        if not rows:
            await _insert_seed_skill(db, name, kind, content, tags, 1, "active")
            continue
        # Highest active version — the same row ``get_active_skill`` serves.
        active = next((r for r in rows if str(r["status"]) == "active"), None)
        if active is not None and str(active["content"]) == content:
            continue
        next_version = int(rows[0]["version"]) + 1
        if active is not None and not _is_seed_word(active):
            # Case 2. Someone published this; our text waits beside it.
            if not any(str(r["content"]) == content for r in rows):
                await _insert_seed_skill(
                    db, name, kind, content, tags, next_version, "draft"
                )
            continue
        # Case 3. The shipped text has to become the one agents read.
        shipped = next((r for r in rows if str(r["content"]) == content), None)
        if shipped is not None:
            if _is_seed_word(shipped):
                await db.execute(
                    "UPDATE skills SET status='active', activated_by='seed' WHERE id=?",
                    (int(shipped["id"]),),
                )
            else:
                # A person published this exact text. Activating it is AGREEING
                # with them, not replacing them, so their signature outlives the
                # act — stamping 'seed' here would erase the only record that a
                # human ever spoke, and the next upgrade would then read the row
                # as ours and overrule a decision that was never ours to make.
                await db.execute(
                    "UPDATE skills SET status='active' WHERE id=?",
                    (int(shipped["id"]),),
                )
        else:
            await _insert_seed_skill(
                db, name, kind, content, tags, next_version, "active"
            )
        # And the hub's own PREVIOUS word steps back to a draft. Without this
        # every upgrade leaves another live version behind, and since
        # ``get_active_skill`` serves the HIGHEST active one, a revert never
        # takes effect: reverting the constant re-activates an older version
        # while the reverted-away text keeps winning on version number, for
        # good. ``draft`` is the existing vocabulary for "in the library, not
        # served" — a third status would be one this schema has never had.
        #
        # Two guards, and both are load-bearing.
        #
        # The seed-ownership half is ``_is_seed_word`` said in SQL, deliberately:
        # it must be impossible for this statement to demote a row a person
        # published, and the safest way to say that is to spell the same
        # condition the branch above was chosen by.
        #
        # The EXISTS half is what makes losing the insert survivable. The
        # statement above says ON CONFLICT DO NOTHING, so this connection may
        # have written nothing at all — and if the row that took its version
        # number belongs to somebody else (an agent proposing a version through
        # POST /api/skills files a DRAFT), stepping our previous word down would
        # leave the library with NO active version and ``hub_get_skill``
        # answering 404. Serving stale text until the next seeder replaces it is
        # bad; serving nothing is worse. So the old word only steps down once
        # the shipped text is demonstrably the one being served.
        await db.execute(
            "UPDATE skills SET status='draft' WHERE name=? AND status='active' "
            "AND content<>? AND created_by='seed' AND activated_by IN ('', 'seed') "
            "AND EXISTS (SELECT 1 FROM skills live WHERE live.name=skills.name "
            "AND live.content=? AND live.status='active')",
            (name, content, content),
        )
    await db.commit()


def _is_seed_word(row: aiosqlite.Row) -> bool:
    """True when the hub is the only one who ever spoke for this row.

    Two acts, two fields. ``created_by`` says who wrote the text;
    ``activated_by`` says who decided agents should read it. A person who
    activates a seeded draft (#380 makes activation a human gate) writes the
    second without touching the first — so a check that reads only
    ``created_by`` would call that row ours and replace a human's decision on
    the next deploy. Rows that predate the column carry an empty
    ``activated_by`` and are judged by authorship alone, which is all that was
    ever recorded about them.
    """
    return str(row["created_by"]) == "seed" and str(row["activated_by"] or "") in (
        "",
        "seed",
    )


async def _insert_seed_skill(
    db: aiosqlite.Connection,
    name: str,
    kind: str,
    content: str,
    tags: str,
    version: int,
    status: str,
) -> None:
    """Insert one seeded version, tolerating a parallel seeder.

    The race is real and benign: ``get_db`` seeds on every connection, so two
    workers starting together compute the same next version. ``ON CONFLICT DO
    NOTHING`` is what makes losing cheap — catching the ``IntegrityError``
    instead would leave sqlite's transaction aborted, and the very next
    statement of this loop (the second seeded skill) would fail on nothing it
    did wrong, taking the connection down over a row that already says what we
    wanted to say.
    """
    await db.execute(
        "INSERT INTO skills (name, kind, version, content, tags, status, "
        "created_by, activated_by) VALUES (?, ?, ?, ?, ?, ?, 'seed', ?) "
        "ON CONFLICT(name, version) DO NOTHING",
        (
            name,
            kind,
            version,
            content,
            tags,
            status,
            "seed" if status == "active" else "",
        ),
    )


async def seed_system_roles(db: aiosqlite.Connection) -> None:
    """Ensure system roles and their permissions exist.

    Idempotent: skips roles that already exist, adds missing permissions.
    """
    for slug, name, description, permissions in SYSTEM_ROLES:
        rows = await fetchall(db, "SELECT id FROM roles WHERE slug = ?", (slug,))
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


async def seed_chat_pair_agent(db: aiosqlite.Connection) -> None:
    """Ensure the acting principal for kind=implementer exists (#980).

    Idempotent. Username comes from ``HAIPLANE_CHAT_PAIR_AGENT`` (default
    ``cloud``). Issue refuses with 503 when this row is missing or not active;
    seeding here means a stock hub can issue implementer codes.
    """
    username = (CHAT_PAIR_AGENT or "cloud").strip() or "cloud"
    rows = await fetchall(
        db, "SELECT id FROM principals WHERE username = ?", (username,)
    )
    if rows:
        return
    cursor = await db.execute(
        "INSERT INTO principals (kind, username, display_name, notes) "
        "VALUES ('agent', ?, ?, 'seeded chat-pair implementer')",
        (username, username),
    )
    principal_id = inserted_id(cursor)
    role_rows = await fetchall(db, "SELECT id FROM roles WHERE slug = 'agent'")
    if role_rows:
        await db.execute(
            "INSERT OR IGNORE INTO principal_roles (principal_id, role_id) "
            "VALUES (?, ?)",
            (principal_id, role_rows[0][0]),
        )
    await db.commit()


async def has_active_admin(db: aiosqlite.Connection) -> bool:
    """Check if at least one active principal with admin or super_admin role exists."""
    rows = await fetchall(
        db,
        """SELECT 1 FROM principals p
           JOIN principal_roles pr ON p.id = pr.principal_id
           JOIN roles r ON pr.role_id = r.id
           WHERE p.status = 'active'
             AND r.slug IN ('super_admin', 'admin')
           LIMIT 1""",
    )
    return len(rows) > 0
