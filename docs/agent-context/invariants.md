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
- Human overrides bypass the gate by design and stay audited: `hub_decide_task`
  accept and `force_complete`.
- Parent rollup: completing the last child `task` under a `feature` (or the last
  `feature` under an `epic`) auto-completes the parent when all siblings are
  `completed` (idempotent).
- `force_complete` is the audited human override and is allowed from
  `pending_report`, `claimed`, or pair `running` (no `job_id`); headless
  `running` (with `job_id`) is owned by the poller and is excluded.
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
