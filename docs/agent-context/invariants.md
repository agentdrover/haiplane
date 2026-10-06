# Invariants

These are the rules most likely to be broken by “small” changes.

## Domain Invariants

- Task hierarchy is strict: `epic -> feature -> task -> subtask`.
- Agent-created work starts as `draft` — including `epic`/`feature` proposals (#323); human-created work usually starts as `open` unless run immediately.
- Human-created `epic` and `feature` items are created as `open`; epics and features never auto-run and never auto-review.
- `subtask` items must not auto-enable review by default.

## Lifecycle Invariants

- Status values are defined in `hub/models.py:TaskStatus`.
- Final statuses are `completed`, `failed`, `rejected`.
- Active statuses are tracked by `ACTIVE_STATUSES` in `hub/models.py`; progress math depends on them.
- A `done` report from a disallowed status must not create a duplicate done row;
  the API returns HTTP 400/409 with `{reason, hint, required_status}`.
- On pair `running` (no `job_id`) or `claimed`, a valid done report routes through
  the shared post-done transition (blocker → `needs_decision`, else the
  Universal Review Gate below); completing `claimed` clears the claim.
- Universal Review Gate (#306): normal completion paths (done reports on
  pair/claimed/pending_report) complete a task only when
  `completion_requires_review` is false — i.e. `auto_review` is off (explicit
  opt-out) or the CURRENT submission generation has an APPROVED verdict.
  Otherwise the done report is a submission: → `ci_check` when a `branch`
  exists (conveyor), → `review` (client-driven, no `review_job_id`) without
  one, → `needs_decision` at the review-cycle limit. Completing approved work
  must NOT bump the submission generation (it would invalidate the approval).
- Review is submission-bound: `hub_submit_for_review` (or a routed done report)
  bumps the submission generation, which makes prior verdicts and reports stale.
  Fixes after `changes_requested` reach review only via a resubmit of the SAME
  task on the SAME branch — pushing commits alone does not re-trigger review.
  A review of task A never sees task B's branch; do not base new task branches
  on unmerged branches under review (see `docs/repository-rules.md`,
  «Жизненный цикл ветки задачи»). The stacking *advisory* (#438) judges
  pushed refs (`_resolve_ref_remote_first`): a stale local `develop` in the
  hub clone must not invent a stack or tell the implementer to merge another
  task's unreviewed branch (#1046).
- Finding routing (#435, #437): `in_scope` findings are closed ONLY via a
  resubmit of the same task on the same branch (`changes_requested` →
  `running` → fix → `hub_submit_for_review`). Never spawn parallel tasks for
  in-scope findings (incident #392). `out_of_scope` findings go to separate
  tasks referenced by `linked_task_id` and never block the verdict.
- Delivery PR for confirmed commits (#967): a pair task whose branch
  verifiably carries commits (`branch_diff_paths` → non-empty list) does not
  submit or complete without a PR — the hub pushes the branch and opens one
  itself (`ensure_delivery_pr`), at submission and at done. If commits are
  confirmed and the PR cannot be opened, the done report goes to
  `needs_decision`, not `completed`. The block arms ONLY on positive
  knowledge: `None` ("could not look") and `[]` (empty diff) keep the old
  path — the #498/#767 line that ignorance is not an accusation. Boundary:
  the invariant sees only what the hub's clone and origin see; a branch that
  exists solely in a foreign unpushed clone stays silent (#966's territory).
  This deliberately RETIRES the old carve-out "a task with a branch and no
  PR completes as before" for anything with observable commits.
- Delivery-gate refusals split three ways, by WHO has to act (#951, #1030, #1041, #1053).
  Transient (`ci_pending`, `ci_unavailable`, `ci_missing_run` within the CI
  grace window, `pr_draft`): nobody acts, the task stays `running` and the hub asks again.
  After the window a missing run becomes terminal `ci_untested` naming the
  commit. A GitHub draft after Hub APPROVED is transient, not recoverable: the
  hub marks the PR ready (approval is the ready signal) and retries;
  resubmitting would stale the verdict (#612) with no new commits. Recoverable
  (`ci_failed`, `stale_approval`): the EXECUTOR acts, the task stays `running`,
  and the hint names `hub_submit_for_review` — a fix is new commits, so
  delivery needs a new review (#612), never another done report. Terminal
  (`merge_failed`, closed PR, `no_pr`, `merge_gate_error`, `ci_untested` past
  the window): a human acts, `needs_decision` as before.
  Waiting on a recoverable refusal is bounded twice, because two different
  things can go wrong: the fix budget (`ci_fix_cycle` vs `MAX_CI_FIX_CYCLES`,
  charged once per submission — the same budget the headless conveyor spends)
  stops an executor that keeps failing, and session presence stops a task
  nobody works on any more. `running:pair` has no machine deadline, so the
  #418 backstop does NOT cover this — the bounds above are the only ones.
- Human overrides bypass the gate by design and stay audited: `hub_decide_task`
  accept and `force_complete`.
- Parent rollup: completing the last child `task` under a `feature` (or the last
  `feature` under an `epic`) auto-completes the parent when all siblings are
  `completed` (idempotent).
- `force_complete` is the audited human override for `task`/`subtask` rows:
  allowed from any non-terminal status when no *active* dispatch job backs
  `job_id` or `review_job_id` (409 if active; missing/terminal jobs are
  audited and allowed). A non-empty comment is required for active lifecycle
  states other than `pending_report`/`claimed` (those two may fall back to
  the default audit message). Clears stale claim metadata. Rejects terminal
  tasks and `epic`/`feature` rows with incomplete descendants.
- Write serialization (`get_write_lock`) covers refinement/AC paths,
  `create_subtasks_bulk`, and the lifecycle completion paths (`add_update`
  done-flow, `force_complete_task`). It is NOT yet a full per-connection
  commit lock; broad commit serialization is tracked as hardening work.
- Approval is only valid from `draft`.
- Approval uses an atomic conditional transition to avoid double-processing races.
- If required DoR checks fail and `force` is not set, approval must fail with HTTP 422.
- If concurrent approval loses the race, the caller should see HTTP 409, not silent success.

## Structured Form Invariants

- Structured fields are stored on the `tasks` row except acceptance criteria, which live in a separate table.
- Risks are stored as JSON on the task row.
- `TaskRefine` is PATCH semantics: omitted fields must remain unchanged.
- Unknown `work_type` falls back to the strict `feature` DoR profile.

## Surface Alignment Invariants

- REST API is the canonical behavior surface.
- MCP tools should mirror API behavior; they should not introduce separate business rules.
- CLI should stay behaviorally aligned with API contracts and error handling.
- If request or response models change, affected API, CLI, MCP, and tests should be reviewed in the same pass.

## Persistence Invariants

- Schema changes belong in `hub/db.py` migrations.
- Repository helpers must serialize and deserialize structured list/JSON fields consistently.
- It is safer to fail loudly on missing structured columns than silently treat them as empty.

## Integration Invariants

- Core code depends on plugin protocols, not concrete integrations.
- No-op plugins are valid runtime behavior and should keep the app usable without external binaries.
- Dispatch, git, GitHub, notes, transcripts, and Vast integrations are optional adapters, not prerequisites for core task CRUD.
- Review not bought on red CI (#1405): the decision is made ONCE, in `_policy_and_novelty_allow` right after the policy check and before the ladder/cascade/ask-again bypass, from the CI report for the PINNED sha present at that moment. A report naming `fail` (checks or `validation_status`) buys no run and writes one `review_withheld_red_ci` event per generation with the failed checks and the way out (resubmit or `machine_review_override=require`); a red report refuses every entry, including a ladder/cascade/ask-again top-up after a run was already bought (then one `review_topup_withheld_red_ci` event instead); `unknown`/`skipped` are not red. No report: the order goes as before, plus one `review_ordered_without_ci` line per generation. There is NO deferred order: neither `accept_ci_run_report` nor the poller orders a review. Events are deduplicated by `events` kind + `payload.generation`, never by feed text.
- Review circle stops deep (#1432): an unnamed finding outcome is unknown and does NOT break the circle chain; only a clean complete report or an explicit refusal (`false_positive`/`not_a_defect`/`wont_fix`) of ALL findings of a generation does. At `circle_deep_stop` laps (gate_policy key, else `REVIEW_CIRCLE_DEEP_STOP`, default 2; 0 or unreadable = off) a submission the rule gives deep gets lite with the reason `круг: N заходов, deep приостановлен до решения человека` plus the cancelled deep reasons, and the ladder (#879) / second axis (#1243) forced deep is refused in `_policy_and_novelty_allow` with a named reason. `machine_review_override=require` and a declared security risk keep deep. One `review_circle_deep_stopped` event + alert per generation, deduplicated by `events` kind + `payload.generation`.
- Review-dispatch billing (#1026): `cursor_cloud.get_usage` is stamped on `review_dispatches.provider_tokens` for every terminal close (`done` and `failed`). NULL is unknown; 0 is a billed zero. Wasted spend of failed runs is a sibling of `machine_reviews` and must not enter `tokens_per_confirmed` / `provider_tokens_per_confirmed` (#516).
- Shared project workspace (`workspace_path`): pair-start may auto-switch away from a **clean, pushed** `task-N/*` branch (#451); dirty or unpushed foreign branches still block with 422. After submit-for-review, report-done, or release, Hub best-effort checks out the project base branch when the workspace is clean and on that task's branch.
- Pair-start `git_mode=remote` (#975) records the canonical `task-<id>/<slug>` name and skips host git at prepare, restore (submit/done/release), and switch (CHANGES_REQUESTED / worktree recreate). Omitted/`hub` keeps today's laptop path. Remote submit-review on a project without `repo`/`gh_repo` names that diff/PR could not be made on the response (lifecycle_hint); it must not look like empty success. Laptop `git_mode=hub` still treats "could not look" as not an accusation (#498). GET `/api/tasks/{id}` and `/context` fill `worktree_path` only when a pair worktree is still registered (`worktree_is_registered`), not by status (#989): `submit_for_review` removes the tree; headless `start_task` never creates one; `git_mode=remote` has no hub-host tree. A session.workspace mismatch is an advisory line on `/context`, never HTTP 409.
- Session registry ownership (#977): `POST /api/sessions/register` must not overwrite another principal's `principal_id` or `agent` (HTTP 409, row unchanged). `POST /api/sessions/{id}/heartbeat` from a foreign principal is HTTP 404 with the same body as an unknown id and must not bump `last_seen_at`. Same-principal re-register stays 200 and refreshes `last_seen_at`.
- Chat-pair implementer (#980): sibling `kind` on the same code machinery, not a flip of intake #961. Intake `role=human` / `CHAT_PAIR_PERMS` / create stay. Implementer is issued only from `open`, acts as `role=agent` with `CHAT_PAIR_IMPLEMENTER_PERMS` (no `tasks.create`), and `{task_id}` outside `bound_task_id` is 403 `chat_pair_gate_forbidden`. Revoke is scoped by kind so intake and implementer do not kill each other. Missing acting principal is 503 on issue and indistinguishable 401 on redeem. The open task card issues that code (#981); `/chat-pair` stays intake copy and counts only intake sessions. Operator path is guide §4b, not §4a. Cloud implementer pair-start uses `git_mode=remote` so host git is skipped (#975) — that skip is the same invariant as the previous bullet, not a second git mode. Implementer session TTL is the same `CHAT_PAIR_TTL_SECONDS` as intake; there is no renew (#983, allowlist stays closed). When the last live implementer session for that bound task expires or is revoked while the task is still `running` or `claimed` (pair, no `job_id`), the chat-pair reaper returns the task to `open` with a recorded status update before deleting the session row. A dead sibling must not yank a task that still has another unexpired, unrevoked implementer session. Intake expiry does not move tasks. Review/completed are left alone.
- Steward principal (#1021): role `steward` is neither human nor agent. `is_human` is false even when `tasks.human_gate` is absent and `principal_id` is set; `is_agent` is false so "not an agent" must not be read as "a human". Access is deny-by-default: `STEWARD_ALLOWLIST` is two routes (GET evidence pack, POST judgement). A new route is 403 until it is listed explicitly. `CHAT_PAIR_PERMS` is the pattern, not the permission set.
- Steward judgement contract (#1022): `POST /api/tasks/{id}/steward-judgement` records a structured judgement and does not transition the task. `verdict` has no default (422). `ground.source` and `escalate_reason` are closed sets with no `unknown`. `confidence=low` is stored as `escalate` / `low_confidence`; an `approve` or `changes_requested` with no grounds is stored as `escalate` / `no_grounds`, and with empty confidence as `escalate` / `no_confidence` (#1327) — same place, `submitted_verdict` keeps what was filed; an `escalate` needs neither. At-most-once on `(task_id, generation, kind)`. Closures address findings by `finding_uid` of that generation's report.
- Steward advisor and exit criterion v2 (#1601): the judge's `approve` is never a verdict by itself — recording it applies nothing on any path. The advisor is a separate order (`steward_runs.kind='advisor'`, ordered only after an approve of the new contour) and a separate judgement (`steward_judgements.kind='advisor'`, verdict `concur|object`, closed vocabulary), written only from a `steward_advisor` chat-pair session (a judge session gets 403 `steward_advisor_channel`, and the other way round), and only under an open STARTED advisor order. The hub binds the answer: `judged_id` (the judge row), generation, `packet_hash` (stamped on the run row when the door serves the packet, kind-aware: a judge reads under a verdict order, the advisor under an advisor order) and the model of the RUN. Family: not the implementer's, not the reviewer's, not the judge's ACTUAL run model (`steward_runs.model` after a #1182 substitution), only from `SUBSCRIPTION_LAUNCHABLE_MODELS` (`STEWARD_ADVISOR_MODELS` order); none fits = a refused order with the reason, never a same-family advisor. States are derived from rows: `not_ordered|pending|received|refused|timeout`; reply window `STEWARD_ADVISOR_WAIT_MAX` (60 min from the order, start and work together). `steward_advisor.advisor_refusal` is the ONE reader of consent (concur, bound to this approve, judge and advisor saw the same hash, generation live, packet unchanged) and is asked by `apply_judgement` (the verdict write), `apply_refusals` (the gatekeeper) and `apply_self_approval` (live path). The poller step `apply_advisor_outcomes` (act only) claims `steward_judgements.advisor_outcome` with a conditional UPDATE: applied exactly once, survives a restart; the verdict write AND the final outcome are conditional on the claim (`advisor_outcome='applying'` + `advisor_claimed_at` of that pass) in the same statement, and a claim older than `STEWARD_ADVISOR_WAIT_MAX` is moved to `escalated` with one alert (`_escalate_interrupted`). Exit criterion (`steward_exit.act_refusals_v2`): sample = judge rows with `contour=2` (after rollout); pairs >= 20; false approve = 0 (a pair CONCURRED, then at ANY moment after it: a human `changes_requested` on that or a later generation, an exit of the task from `completed`, or a `found_in='prod'` defect with `caused_by_task_id` on it; no window, no delivery anchor; `found_in='review'` is not one; each fact is written BY A DB TRIGGER in the transaction of the event itself — `trg_false_approve_*` on events/tasks, actor list = `NON_HUMAN_GATE_ACTORS` pinned by a test — and the criterion reads only the table; the tick poll is insurance); share = (judge escalations except `no_current_report`/`report_incomplete` + advisor objections) / (all new-contour judge rows minus those two) within 5%..50%. `precondition_failed` is substantive. A false approve is persisted in `steward_false_approvals` keyed by (task, source, case ref), is global (one mode per hub) and is lifted — per CASE, never per task, and not in advance — only by `POST /api/tasks/{id}/steward-false-approve/clear` / `oc-hub steward-false-approve-clear` (human only, no MCP tool — catalog budget). The weekly corridor (`weekly_sample`) uses the same classification. The 2x2 table «steward against human» stays as a reference metric (`practice_metrics.steward_shadow.human_table`) and no longer decides the exit.
- Outcome due date (#1568): one function, `outcomes.resolve_outcome_status`, decides a task's outcome status for the card, the review brief and `outcome_debt`. Due = first successful deploy of `released_sha` of the task's LAST merge (`repository.first_fix_deploy_at`: `pipeline_merges` → `releases`, sha trim+lower, NO project_id — releases before 28.09 have `project_id=NULL`) + `OUTCOME_WINDOW_DAYS` (14). `epic`/`feature` take the latest such deploy among the task and its descendants. No recorded fix release (no merge, unreleased merge, sha without a release row) is `unknown`, never `unanswered`; the one exception is a project whose owner set `gate_policy.merge_is_delivery=true` (#1572, bool, default false, never inferred from branches): for a task WITHOUT a stamped merge the project of its LAST merge (`pipeline_merges.project_id`, not `tasks.project_id`) dates it by its first successful `releases` row with `deployed_at >= merged_at`, or by `merged_at` itself when the project has no successful releases (`assumed`: `resolve_outcome_status` returns it, `outcome_debt` shows `due_assumed`; a rolled-up epic is assumed if any dated descendant is); releases exist but none after the merge stays `unknown`; nothing is stamped or written; `outcome_debt` reports `overdue`/`observing`/`unknown` with `*_total`, `due_on` and `due_assumed`. `outcome_deadline` is still free text and never parsed (#839). No migration, no column.
- Prod defect type (#1565, #914): `found_in='prod'` is refused when the FINAL `work_type` is `feature`; chore, spike, refactor, docs, bug, incident stay allowed. One rule, `repository.defect_stage_problem`, judged on the pair after the payload's `work_type` is applied (the stored one fills the gap), called from `_apply_refine_writes` BEFORE any write (REST refine, refine-bulk, MCP and CLI all go through it) and from `set_defect_passport` (direct writer). A call that sets `work_type=feature` on a `found_in='prod'` row is refused too, and a refused item rolls back the whole bulk. Rows already breaking the rule are not refused for an unrelated edit and are cleaned by hand; the refusal text says to set `work_type=bug` in the same call.
- Path notices (#1589): gate_policy key `path_notices` is a list of up to 50 `{pattern, text}` rules (pattern <= 200 chars, text 1-500). Pattern semantics are fixed in `hub/services/path_notices.py`: `*` never crosses `/`, a whole `**` segment is any depth including zero (`deploy/**/*.service` covers `deploy/x.service`), a trailing `/**` is any tail; risk_class's fnmatch (where `*` crosses `/`) is NOT used. A project without the key or with an empty list gets nothing new anywhere and no git read. With rules, every submission entrance (`submit_for_review`, `HEADLESS_STEPS`/the shared done transition, the `pending_report` done route) reads paths with `git_ops.branch_touched_paths` (`--name-status -z -M`: BOTH ends of a rename, deletions) for the pinned submission sha — `branch_diff_paths` and the surface check keep their own source and behaviour. The result `{generation, sha, state: matched|none|unknown, reason, notices}` is stored once per generation in `path_notice_results` (table, not an event: events are pruned after 14 days) in the caller's transaction, is never rewritten by a later policy edit, and is shown only for the CURRENT generation: submit response (`path_notices`), one feed line, the review brief (REST/MCP) and the verdict card (escaped). An unreadable diff is `unknown` with a reason, never silence. Same-sha resubmission returns the stored result and writes no second feed line. The notice proves nothing about the step being done and blocks nothing.
- Release artifacts (#1591): gate_policy key `release_artifacts` is a list of up to 20 `{repo_path, server_path, update_hint}` pairs (exact copies only). Before the auto-release merge (`release._merge_release_pr`), the PR head is pinned FIRST (`pr_head_sha`, before the CI probe), the sha256 of the repo file at that head is taken over the RAW blob bytes (`release_artifacts.repo_blob_sha256`, never `file_at_ref`: it decodes and strips) and compared with the server file read by `release_artifact_paths.server_sha256` (only inside `HAIPLANE_RELEASE_ARTIFACT_DIRS`, component-boundary check, no symlink in the path or the file, regular files only, rechecked at every read). Any mismatch, missing or unreadable file starts the reason with `серверная копия <path>` — `release_alert` classifies that start as ALERT (checked before the probe markers, so an `update_hint` cannot turn it into a probe). The merge passes `expected_head_sha` (`gh pr merge --match-head-commit`); a moved head reads as «не проведён… сверка повторится». Without the key or with an empty list the merge call is unchanged. A matching file proves the file on disk, not the running process.
