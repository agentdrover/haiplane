# Contracts

## Canonical Sources

| Contract Type | Source |
|---|---|
| Domain enums and request/response models | `hub/models.py` |
| REST routes | `hub/app.py` |
| Web routes | `hub/web.py` |
| CLI surface | `hub/cli.py` |
| MCP tool surface | `hub/mcp_server.py` |
| Integration interfaces | `hub/integrations/protocols.py` |
| DB schema and migrations | `hub/db.py` |

## Surface Rules

- Prefer changing models first, then the service or repository behavior, then the entry surfaces.
- Do not add a new business rule only in CLI or only in MCP.
- MCP tools should call the same API semantics the web and CLI rely on.
- Plugin interface changes are contract changes; update protocol, registry assumptions, concrete plugin, and noop implementation together.

## Common Contract Bundles

### Adding a task field

- `hub/models.py`
- `hub/db.py`
- `hub/repository.py`
- API endpoint handling in `hub/app.py`
- refine/readiness/recommendation services if relevant
- CLI flags or file import paths in `hub/cli.py`
- MCP tool arguments if the field must be agent-visible

### Adding a task kind that automation must not touch

- the single predicate in `hub/services/result_kind.py` (`automation_not_applicable`), called by every door listed in `invariants.md` "State task (#1647)"; a new door of automation (review order, steward order, auto-approve, executor order) calls it, it does not compare `result_kind` itself
- DoR profile in `hub/services/dor.py`, snapshot fields in `hub/services/dor_snapshot.py`
- submit/verdict branches in `hub/services/lifecycle.py` route to `hub/services/state_task.py`; the commit pipelines (`SUBMIT_STEPS`, `VERDICT_STEPS`) are not edited
- REST/CLI/MCP: `result_kind`/`rollback` on create and refine, `evidence` on submit-review, `expected_generation` on review-verdict (web: hidden form field); the MCP catalog budget moves only down

### Showing a state task (`result_kind=state`) on the gate and in dependencies (#1648)

- ONE reader of "what the human accepts": `hub/services/state_review.py` (`current_submission`, `state_review_view`). Review queue, inbox, verdict route, review brief (REST + MCP text) and the task card read it; none of them counts evidence completeness or compares `result_kind` itself. The kind comes from `result_kind.automation_not_applicable`.
- Readiness for a verdict is the COMPLETE evidence kit of the CURRENT generation against the submission's snapshot (`submissions.state_snapshot`), not a machine report. `review_queue` row: `readiness=ready`, `result_kind=state`, `sha_check` and `report_status` = `not_applicable`, `evidence_count`/`evidence_complete`. Inbox: action `verdict`, `offer` true only with a complete kit. `verdict_route`: final `human`, code `state_task_no_automation`, the reason text of the automation doors.
- Brief (`ReviewBrief`): `result_kind`, `state_review` (`StateReviewView`: `generation`, `expected_generation`, `rollback`, snapshot `acceptance_criteria`, `evidence` of the current generation with `author`/`observed_at`, `evidence_complete`, `missing_ac`, `not_applicable`). Code checks (`sha_check`, `ci_run_report`, `ac_tests`, `base_merge`, `call_sites`, `path_notices`, machine review) are `not_applicable` with a reason, never `unknown`/`match`/`pass`. The state brief reads DB rows only: no git, no network, no repository walk (`build_state_brief`). A new block of the brief that touches code must be skipped there and named in `NOT_APPLICABLE_CHECKS`.
- Card: label "результат: состояние", rollback, evidence table, hidden `expected_generation`; a stale web form gets the refusal in place (plain post: redirect with the reason; htmx: 409), nothing is written.
- Dependencies: ONE reader, `delivery_state.blocker_delivery` (every enrichment — REST, context, start warning, queue, project path, steward evidence — goes through it). A state blocker is ready exactly when `result_kind.state_accepted` (completed + APPROVED verdict on the current generation); delivery path `state_accepted`/`state_pending`. No `pipeline_merges`, no `releases`, no git. A parent (feature/epic) with state and commit children is delivered when it is completed and every non-rejected child is ready by ITS OWN rule: state child by acceptance, commit child by delivery. Returning a task to work changes its status and takes the readiness away without any write.
- A new surface that reads a task in review must branch on the predicate or call the readers above.

### Adding or changing a status

- `hub/models.py`
- lifecycle and orchestration logic
- poller transitions
- status rendering in UI
- tests covering transitions and progress

### Adding a new integration capability

- `hub/integrations/protocols.py`
- noop implementation
- concrete adapter
- registry wiring
- calling service or poller path

## Contract-Smell Checklist

- Does the same concept have different names across API, CLI, MCP, and DB?
- Did a new enum value get added without a migration, rendering path, or tests?
- Did a “small” contract change bypass `hub/models.py` and get hardcoded in a route?
- Did an adapter change leak implementation assumptions into core services?
