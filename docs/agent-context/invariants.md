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
  «Жизненный цикл ветки задачи»).
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
- Shared project workspace (`workspace_path`): pair-start may auto-switch away from a **clean, pushed** `task-N/*` branch (#451); dirty or unpushed foreign branches still block with 422. After submit-for-review, report-done, or release, Hub best-effort checks out the project base branch when the workspace is clean and on that task's branch.
- Pair-start `git_mode=remote` (#975) records the canonical `task-<id>/<slug>` name and skips host git at prepare, restore (submit/done/release), and switch (CHANGES_REQUESTED / worktree recreate). Omitted/`hub` keeps today's laptop path. Remote submit-review on a project without `repo`/`gh_repo` names that diff/PR could not be made on the response (lifecycle_hint); it must not look like empty success. Laptop `git_mode=hub` still treats "could not look" as not an accusation (#498).
- Session registry ownership (#977): `POST /api/sessions/register` must not overwrite another principal's `principal_id` or `agent` (HTTP 409, row unchanged). `POST /api/sessions/{id}/heartbeat` from a foreign principal is HTTP 404 with the same body as an unknown id and must not bump `last_seen_at`. Same-principal re-register stays 200 and refreshes `last_seen_at`.
- Chat-pair implementer (#980): sibling `kind` on the same code machinery, not a flip of intake #961. Intake `role=human` / `CHAT_PAIR_PERMS` / create stay. Implementer is issued only from `open`, acts as `role=agent` with `CHAT_PAIR_IMPLEMENTER_PERMS` (no `tasks.create`), and `{task_id}` outside `bound_task_id` is 403 `chat_pair_gate_forbidden`. Revoke is scoped by kind so intake and implementer do not kill each other. Missing acting principal is 503 on issue and indistinguishable 401 on redeem. The open task card issues that code (#981); `/chat-pair` stays intake copy and counts only intake sessions.
