# Hub Refactoring Plan — Architectural Review & Roadmap

## Current State

| Metric | Value |
|--------|-------|
| Python files | 12 |
| Total code | ~4 000 lines |
| Largest module | `app.py` — 1 565 lines |
| Tests | 0 |
| Coverage | 0% |
| DB indexes | 0 |
| Authentication | None |
| Classes | 0 (all module-level functions) |

### Dependency Graph

```
config ← db, app, dispatch, github, notes, transcripts, git_ops, vast
models ← app
integrations/* ← app
```

No circular dependencies.

---

## Problems (by severity)

### P0 — Critical (loss of maintainability)

#### P0.1 — `app.py` is a God Module (1 565 lines)

One file combines: HTTP API (20+ JSON routes), Web/HTMX (15+ HTML routes),
background poller, SQL queries, business logic, integration orchestration,
and template helpers.

**Impact:** untestable business logic; any change risks unrelated breakage;
merge conflicts in parallel work.

#### P0.2 — No Service Layer

Data flows directly: HTTP handler → SQL → response. No isolated business
logic callable from API, CLI, MCP, and tests uniformly. Three clients
(API, CLI, MCP) duplicate knowledge of paths and field semantics.

#### P0.3 — Zero Test Coverage

`AGENTS.md` declares `hub/tests/` but the directory doesn't exist.
`pytest` and `coverage` are not in dev dependencies.

### P1 — Important (data integrity)

#### P1.1 — SQLite FK Not Enforced

`PRAGMA foreign_keys = ON` is not set. `parent_id REFERENCES tasks(id)` is
declarative only — SQLite does not enforce it.

#### P1.2 — No Indexes

No indexes on `tasks(parent_id)`, `tasks(status)`, `tasks(task_type)`,
`task_updates(task_id)`. Performance degrades with data growth.

#### P1.3 — Migrations Swallow Errors

`ALTER TABLE` failures are caught by bare `except: pass`, migration is
marked as applied regardless. Schema-code desync risk.

### P2 — Hygiene (supportability)

#### P2.1 — Deprecated `/api/proposals` Routes Still Alive

No removal deadline, creates confusion with new task-based model.

#### P2.2 — Missing `response_model` on 4 API Routes

`/api/tasks/{id}/context`, `/api/dispatch/jobs`, `/api/transcripts`,
`/api/tasks/{id}/refresh` — weaker OpenAPI contract.

#### P2.3 — Broken Redirect

`GET /proposals` → `/tasks?source=agent`, but `web_tasks` does not read
`source` from query — filter is not applied.

#### P2.4 — API Filter Inconsistency

HTML `/tasks/list` supports `priority` filter; JSON `GET /api/tasks` does not.

#### P2.5 — Unused Config

`AUTO_REVIEW_DEFAULT` defined in `config.py` but never referenced in
application code (default lives in Pydantic `TaskCreate`).

---

## Target Architecture

```
hub/hub/
├── app.py              # FastAPI routes only (thin handlers)
├── web.py              # HTMX/HTML routes (thin handlers)
├── services.py         # Business logic (lifecycle, orchestration)
├── repository.py       # All SQL queries + DB access
├── poller.py           # Background task polling
├── models.py           # Pydantic models + enums (unchanged)
├── config.py           # Configuration (unchanged)
├── cli.py              # CLI → HTTP (unchanged)
├── mcp_server.py       # MCP → HTTP (unchanged)
├── integrations/       # External systems (unchanged)
│   ├── dispatch.py
│   ├── git_ops.py
│   ├── github.py
│   ├── notes.py
│   ├── transcripts.py
│   └── vast.py
├── static/
├── templates/
└── tests/
    ├── conftest.py         # fixtures: in-memory SQLite, AsyncClient
    ├── test_repository.py  # DB layer unit tests
    ├── test_services.py    # Business logic unit tests
    ├── test_api.py         # API integration tests
    └── test_models.py      # Pydantic validation tests
```

**Key principle:** HTTP handler → service → repository.
Business logic tests run without HTTP. API tests via `httpx.AsyncClient`.

---

## Metrics & Thresholds

### Module Size

| Metric | Threshold |
|--------|-----------|
| Max lines per file | ≤ 400 |
| Max lines per function | ≤ 50 |
| Max functions per module | ≤ 20 |

### Tests & Quality

| Metric | Threshold |
|--------|-----------|
| Line coverage | ≥ 60% |
| Unit tests for business logic | present |
| Integration tests for API | present |
| `ruff check` errors | 0 |
| `ruff format` diff | 0 |

### Architecture

| Metric | Threshold |
|--------|-----------|
| SQL queries outside repository.py | 0 |
| Business logic in HTTP handlers | 0 |
| All API routes have `response_model` | yes |

### Database

| Metric | Threshold |
|--------|-----------|
| `PRAGMA foreign_keys = ON` | yes |
| Indexes on FK and filter columns | yes |
| Migrations with silent error swallow | 0 |

---

## Execution Plan

### Feature 1: Extract Repository Layer

Extract all SQL from `app.py` into `repository.py`:

1. Create `hub/hub/repository.py`.
2. Move all `SELECT/INSERT/UPDATE/DELETE` queries from `app.py`.
3. Functions accept `aiosqlite.Connection` as first arg.
4. Keep `db.py` for connection management, schema, migrations only.
5. Update `app.py` imports — handlers call repository functions.
6. Verify: `ruff check`, manual smoke test.

### Feature 2: Extract Service Layer

Extract business logic from `app.py` into `services.py`:

1. Create `hub/hub/services.py`.
2. Move lifecycle transitions (approve, reject, start, question, answer, decide).
3. Move orchestration logic (dispatch, review cycle, CI fix cycle).
4. Services call repository; services called by handlers.
5. Move `_row_to_task`, `_enrich_task_view` to services.
6. Update `app.py` — handlers become thin wrappers.
7. Verify: `ruff check`, manual smoke test.

### Feature 3: Extract Poller & Web Routes

1. Move `_poll_running_tasks` + helpers to `hub/hub/poller.py`.
2. Move all `web_*` handlers to `hub/hub/web.py`.
3. `app.py` retains: lifespan, API routes, static mount.
4. Verify: each file ≤ 400 lines.

### Feature 4: Fix Database Layer

1. Add `PRAGMA foreign_keys = ON` in `get_db()`.
2. Add indexes: `tasks(parent_id)`, `tasks(status)`, `tasks(task_type, status)`,
   `task_updates(task_id)`.
3. Replace `except: pass` in migrations with `_column_exists()` check.
4. Verify: FK violation raises error; indexes appear in `.schema`.

### Feature 5: Test Infrastructure

1. Add `pytest`, `pytest-asyncio`, `pytest-cov` to dev deps.
2. Create `hub/tests/conftest.py` with in-memory SQLite fixture and
   `httpx.AsyncClient` fixture.
3. Write `test_repository.py`: CRUD, hierarchy validation, breadcrumb, tree.
4. Write `test_services.py`: lifecycle transitions (valid + invalid),
   orchestration mocks.
5. Write `test_api.py`: happy path for all endpoints, error cases.
6. Write `test_models.py`: Pydantic validation edge cases.
7. Target: ≥ 60% line coverage.

### Feature 6: API Hygiene

1. Add `response_model` to all API routes missing it.
2. Add `priority` filter to `GET /api/tasks`.
3. Fix `GET /proposals` redirect — make `source` filter work in `web_tasks`.
4. Mark deprecated proposal routes with deprecation headers + removal date.
5. Remove unused `AUTO_REVIEW_DEFAULT` from config or wire it up.

---

## Execution Order

```
Feature 1 (repository) → Feature 2 (services) → Feature 3 (poller+web)
    ↓
Feature 4 (database) — can run in parallel with 1-3
    ↓
Feature 5 (tests) — after layers are separated
    ↓
Feature 6 (hygiene) — after tests exist to verify changes
```

Feature 4 is independent and can start immediately.
Features 1→2→3 are sequential (each depends on the previous).
Feature 5 benefits from separated layers but can start with conftest early.
Feature 6 is low-risk cleanup after test safety net is in place.
