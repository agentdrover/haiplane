# ADR 0001: Branch policy schema & resolver

| Field | Value |
|-------|-------|
| Status | **Proposed** |
| Date | 2026-07-04 |
| Hub | Epic [#236](https://agenthai.ru/tasks/236) — Branch policy |
| Feature | [#237](https://agenthai.ru/tasks/237) — Schema & branch policy resolver |
| Deciders | Hub maintainers |
| Supersedes | — |
| Superseded by | — |

## Context

Haiplane Hub already persists `branch` and `pr_number` on tasks and runs a
post-done CI conveyor (`transition_after_agent_done`, poller). Branch names are
created ad hoc in `git_ops` as `task-{id}/{slug}` with slug from title.

Problems today:

1. **No policy source** — rules are copy-pasted into `constraints` on every
   feature/task (pain seen on epic trees like #209).
2. **No inheritance** — epic cannot declare git rules once for descendants.
3. **No pre-start visibility** — `branch` is empty until `pair_start` /
   `dispatch`; UI and MCP cannot show the expected branch name.
4. **Base branch drift** — pair mode uses `PAIR_BASE_BRANCH` (default
   `develop`); headless `create_branch` hardcodes `main`.
5. **No shared resolver** — naming logic lives inside `git_ops._slugify` and
   branch templates; downstream features (#238–#242) need one entry point.

Feature **#237** delivers the **policy → computed context** layer only.
Wiring `git_ops`, lifecycle hooks, DoR, and UI are explicitly out of scope
(features #238–#242).

## Decision

### 1. Store policy on epic only

Add a nullable JSON column `branch_policy` on `tasks`.

- **Writable** only when `task_type = epic` (enforce in refine/create services;
  return HTTP 422 otherwise).
- **Readable** on any task via resolver (inherited, not copied to children).
- Empty / NULL means “no inherited policy” (legacy behaviour unchanged).

Rationale: policy is a project/epic contract, not a per-task free-text field.
Keeping one column avoids a new table and matches existing structured-field
patterns (`risks`, `constraints` as JSON TEXT).

### 2. `BranchPolicy` contract (Pydantic)

```python
class BranchPolicyMode(str, Enum):
    one_task_one_branch = "one_task_one_branch"
    # Reserved for later ADRs — parse but do not implement in #237:
    one_feature_one_branch = "one_feature_one_branch"
    direct_to_main = "direct_to_main"


class BranchPolicy(BaseModel):
    mode: BranchPolicyMode = BranchPolicyMode.one_task_one_branch
    base: str = Field("main", min_length=1, max_length=100)
    naming: str = Field("task-{id}/{slug}", min_length=1, max_length=200)
    require_pr: bool = True
    repo: str = Field("", max_length=200)  # empty → hub default repo
```

Defaults mirror today’s de-facto rule (`1 task = 1 branch = 1 PR`) while
allowing epic override of `base` (fixes pair/headless drift in #238).

**Naming template** (v1): only placeholders `{id}` and `{slug}`.

- `{id}` — task id of the leaf work item (task/subtask), not feature id.
- `{slug}` — resolved slug (see §4).

Validation rules:

- `naming` must contain `{id}` when `mode = one_task_one_branch`.
- Reject unknown placeholders at parse time.
- `mode != one_task_one_branch` → HTTP 422 on write in #237 (reserved).

### 3. Resolver service (`hub/services/branch_policy.py`)

New pure service module; no git subprocess calls.

```python
@dataclass(frozen=True)
class ResolvedBranchPolicy:
    source_epic_id: int
    policy: BranchPolicy


@dataclass(frozen=True)
class BranchContext:
    """Read-only computed git context for API/MCP/UI."""

    branch_name: str | None      # expected or frozen actual
    base_branch: str | None
    pr_url: str | None
    policy_source_epic_id: int | None
    resolved_policy: BranchPolicy | None
```

Public functions:

| Function | Responsibility |
|----------|----------------|
| `resolve_branch_policy(db, task_id)` | Walk ancestors (reuse `get_breadcrumb`), return nearest epic with non-empty `branch_policy`, else `None` |
| `slug_for_branch(task, *, branch_slug: str = "")` | Shared slugify; move algorithm from `git_ops._slugify` to `hub/slugify.py` (or `branch_policy.py`) so services do not import integrations |
| `render_branch_name(task, policy, *, branch_slug: str = "")` | Apply `policy.naming` with `{id}`, `{slug}` |
| `resolve_repo(policy)` | `policy.repo` or `config.REPO_NAME` |
| `pr_url_for(pr_number, repo)` | `https://github.com/{owner}/{repo}/pull/{n}` when both set |
| `build_branch_context(db, task_row)` | Orchestrates resolver + render + frozen branch + pr_url |

**Inheritance rule:** closest epic wins. Feature-level override is **not** in v1
(you cannot set `branch_policy` on feature).

**Cycle safety:** reuse breadcrumb `seen` set pattern from `get_breadcrumb`.

### 4. Slug semantics & freeze

| State | `branch_name` source |
|-------|----------------------|
| `task.branch` is non-empty | Use stored `branch` (**frozen** after first `pair_start` / dispatch — enforced in #239, documented here) |
| `task.branch` empty | `render_branch_name(...)` from inherited policy |
| No policy | `branch_name = None`, `base_branch = None` (callers keep current behaviour) |

Slug precedence when computing expected name:

1. Explicit `branch_slug` argument (pair-start body; not persisted in #237)
2. Future: optional persisted `branch_slug` column (defer unless needed in #239)
3. `slug_for_branch(task.title)`

Slug algorithm (unchanged from today):

```python
re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()[:40].rstrip("-")
```

### 5. API surface (TaskView + refine)

Add to `TaskView` (always populated on single-task GET; optional on list views):

```python
branch_context: BranchContext | None = None
```

Flat denormalized fields are **not** added to avoid drift; consumers read
`branch_context.branch_name`, etc.

Extend `TaskRefine` with:

```python
branch_policy: BranchPolicy | None = None  # epic only; None omitted = unchanged
```

REST `PATCH /api/tasks/{id}/refine` — same contract. MCP `hub_refine_task` —
new optional parameter `branch_policy` (dict).

`GET /api/tasks/{id}` and `row_to_task` call `build_branch_context` when
assembling the view. List endpoints may omit `branch_context` for payload size
(document in `docs/agent-context/contracts.md`).

### 6. Migration

```sql
-- name: add_branch_policy_column
ALTER TABLE tasks ADD COLUMN branch_policy TEXT;
```

Serialization: `json.dumps(policy.model_dump(), ensure_ascii=False)`; NULL when
unset. Deserialize with Pydantic; invalid JSON on epic → log warning, treat as
no policy (fail open for reads, 422 for writes).

Add `branch_policy` to `STRUCTURED_TASK_FIELDS` / repository serialize path.

### 7. Out of scope (#237)

| Item | Owner feature |
|------|---------------|
| `git_ops` checkout/create using resolver | #238 |
| `pair_start` / `dispatch` / `report_done` hooks | #239 |
| DoR `has_branch_policy` | #240 |
| MCP/CLI doc parity beyond refine param | #241 |
| Web badges / tree column | #242 |
| `one_feature_one_branch`, `direct_to_main` behaviour | Later ADR |

## Consequences

### Positive

- Single resolver for all git-aware features.
- Epic declares policy once; descendants get computed context without copy-paste.
- Explicit contract for #238–#239 to consume.
- `base` on policy removes env-level guesswork per project.

### Negative / trade-offs

- Extra DB read on task GET (breadcrumb walk). Mitigation: breadcrumb query is
  small; cache per request if hot path becomes an issue.
- `branch_context` is computed, not stored — PR URL depends on `pr_number`
  already on row.
- Epic-only write may feel restrictive; feature-level override deferred to avoid
  ambiguous inheritance.

### Risks

| Risk | Mitigation |
|------|------------|
| Invalid policy JSON on old rows | Read: treat as None; Write: Pydantic 422 |
| Template injection / weird branch names | Whitelist placeholders; slug charset filter |
| Slug changes after branch frozen | #239 freezes `branch` column; context prefers stored value |

## Alternatives considered

| Alternative | Why rejected |
|-------------|--------------|
| Policy in `constraints` text | Not machine-readable; no inheritance |
| Policy on every task | Copy-paste returns; epic is natural owner |
| Separate `branch_policies` table | Overkill for v1; JSON column matches `risks` |
| Store computed `branch_name` on task | Duplicates template logic; stale on title edit |
| Global config only (`HAIPLANE_PAIR_BASE_BRANCH`) | Cannot differ per epic/repo |

## Implementation checklist (#237)

### Models & DB

- [ ] `BranchPolicyMode`, `BranchPolicy`, `BranchContext`, `ResolvedBranchPolicy` in `hub/models.py`
- [ ] Migration `add_branch_policy_column` in `hub/db.py`
- [ ] Serialize/deserialize in `hub/repository.py` / `structured_fields_*`

### Service

- [ ] `hub/services/branch_policy.py` — resolver + render + `build_branch_context`
- [ ] `hub/slugify.py` — extract slug function from `git_ops` (git_ops imports slugify in #238)

### API / lifecycle assembly

- [ ] `row_to_task` attaches `branch_context`
- [ ] `refine_task` accepts `branch_policy` for epic only (422 for non-epic)
- [ ] `hub/mcp_server.py` — `hub_refine_task(branch_policy=...)`
- [ ] `hub/cli.py` — refine flag if CLI exposes structured refine

### Tests

- [ ] `tests/test_branch_policy.py` — resolver inheritance, render, freeze precedence, pr_url
- [ ] `tests/test_models.py` — BranchPolicy validation
- [ ] `tests/test_db_migrations.py` — column exists
- [ ] `tests/test_api_refine.py` — epic write / feature 422
- [ ] `tests/test_mcp_server.py` — refine passes branch_policy

### Docs

- [ ] `docs/agent-context/contracts.md` — `branch_context` field
- [ ] `docs/agent-context/change-map.md` — row for branch policy

### Validation

```bash
uv run ruff check hub tests
uv run ruff format --check hub tests
uv run pytest -q
```

## Acceptance criteria (from Hub #236)

- **AC-1:** Child task GET returns `branch_context.branch_name` and `base_branch`
  when ancestor epic has `branch_policy`, before `pair_start`.
- **AC-4 (partial):** Resolver returns `policy_source_epic_id` for readiness
  integration in #240.

## References

- Epic [#236](https://agenthai.ru/tasks/236) — Branch policy
- Feature [#237](https://agenthai.ru/tasks/237) — Schema & branch policy resolver
- `hub/integrations/git_ops.py` — current naming
- `hub/db.py:get_breadcrumb` — ancestor walk
- `docs/workspace-safety-policy.md` — pair worktree rules
- `docs/repository-rules.md` — branch naming convention
