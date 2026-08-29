# Haiplane Hub Standalone

## General Rules

- Work from this repository root only.
- Keep changes narrow and intentional.
- Follow repository workflow rules in `docs/repository-rules.md`.
- **Merge into `main` auto-deploys to the production-like server** via
  `.github/workflows/ci.yml` (job `deploy`, runs after tests pass on push to
  `main`). Treat any merge to `main` as a release. See `deploy/CD.md`.
- Use `uv run pytest`; do not use `python -m pytest`.
- Use `uv add <package>` for dependencies.
- If a change touches API contracts, update `hub/cli.py`, `hub/mcp_server.py`, and affected tests in the same pass.
- If a change touches persisted task data or schema, update migrations in `hub/db.py`.
- Функция сверх бюджета сложности красит CI (`scripts/complexity_budget.py`,
  #1066). Пороги — в `pyproject.toml`, ledger исключений — в
  `docs/agent-context/complexity-budget.json`. Разбейте функцию; если это
  невозможно, внесите запись через `--update` и впишите `removed_by` руками —
  `task:#NNNN`, `permanent` с причиной или честное `unclaimed`. Запись
  снимается удалением строки: если нарушения больше нет, проверка сама
  потребует её убрать.
- If a change touches MCP tool docstrings or signatures, it changes the published
  `tools/list`: run `uv run python scripts/mcp_catalog_budget.py` (CI runs it too)
  and re-freeze the budget with `--update` only as a deliberate, explained decision.
- Prefer focused validation:
  - `uv run ruff check hub tests`
  - `uv run ruff format hub tests`
  - `uv run pytest -q`
  - `uv run python scripts/surface_parity.py` before submitting: names the
    contract surfaces (REST/CLI/MCP, metric and page, schema and storage) that
    usually change together and are missing from this diff. Warning only —
    always exits 0, so either change them too or say in the submission why they
    do not apply (#833).

## Fast Start For Agents

- Read `docs/agent-context/system-map.md` before exploring the codebase.
- Use `docs/agent-context/change-map.md` to decide which files and tests to touch.
- Read `docs/agent-context/invariants.md` before changing lifecycle, schema, DoR, or integrations.
- Read `docs/agent-context/testing-playbook.md` before choosing validation scope.
- If the task is mostly onboarding, architecture, or impact analysis, use `skills/project-context/`.

## Repo Map

- `hub/app.py`: FastAPI app and API routes
- `hub/web.py`: dashboard and HTMX routes
- `hub/services/`: lifecycle, readiness, orchestration, refinement
- `hub/db.py`: schema and migrations
- `hub/repository.py`: SQL access layer
- `hub/models.py`: contracts and enums
- `hub/cli.py`: `oc-hub`
- `hub/mcp_server.py`: MCP surface for agents
- `hub/cli_templates/work_types/`: task templates
- `tests/`: regression coverage

## Agent Roles

- `agents/architect-analyst.md`
- `agents/python-senior-developer.md`
- `agents/testing-agent.md`
- `agents/code-reviewer.md`
- `agents/devsecops.md`

## Project Skills

- `skills/project-context/`
- `skills/hub-development/`
- `skills/hub-testing/`
- `skills/hub-task-prep/`
