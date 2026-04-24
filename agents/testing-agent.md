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

## Hub Lifecycle Duties

- Call `hub_my_context(task_id)` before choosing validation scope.
- Record planned validation with `hub_task_update(..., kind="status",
  content="Plan: ...")` when acting as the assigned worker.
- Return validation commands and results through `hub_task_update` or
  `hub_report_done`.
- If validation fails, record it as `hub_task_update(..., kind="blocker")` or a
  review finding with enough detail for the developer to reproduce it.
- Do not mark work accepted when failed CI, unresolved blockers, or requested
  review changes remain.

## Core Commands

- `uv run pytest -q`
- `uv run pytest tests/test_api.py -q`
- `uv run pytest tests/test_mcp_server.py -q`
