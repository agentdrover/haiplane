# Invariants

These are the rules most likely to be broken by “small” changes.

## Domain Invariants

- Task hierarchy is strict: `epic -> feature -> task -> subtask`.
- Agent-created tasks start as `draft`; human-created work usually starts as `open` unless run immediately.
- `epic` and `feature` items are created as `open` and do not auto-run.
- `subtask` items must not auto-enable review by default.

## Lifecycle Invariants

- Status values are defined in `hub/models.py:TaskStatus`.
- Final statuses are `completed`, `failed`, `rejected`.
- Active statuses are tracked by `ACTIVE_STATUSES` in `hub/models.py`; progress math depends on them.
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
