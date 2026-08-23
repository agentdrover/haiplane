# System Map

## Purpose

Haiplane Hub is a standalone task-orchestration server for AI-agent work.
It exposes the same domain through four main surfaces:

| Surface | Entry point | Purpose |
|---|---|---|
| REST API | `hub/app.py` | canonical write/read API for tasks, readiness, approvals, updates |
| Web UI | `hub/web.py` + `hub/templates/` | HTMX dashboard, inbox, task details, logs |
| CLI | `hub/cli.py` | operator and agent command-line workflow via `oc-hub` |
| MCP | `hub/mcp_server.py` | model-facing tools mapped onto the REST API |

## Core Domain

The central entity is a task with:

- hierarchy: `epic -> feature -> task -> subtask`
- lifecycle: `draft -> open -> running -> ci_check/review/fix_requested -> completed`
- structured form: `work_type`, scope, acceptance criteria, risks, validation commands
- orchestration metadata: runtime, assigned agent, job id, branch, PR, review cycles

Source-of-truth enums and request/response models live in `hub/models.py`.

## Layer Map

| Layer | Files | Responsibility |
|---|---|---|
| Contracts | `hub/models.py` | enums, payloads, views, readiness models |
| API/Web entry | `hub/app.py`, `hub/web.py` | route wiring, request parsing, response shaping |
| Service layer | `hub/services/` | lifecycle, readiness, recommendations, orchestration, dashboard, refinement |
| Persistence | `hub/repository.py`, `hub/db.py` | SQL, migrations, hierarchy helpers, structured field serialization |
| Integrations | `hub/integrations/` | plugin protocols and concrete adapters |
| Background sync | `hub/poller.py` | polling dispatch jobs, CI/review state progression |
| UX assets | `hub/templates/`, `hub/static/` | dashboard rendering |
| Agent API cost | `hub/services/mcp_telemetry.py`, `hub/mcp_catalog.py` | usage records per MCP call, usage report, catalog size and its CI budget |
| Verification | `tests/` | regression coverage by subsystem |

## Data Model Notes

- Structured task fields mostly live on the `tasks` row.
- Acceptance criteria live in a separate table.
- Risks are stored as JSON on the task row.
- `TaskRefine` is PATCH-like: omitted fields should remain unchanged.

## Architectural Boundaries

- `hub/services/` should contain business decisions; entrypoints should stay thin.
- Core code depends on plugin protocols from `hub/integrations/protocols.py`, not concrete implementations.
- MCP is a projection of the REST API, not an independent business layer.
- MCP usage telemetry records metadata only: there is no column for an argument value, a token or a response body (#780). Anything that would store one is a schema change, not a call-site change.
- CLI should stay aligned with API behavior; it is another client surface.

## Read Next

- For impact analysis: `docs/agent-context/change-map.md`
- For hard rules: `docs/agent-context/invariants.md`
- For contract-sensitive work: `docs/agent-context/contracts.md`
- For validation choice: `docs/agent-context/testing-playbook.md`
