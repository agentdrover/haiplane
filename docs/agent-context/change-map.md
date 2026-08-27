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
| MCP usage telemetry or the usage report | `hub/services/mcp_telemetry.py`, `hub/repository.py` | `hub/mcp_server.py` (call path), `hub/db.py`, `hub/app.py`, `hub/web.py` | `tests/test_mcp_telemetry.py`, `tests/test_agent_api_metrics.py`, `tests/test_db_migrations.py` |
| MCP catalog size or its CI budget | `hub/mcp_catalog.py`, `docs/agent-context/mcp-catalog-budget.json` | `scripts/mcp_catalog_budget.py`, `hub/mcp_server.py` (tool docstrings and signatures) | `tests/test_mcp_catalog_budget.py` |
| Persistence queries | `hub/repository.py` | `hub/db.py`, service callers | `tests/test_repository.py`, `tests/test_repository_structured.py` |
| DB schema or migrations | `hub/db.py` | `hub/repository.py`, `hub/models.py` | `tests/test_db_migrations.py`, repository tests |
| Plugin interfaces | `hub/integrations/protocols.py` | `hub/integrations/registry.py`, concrete plugin file, service caller | `tests/test_poller.py`, `tests/test_services.py` |
| Dispatch / review orchestration | `hub/services/orchestration.py`, `hub/poller.py` | `hub/integrations/dispatch.py`, `hub/integrations/git_ops.py`, `hub/integrations/github.py` | `tests/test_poller.py`, `tests/test_services.py` |
| Auth changes | `hub/auth.py`, `hub/app.py`, `hub/web.py` | templates if login/UI changes | `tests/test_auth.py`, `tests/test_web.py` |
| Chat pairing (code → short session) | `hub/services/chat_pair.py`, `hub/auth.py` | `hub/app.py` (start/redeem/revoke, create guard), `hub/web.py` + `hub/templates/chat_pair.html`, `hub/config.py`, `hub/db.py`, `hub/poller.py` | `tests/test_chat_pair.py`, `tests/test_auth.py`, `tests/test_db_migrations.py` |
| Chat-pair implementer (`kind=implementer`, one open task) | `hub/services/chat_pair.py`, `hub/auth.py` | `hub/app.py`, `hub/config.py` (`CHAT_PAIR_IMPLEMENTER_PERMS`), `hub/db.py` (kind/bound/acting columns), `hub/models.py`, `hub/web.py` + `hub/templates/task_detail.html` (card button #981) | `tests/test_chat_pair.py`, `tests/test_db_migrations.py`, `tests/test_web.py` |
| Session registry ownership | `hub/services/sessions.py`, `hub/repository.py` | `hub/app.py` (heartbeat passes principal), `hub/actionable_errors.py` | `tests/test_agent_sessions.py` |
| Pair-start git_mode (hub vs remote) | `hub/services/lifecycle.py`, `hub/models.py`, `hub/db.py` | `hub/cli.py`, `hub/mcp_server.py`, `hub/services/orchestration.py`, `docs/software-development-workflow.md` | `tests/test_services.py`, `tests/test_cli.py`, `tests/test_mcp_server.py`, `tests/test_db_migrations.py`, `tests/test_api.py` |

## High-Risk Couplings

- Changing `TaskStatus`, `TaskType`, or structured enums usually affects API, CLI, MCP, persistence, and tests together.
- Changing task row fields usually means touching both migration logic in `hub/db.py` and serialization/deserialization paths.
- Adding a REST route does NOT grant it to chat-pair sessions: access for `auth_source=chat_pair` is the deny-by-default allowlist in `hub/auth.py` (#961). Widening that list is a decision with a reason in the task, never a step in an unrelated change.
- Changing DoR or readiness logic can break approval behavior even if the API schema stays the same.
- Changing plugin protocols is a cross-cutting contract change; inspect noop and real adapters together.
- Editing a tool docstring or adding a parameter changes the published `tools/list` and eats headroom under the catalog ceiling (#780, #829). CI runs `uv run python scripts/mcp_catalog_budget.py` and prints how much headroom is left. Ordinary delivery fits under the ceiling and must NOT touch `mcp-catalog-budget.json` — that file is edited only when the ceiling itself is being raised (`--update`), with the reason stated.

## Safe Read Order

1. Read the row for the requested change.
2. Open only `Primary Files`.
3. Confirm invariants in `docs/agent-context/invariants.md`.
4. Expand into `Also Inspect` only if the first pass shows actual coupling.
