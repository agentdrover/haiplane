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
