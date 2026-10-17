"""Миграции схемы, часть 2 из 3 (#1809). Порядок и SQL не менять."""

from __future__ import annotations

from hub.migrations.sql_constants import _SQL_EVIDENCE_BAR

MIGRATIONS_2: list[tuple[str, str]] = [
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
        # Mutation run over the functions a PR changed (#1270): warning only.
        # Its own column rather than an entry in ``checks``: a check outcome
        # feeds the reviewer's prepass, and a surviving mutant is a named
        # suspicion about a test, not proof the code is broken. '{}' for every
        # older report means "no mutation run was reported" — never "none
        # survived".
        "add_ci_run_reports_mutations",
        "ALTER TABLE ci_run_reports ADD COLUMN mutations TEXT NOT NULL DEFAULT '{}'",
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
            -- множество уже озвученных состояний через запятую (#294):
            -- одна ячейка затиралась при дребезге провайдера
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
        # #1084: a reviewer code belongs to ONE SUBMISSION, not merely to a
        # task that happens to still be in review. A resubmission while the
        # run is alive leaves the status untouched, so a code guarded only by
        # status would outlive the work it was minted for and file a report
        # about the old diff against the new generation.
        "add_chat_pair_codes_bound_generation",
        "ALTER TABLE chat_pair_codes ADD COLUMN bound_generation INTEGER",
    ),
    (
        # The session inherits it: redeem can happen before the resubmission
        # and filing after it, and that window is the same defect one step
        # later.
        "add_chat_pair_sessions_bound_generation",
        "ALTER TABLE chat_pair_sessions ADD COLUMN bound_generation INTEGER",
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
        # #1180: каким способом добыт отчёт — cloud или local. Умолчание
        # 'cloud' описывает историю правдиво: до этой колонки другого способа
        # не было вовсе.
        "add_review_dispatches_channel",
        "ALTER TABLE review_dispatches ADD COLUMN channel TEXT NOT NULL "
        "DEFAULT 'cloud'",
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
        # Which section of the report the answered finding came from (#1085).
        # 'confirmed' for every row written before this column existed, which
        # is what they all were: until now only confirmed findings could be
        # answered at all. The kind is stored rather than inferred from the
        # outcome word — the two dictionaries do not overlap TODAY, and a
        # reader that leans on that keeps working only until someone adds a
        # word to either one.
        "add_finding_outcomes_kind",
        "ALTER TABLE finding_outcomes ADD COLUMN finding_kind TEXT NOT NULL "
        "DEFAULT 'confirmed'",
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
        # #1105: which cloud run is carrying this order. Empty means nobody
        # started it yet — that is what makes starting at-most-once possible
        # without a second table.
        "add_steward_runs_agent_id",
        "ALTER TABLE steward_runs ADD COLUMN agent_id TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_steward_runs_run_id",
        "ALTER TABLE steward_runs ADD COLUMN run_id TEXT NOT NULL DEFAULT ''",
    ),
    (
        "idx_steward_runs_open",
        "CREATE INDEX IF NOT EXISTS idx_steward_runs_open "
        "ON steward_runs(status, deadline_at)",
    ),
    (
        # #1156: поколение ПОСТАНОВКИ, отдельное от поколения сдачи.
        # Суждения и прогоны стюарда стоят на (task_id, generation, kind), а
        # у драфта submission_generation равен нулю и не двигается — значит
        # kind='dor' имел бы один слот на всю жизнь драфта, и повторное
        # чтение после правок автора некуда было бы записать.
        #
        # DEFAULT 0 без пересчёта старых строк: прошлых ревизий в базе нет,
        # и восстанавливать их не из чего. Ноль здесь означает «правок после
        # появления счётчика не было», а не «постановку не меняли никогда».
        "add_tasks_statement_generation",
        "ALTER TABLE tasks ADD COLUMN statement_generation INTEGER NOT NULL DEFAULT 0",
    ),
    (
        # Отпечаток постановки, по которому видно, что она ИЗМЕНИЛАСЬ, а не
        # была пересохранена. Без него счётчик рос бы на каждом пересчёте
        # готовности, и «постановку правили» перестало бы быть признаком —
        # а на нём стоит потолок против хождения драфта по кругу (#1161).
        "add_tasks_statement_fingerprint",
        "ALTER TABLE tasks ADD COLUMN statement_fingerprint TEXT NOT NULL DEFAULT ''",
    ),
    # ---- Голос реестра расхождений (#1198). Обнаружение было построено и
    # верно; не хватало того, чтобы найденное перестало ждать вопроса — и при
    # этом не превратилось в шум, который перестают читать.
    #
    # acknowledged_* — признание расхождения ЗАКОННЫМ. Строка замолкает, но
    # остаётся в реестре с названной причиной: заткнуть можно, стереть нельзя.
    # Причина обязательна в коде, а не в комментарии: без неё это выключатель,
    # а не решение.
    #
    # alerted_age_bucket — сколько часов возраста уже отзвучало. Повтор
    # случается при переходе рубежа и ровно один раз. Условие вида «возраст
    # больше N» без этой памяти звучало бы на КАЖДОМ тике, а лента одинаковых
    # строк — это ровно тот способ убить сигнал, от которого задача защищает.
    (
        "add_delivery_acknowledged_at",
        "ALTER TABLE delivery_discrepancies "
        "ADD COLUMN acknowledged_at TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_delivery_acknowledged_by",
        "ALTER TABLE delivery_discrepancies "
        "ADD COLUMN acknowledged_by TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_delivery_ack_reason",
        "ALTER TABLE delivery_discrepancies "
        "ADD COLUMN ack_reason TEXT NOT NULL DEFAULT ''",
    ),
    # acknowledged_state — ФАКТ, который признали законным, а не задача целиком.
    # Решение владельца 08.09.2026 по неразрешённой находке ревью: признание
    # относится к факту. Без этой колонки «PR держим открытым намеренно»
    # глушило и следующее, никем не одобренное состояние — например «доставку
    # подтвердить не удалось», — то есть одно суждение отнимало голос у
    # другого. ОБРАТНОЙ ЗАСЫПКИ НЕТ НАМЕРЕННО: признанных строк не существует
    # — колонки acknowledged_* родились в этой же задаче и на прод не
    # выкатывались, — а засыпать пришлось бы ТЕКУЩИМ состоянием, которое свип
    # перезаписывает каждые 15 минут. Это не то, что признавал человек, и
    # правило было бы неверным ровно в ту сторону, ради которой задача
    # заведена. Пустое значение тут не совпадёт ни с одним состоянием, то есть
    # читается как «не признано» — и это верная сторона осторожности (#762).
    (
        "add_delivery_acknowledged_state",
        "ALTER TABLE delivery_discrepancies "
        "ADD COLUMN acknowledged_state TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_delivery_alerted_age_bucket",
        "ALTER TABLE delivery_discrepancies "
        "ADD COLUMN alerted_age_bucket INTEGER NOT NULL DEFAULT 0",
    ),
    (
        # #1236: ИМЯ читающей пробы из закрытого реестра, которую хаб исполнит
        # сам после доставки. Одно имя, а не строка вызова: колонка, куда
        # ложится текст из карточки, стала бы каналом исполнения произвольного
        # кода на проде. Пусто — зонд не объявлен, и это законное состояние:
        # обязательной живая проверка не делается, обязательной её делает
        # постановка, когда объявляет зонд.
        "add_tasks_live_probe",
        "ALTER TABLE tasks ADD COLUMN live_probe TEXT NOT NULL DEFAULT ''",
    ),
    (
        # #1252: терминальный статус прогона, кончившегося БЕЗ отчёта. Он
        # нужен уже после того, как строку закрыли: долг второй двери
        # переживает перезапуск хаба, и на возобновлении назвать причину
        # отказа облака больше неоткуда — спрашивать провайдера заново значит
        # зависеть от его доступности ради текста в карточке. Пусто — прогон
        # до терминального статуса не доходил.
        "add_review_dispatches_run_status",
        "ALTER TABLE review_dispatches ADD COLUMN run_status TEXT NOT NULL DEFAULT ''",
    ),
    # --- Наблюдение доставки: четвёртый ВХОД, но не четвёртый ИСТОЧНИК (#1215).
    #
    # Три источника ответа о доставке — строка pipeline_merges, базовая ветка,
    # провайдер — могут оказаться закрыты ОДНОВРЕМЕННО и навсегда. Живой
    # случай 09.09.2026: #878 (PR #443), #875 (PR #461), #909 (PR #468) стояли
    # в unknown 441-453 часа, потому что прежний репозиторий, где жили эти PR,
    # удалён (в нынешнем нумерация началась заново), строк pipeline_merges
    # нет, а базовая ветка не отвечает из-за squash. Спрашивать больше некого
    # — ни сегодня, ни через месяц, и у строки не было НИ ОДНОГО выхода.
    #
    # Эти колонки — место, куда человек или агент кладёт то, что проверил
    # своими глазами. Почему отдельные колонки, а не перезапись state/reason:
    # наблюдение обязано ВИДИМО отличаться от того, что хаб установил сам.
    # Перезапись сделала бы наблюдение неотличимым от вывода трёх источников,
    # то есть ровно четвёртым источником правды, — и заодно стёрла бы прежнее
    # unknown вместе с его причиной. Закрытие дописывает историю, а не
    # заменяет её: state и reason остаются ровно теми, какими их оставил свип.
    #
    # observed_state — тот ФАКТ, который закрыли наблюдением, по образцу
    # acknowledged_state. Наблюдение «код в 19ee3f6 есть» относится к
    # состоянию unknown; если провайдер завтра оживёт и скажет pr_open, это
    # уже другое утверждение, которого никто не наблюдал, и строка обязана
    # вернуться в список. Обратной засыпки нет и быть не может: наблюдений до
    # этой миграции не существует.
    (
        "add_delivery_observed_at",
        "ALTER TABLE delivery_discrepancies "
        "ADD COLUMN observed_at TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_delivery_observed_by",
        "ALTER TABLE delivery_discrepancies "
        "ADD COLUMN observed_by TEXT NOT NULL DEFAULT ''",
    ),
    # probe и evidence раздельно — та же схема доказательства, что у живых
    # проверок (#813): «что запускал» и «что увидел». Одно поле принимало бы
    # «проверено, всё хорошо» как полноценную запись, а это штамп.
    (
        "add_delivery_observed_probe",
        "ALTER TABLE delivery_discrepancies "
        "ADD COLUMN observed_probe TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_delivery_observed_evidence",
        "ALTER TABLE delivery_discrepancies "
        "ADD COLUMN observed_evidence TEXT NOT NULL DEFAULT ''",
    ),
    # Коммит обязателен: наблюдение доставки — это утверждение о том, что код
    # ГДЕ-ТО есть, и без названного места оно нефальсифицируемо.
    (
        "add_delivery_observed_sha",
        "ALTER TABLE delivery_discrepancies "
        "ADD COLUMN observed_sha TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_delivery_observed_state",
        "ALTER TABLE delivery_discrepancies "
        "ADD COLUMN observed_state TEXT NOT NULL DEFAULT ''",
    ),
    # ЗАСЫПКА ПО УЖЕ ЗАПИСАННЫМ НАБЛЮДЕНИЯМ (#1215), и она здесь не ради
    # удобства выката. Три строки — #878, #875, #909 — простояли в unknown
    # 441-453 часа, и наблюдения по ним уже лежат: живые проверки от
    # 09.09.2026, с командой, с ответом и с раскатанным коммитом. Без засыпки
    # выкат оставил бы механизм, у которого нет ни одного закрытого случая, и
    # закрывать три строки пришлось бы руками, повторяя запись, которая уже
    # есть. Ровно этого требует постановка: «выкат проверяется на них».
    #
    # Берётся ПОСЛЕДНЯЯ живая проверка задачи, прошедшая ТОТ ЖЕ порог
    # доказательства, что и прямая запись, — и «тот же» здесь буквально: и
    # эта SQL, и живая дверь считают его по _SQL_EVIDENCE_BAR / константам
    # выше, одним определением. Пока определений было два, они разошлись:
    # засыпка спрашивала лишь TRIM != '' и закрывала строку записью вида
    # «ок / норм / zzz», которую живая дверь отвергает как штамп (находка
    # 80fcb9c9). Обещание в комментарии, не проверяемое кодом, — та же
    # подмена доказательства штампом, только этажом выше. Закрывается только unknown: строку, чей источник
    # отвечает, наблюдением не закрывают ни здесь, ни в коде.
    #
    # Одноразовость даёт сам механизм миграций (имя выполняется один раз), а
    # не WHERE: строка, которую человек закроет и передумает, не должна
    # закрываться заново при следующем запуске.
    (
        "backfill_delivery_observed_from_live_checks",
        f"""UPDATE delivery_discrepancies AS d
              SET observed_at       = (
                    SELECT c.created_at FROM live_checks c
                     WHERE c.task_id = d.task_id AND {_SQL_EVIDENCE_BAR}
                     ORDER BY c.id DESC LIMIT 1),
                  observed_by       = (
                    SELECT COALESCE(NULLIF(c.recorded_agent, ''), 'hub')
                      FROM live_checks c
                     WHERE c.task_id = d.task_id AND {_SQL_EVIDENCE_BAR}
                     ORDER BY c.id DESC LIMIT 1),
                  observed_probe    = (
                    SELECT c.probe FROM live_checks c
                     WHERE c.task_id = d.task_id AND {_SQL_EVIDENCE_BAR}
                     ORDER BY c.id DESC LIMIT 1),
                  observed_evidence = (
                    SELECT c.observation FROM live_checks c
                     WHERE c.task_id = d.task_id AND {_SQL_EVIDENCE_BAR}
                     ORDER BY c.id DESC LIMIT 1),
                  observed_sha      = (
                    SELECT c.sha FROM live_checks c
                     WHERE c.task_id = d.task_id AND {_SQL_EVIDENCE_BAR}
                     ORDER BY c.id DESC LIMIT 1),
                  observed_state    = d.state
            WHERE d.state = 'unknown'
              AND d.observed_at = ''
              AND EXISTS (
                    SELECT 1 FROM live_checks c
                     WHERE c.task_id = d.task_id AND {_SQL_EVIDENCE_BAR})""",  # nosec B608 - константы модуля, не данные
    ),
    (
        # #1266: id заказа, который ЭТА строка заменяет — ставится только на
        # локальную строку второй двери, заказанную вместо упавшего заказа
        # (create_review_dispatch внутри dispatch_local_review, когда
        # late_report_recheck передан). Пусто — строка сама начинает шаг, а
        # не продолжает чужой. Логический шаг лестницы (#879) считается по
        # этой колонке: замена не тратит свой собственный, она донашивает
        # шаг заменяемой строки (count_review_dispatches).
        "add_review_dispatches_replaces_dispatch_id",
        "ALTER TABLE review_dispatches ADD COLUMN replaces_dispatch_id INTEGER",
    ),
    (
        # #1266: почему отчёт добыт НЕ облаком — тем же текстом, что сегодня
        # уходит только в ленту задачи (review_dispatch.py, "why" при
        # create_review_dispatch с cloud_refusal). Живёт на строке заказа,
        # а не только в ленте, потому что бриф (review_brief.py) читает
        # именно строку через get_settled_review_dispatch — вторым автором
        # того же текста заводить не стоит. Пусто — заказ облачный или
        # локальный без наблюдённого отказа облака (форж недоступен облаку).
        "add_review_dispatches_second_door_reason",
        "ALTER TABLE review_dispatches ADD COLUMN second_door_reason TEXT "
        "NOT NULL DEFAULT ''",
    ),
    (
        # #1286: поколение сдачи, чьё одобрение закрыто решением человека
        # «на доработку». Отдельная колонка, а не обнуление review_verdict:
        # карточка строит latest_review из полей вердикта, и стирание унесло
        # бы вместе с окном и находки, и то, что именно было одобрено.
        # NULL — окно никто не закрывал; новый вердикт снимает отметку
        # (record_review_verdict), потому что она принадлежит вердикту так же,
        # как review_self_approved.
        "add_review_verdict_closed_generation_column",
        "ALTER TABLE tasks ADD COLUMN review_verdict_closed_generation INTEGER",
    ),
    (
        # #1240: вершина ветки задачи, которую гейт сам переписал сжатием при
        # доставке (squash_branch). Сдача закрепляет коммит вершины; после
        # сжатия в базу уходит ДРУГОЙ коммит, и «предок ли сдача базы»
        # отвечает «нет» на доставленной работе. Этот факт — единственное, по
        # чему такое «нет» отличимо от настоящего: угадывать по числу коммитов
        # постановка запрещает. '' — гейт ветку не переписывал или запись
        # старше этой колонки; ни то, ни другое не читается как «сжато».
        "add_tasks_gate_squashed_sha",
        "ALTER TABLE tasks ADD COLUMN gate_squashed_sha TEXT NOT NULL DEFAULT ''",
    ),
    (
        # #1328: когда прогон стюарда НАЧАЛ работу. created_at — время
        # заказа, а заказ может ждать исполнителя долго (#1181): длительность
        # суждения, отмеренная от заказа, приписала бы судье чужое ожидание.
        # Ставит её старт прогона (start_run) той же записью, что называет
        # агента. NULL — прогон не начинался или начат до этой колонки;
        # старые строки не пересчитываются (scope_out #1328).
        "add_steward_runs_started_at",
        "ALTER TABLE steward_runs ADD COLUMN started_at TEXT",
    ),
    (
        # #1328: почему у суждения нет числа токенов. '' — число есть (или
        # суждение старше колонки); pending — ждём ответа провайдера;
        # provider_no_answer — окно ответа вышло, провайдер молчал; no_run —
        # у суждения нет начатого прогона, спрашивать не о чем. Ноль в
        # tokens_spent означает только ответ провайдера «ноль».
        "add_steward_judgements_tokens_unknown_reason",
        "ALTER TABLE steward_judgements ADD COLUMN tokens_unknown_reason TEXT "
        "NOT NULL DEFAULT ''",
    ),
]
