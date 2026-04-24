# Testing Agent

## Responsibility

- Add and maintain behavioral coverage for API, CLI, MCP, DB, and services.
- Reproduce regressions with a failing test before fixing when practical.
- Verify happy paths and error cases.

## Workflow

1. Mirror source structure inside `tests/`.
2. Prefer behavior-level assertions over implementation coupling.
3. Cover lifecycle transitions, readiness rules, and acceptance criteria flows.
4. Run the narrowest useful suite first, then broader validation if needed.

## Core Commands

- `uv run pytest -q`
- `uv run pytest tests/test_api.py -q`
- `uv run pytest tests/test_mcp_server.py -q`
