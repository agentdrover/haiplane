# Testing Playbook

Pick the smallest useful validation set first, then widen only if the diff crosses boundaries.

## Baseline Commands

- Lint: `uv run ruff check hub tests`
- Format: `uv run ruff format hub tests`
- Full suite: `uv run pytest -q`

## By Change Type

| Change Type | First Tests |
|---|---|
| REST API route or error behavior | `uv run pytest tests/test_api.py -q` |
| Context / breadcrumb / child-view behavior | `uv run pytest tests/test_api_context.py -q` |
| Refine payloads or structured updates | `uv run pytest tests/test_api_refine.py -q` |
| Approval gate or DoR failures | `uv run pytest tests/test_api_approve_gate.py -q` |
| Models or enum validation | `uv run pytest tests/test_models.py -q` |
| Repository or SQL behavior | `uv run pytest tests/test_repository.py tests/test_repository_structured.py -q` |
| DB migration behavior | `uv run pytest tests/test_db_migrations.py -q` |
| Lifecycle and service logic | `uv run pytest tests/test_services.py -q` |
| DoR / readiness / recommendations | `uv run pytest tests/test_dor.py tests/test_readiness.py tests/test_recommendations.py -q` |
| MCP tools | `uv run pytest tests/test_mcp_server.py -q` |
| CLI | `uv run pytest tests/test_cli*.py -q` |
| Web dashboard and templates | `uv run pytest tests/test_web*.py -q` |
| Poller or orchestration flows | `uv run pytest tests/test_poller.py -q` |
| Auth | `uv run pytest tests/test_auth.py -q` |

## Expansion Rules

- If a change touches `hub/models.py`, run model tests plus the nearest consumer tests.
- If a change touches `hub/db.py`, run migration tests and repository tests.
- If a change touches `hub/services/lifecycle.py`, include API approve or service tests even if the diff looked internal.
- If a change touches `hub/mcp_server.py`, run MCP tests and the API tests that back the same operation.

## Regression Discipline

- Bug fixes should normally ship with a regression test.
- Schema changes should be validated against both fresh-state and migration scenarios.
- Contract changes should be tested at the narrowest public surface that exposes them.

## Universal Review Gate Changes

Any change that touches review-gate behavior (`completion_requires_review`,
`transition_after_agent_done`, `submit_for_review`, `record_review_verdict`,
poller review handling, `refresh_task`) must run the focused gate suites
before the full run:

```bash
uv run pytest tests/test_services.py tests/test_api.py tests/test_mcp_server.py tests/test_poller.py tests/test_cli*.py -q
```

Key regression anchors: stale-approval invalidation, gate routing of done
reports (review / ci_check / needs_decision at cycle limit), MCP envelope
projection (`awaiting=review`, `actor_hint`), poller convergence on
`transition_after_agent_done`, and the audited `force_complete` override.

## Web tests moved by theme (#1677)

`tests/test_web.py` was split by meaning into `tests/test_web*.py` (tasks and
inbox, `_queue`, `_projects`, `_policy`, `_review_verdict`, `_review_evidence`,
`_admin`, `_ui`); shared helpers and the autouse fixture
`_form_carries_policy_version` live in `tests/web_support.py`. Test bodies are
unchanged, but a test's nodeid changed with its file: old nodeids in closed
tasks (`tests/test_web.py::name`) are not rewritten (variant 1); find a test by
name with `grep -rn "def <name>" tests/test_web*.py`. Each file stays under
2000 lines and 200 000 bytes so the AC locator can read it (256 KB limit).
