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
