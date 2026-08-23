---
name: hub-testing
description: Use when writing, fixing, or expanding tests for the standalone Haiplane Hub project, especially for API routes, MCP tools, CLI behavior, lifecycle transitions, readiness checks, and database regressions.
---

# Hub Testing

## Use This Skill For

- regression tests for bugs
- API and CLI behavior checks
- MCP surface verification
- readiness and approval gate coverage
- repository and migration validation

## Workflow

1. Start from the user-visible behavior or failure mode.
2. Map it to the narrowest existing test module in `tests/`.
3. Prefer a failing test first when reproducing a bug.
4. Validate both success and failure paths for lifecycle-sensitive changes.

## Test Targets

- `tests/test_api.py`
- `tests/test_api_approve_gate.py`
- `tests/test_services.py`
- `tests/test_mcp_server.py`
- `tests/test_db_migrations.py`
- `tests/test_cli.py`

## Commands

- `uv run pytest -q`
- `uv run pytest tests/test_services.py -q`
- `uv run pytest tests/test_mcp_server.py -q`
- `uv run pytest tests/test_db_migrations.py -q`
