"""Миграции схемы, часть 1 из 3 (#1809). Порядок и SQL не менять."""

from __future__ import annotations

from hub.migrations.sql_constants import BACKFILL_PUBLICATION_RECORD_SQL

MIGRATIONS_1: list[tuple[str, str]] = [
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
    # Доказательства публикации ЖИВУТ ЗДЕСЬ, а не в ленте событий (#1253).
    # Лента — канал уведомлений с двухнедельной чисткой (#349), и сама она об
    # этом говорит прямым текстом над своим DELETE. Запись о том, что именно
    # опубликовано, лежит на строке версии, которую описывает: у таблицы
    # ``skills`` нет и не может быть срока годности — удалить строку значит
    # удалить саму версию скилла, а не сведения о ней.
    (
        "add_skills_publication_record_column",
        "ALTER TABLE skills ADD COLUMN publication_record TEXT NOT NULL DEFAULT ''",
    ),
    # Разовый перенос того, что ещё лежит в ленте (#1253). Без него починка
    # работала бы только на будущих публикациях, а КАЖДАЯ уже опубликованная
    # версия всё равно потеряла бы свою запись через две недели — то есть
    # ровно тот дефект, ради которого заведена задача. Переносится последняя
    # запись по паре (name, version) — тем же «последняя побеждает», каким
    # читал ленту прежний ``latest_skill_activation``. Запись до #1169 несёт
    # только имя и версию, и она тоже переезжает: «запись есть, дифа в ней
    # нет» — отдельное состояние, и превратить его в «записи нет» нельзя.
    (
        "backfill_skills_publication_record",
        BACKFILL_PUBLICATION_RECORD_SQL,
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
    # ---- Один запрос прогона на коммит (#1197): SHA, по которому хаб уже
    # просил CI породить прогон. Ключ — сам коммит, а не счётчик: «одна
    # попытка на SHA» тогда читается как «записанный ≠ текущий» и не требует
    # сброса при смене вершины, то есть не заводит второго места, где можно
    # ошибиться. reset_ci_check_state эту колонку НЕ чистит намеренно — очистка
    # вернула бы вторую попытку по тому же коммиту, а с ней ферму флейков.
    (
        "add_ci_run_requested_sha_column",
        "ALTER TABLE tasks ADD COLUMN ci_run_requested_sha TEXT NOT NULL DEFAULT ''",
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
    # #1238. WHY the run called itself incomplete, from a fixed vocabulary.
    # The empty default is the honest value for history: every row written
    # before this column made no claim about the cause, and back-filling
    # either word would put one in its mouth — the same reasoning that left
    # `incomplete` nullable above. Nothing reads the cause out of prose.
    (
        "add_machine_reviews_incomplete_reason_column",
        "ALTER TABLE machine_reviews ADD COLUMN incomplete_reason TEXT "
        "NOT NULL DEFAULT ''",
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
        # Форж проекта (#1114, эпик #1112): на каком хостинге живёт репозиторий.
        # DEFAULT 'github' — не вкусовщина, а условие безопасности миграции:
        # все существующие проекты именно на нём, и колонка обязана описать
        # то, что уже есть, а не потребовать от кого-то заполнить её задним
        # числом. NOT NULL, потому что «форж неизвестен» — не состояние: у
        # репозитория всегда есть хостинг, вопрос только в том, объявлен ли он.
        "add_projects_forge",
        "ALTER TABLE projects ADD COLUMN forge TEXT NOT NULL DEFAULT 'github'",
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
]
