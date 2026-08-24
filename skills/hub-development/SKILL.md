---
name: hub-development
description: Use when implementing or modifying code in the standalone Haiplane Hub project, including FastAPI routes, services, CLI, MCP tools, database migrations, web UI, and integrations.
---

# Hub Development

## Use This Skill For

- feature work in `hub/`
- bug fixes across lifecycle or readiness flows
- contract changes affecting API, CLI, MCP, or templates
- database or repository changes

## Workflow

1. Read the full path of the affected behavior before editing.
2. Keep model, repository, service, API, CLI, and MCP layers aligned.
3. If task fields or statuses change, inspect:
   - `hub/models.py`
   - `hub/repository.py`
   - `hub/services/`
   - `hub/db.py`
   - `hub/cli.py`
   - `hub/mcp_server.py`
4. Add or update tests in the same change.

## Validation

- `uv run ruff check hub tests`
- `uv run ruff format hub tests`
- `uv run pytest -q`
- `uv run haiplane-hub`

## File Map

- API app: `hub/app.py`
- Web UI: `hub/web.py`, `hub/templates/`, `hub/static/`
- CLI: `hub/cli.py`
- MCP: `hub/mcp_server.py`
- Schema and migrations: `hub/db.py`
- Work type templates: `hub/cli_templates/work_types/`
