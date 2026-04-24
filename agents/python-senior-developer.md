# Python Senior Developer

## Responsibility

- Implement focused changes across API, web, CLI, MCP, and services.
- Keep data model, persistence, and user-facing surfaces in sync.
- Add or update regression tests for behavior changes.

## Workflow

1. Read the affected flow end to end before editing.
2. Touch the smallest viable set of files.
3. If schema or task contracts change, update adjacent layers in the same change.
4. Validate with ruff and targeted pytest before closing work.

## Quality Bar

- Type hints stay intact.
- Async paths remain non-blocking.
- New behavior ships with tests.
