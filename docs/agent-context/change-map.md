# Change Map

Use this file to avoid blind repo-wide reading. Start from the row that matches the requested change.

| Change Area | Primary Files | Also Inspect | Likely Tests |
|---|---|---|---|
| Task status or lifecycle transitions | `hub/models.py`, `hub/services/lifecycle.py` | `hub/repository.py`, `hub/poller.py`, `hub/app.py` | `tests/test_services.py`, `tests/test_api.py`, `tests/test_poller.py` |
| Task hierarchy rules | `hub/models.py`, `hub/db.py` | `hub/services/lifecycle.py`, `hub/repository.py` | `tests/test_services.py`, `tests/test_repository.py` |
| Structured fields or task schema | `hub/models.py`, `hub/db.py`, `hub/repository.py` | `hub/services/refinement.py`, `hub/services/readiness.py`, `hub/cli.py`, `hub/app.py` | `tests/test_models.py`, `tests/test_repository_structured.py`, `tests/test_api_refine.py`, `tests/test_db_migrations.py` |
| Definition of Ready rules | `hub/services/dor.py`, `hub/services/readiness.py` | `hub/services/recommendations.py`, `hub/models.py`, `hub/services/lifecycle.py` | `tests/test_dor.py`, `tests/test_readiness.py`, `tests/test_recommendations.py`, `tests/test_api_approve_gate.py` |
| Approval gate or force-approve behavior | `hub/services/lifecycle.py` | `hub/services/readiness.py`, `hub/repository.py` | `tests/test_api_approve_gate.py`, `tests/test_services.py`, `tests/test_api.py` |
| REST API behavior | `hub/app.py` | matching service module, `hub/models.py` | `tests/test_api.py`, `tests/test_api_context.py`, `tests/test_api_refine.py` |
| Web dashboard behavior | `hub/web.py`, `hub/templates/` | `hub/services/dashboard.py`, `hub/static/style.css` | `tests/test_web.py` |
| CLI commands or UX | `hub/cli.py` | `hub/app.py`, `hub/models.py`, template files | `tests/test_cli.py` |
| MCP tool surface | `hub/mcp_server.py` | `hub/app.py`, `hub/models.py` | `tests/test_mcp_server.py`, `tests/test_api.py` |
| Persistence queries | `hub/repository.py` | `hub/db.py`, service callers | `tests/test_repository.py`, `tests/test_repository_structured.py` |
| DB schema or migrations | `hub/db.py` | `hub/repository.py`, `hub/models.py` | `tests/test_db_migrations.py`, repository tests |
| Plugin interfaces | `hub/integrations/protocols.py` | `hub/integrations/registry.py`, concrete plugin file, service caller | `tests/test_poller.py`, `tests/test_services.py` |
| Dispatch / review orchestration | `hub/services/orchestration.py`, `hub/poller.py` | `hub/integrations/dispatch.py`, `hub/integrations/git_ops.py`, `hub/integrations/github.py` | `tests/test_poller.py`, `tests/test_services.py` |
| Auth changes | `hub/auth.py`, `hub/app.py`, `hub/web.py` | templates if login/UI changes | `tests/test_auth.py`, `tests/test_web.py` |

## High-Risk Couplings

- Changing `TaskStatus`, `TaskType`, or structured enums usually affects API, CLI, MCP, persistence, and tests together.
- Changing task row fields usually means touching both migration logic in `hub/db.py` and serialization/deserialization paths.
- Changing DoR or readiness logic can break approval behavior even if the API schema stays the same.
- Changing plugin protocols is a cross-cutting contract change; inspect noop and real adapters together.

## Safe Read Order

1. Read the row for the requested change.
2. Open only `Primary Files`.
3. Confirm invariants in `docs/agent-context/invariants.md`.
4. Expand into `Also Inspect` only if the first pass shows actual coupling.
