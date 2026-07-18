# Issue: Schema & branch policy resolver

> Copy-paste body for GitHub issue or Hub task comment.  
> Hub: Feature **#237** under Epic **#236**.  
> ADR: [`docs/adr/0001-branch-policy-resolver.md`](../adr/0001-branch-policy-resolver.md)

---

## Summary

Introduce epic-level **branch policy** storage and a shared **resolver** that
computes read-only git context (`branch_name`, `base_branch`, `pr_url`) for any
task in the subtree. This is the foundation for #238–#242; no `git_ops` or
lifecycle behaviour changes in this PR.

## Problem

- Branch rules are duplicated in `constraints` on every feature/task.
- `branch` is only set at `pair_start` / dispatch — nothing to show beforehand.
- Naming/base logic is embedded in `git_ops`; pair uses `develop`, headless uses
  `main`.
- Downstream features need one resolver entry point.

## Scope

### In

1. **`BranchPolicy` model** + `branch_policy` JSON column (epic-only write)
2. **`hub/services/branch_policy.py`**
   - `resolve_branch_policy(db, task_id)` — walk ancestors, nearest epic wins
   - `render_branch_name`, `build_branch_context`, `pr_url_for`
3. **Shared `hub/slugify.py`** — extract from `git_ops._slugify` (git_ops switch
   deferred to #238)
4. **`TaskView.branch_context`** — populated on single-task GET
5. **`TaskRefine.branch_policy`** — epic refine + MCP `hub_refine_task` param
6. Migration `add_branch_policy_column`
7. Tests + contract docs

### Out

- `git_ops` / `pair_start` / `dispatch` wiring (#238, #239)
- DoR check `has_branch_policy` (#240)
- Web UI (#242)
- Modes `one_feature_one_branch`, `direct_to_main` (parse only, 422 on write)

## Technical design

See ADR 0001. Key types:

```yaml
branch_policy:          # epic only, inherited by descendants
  mode: one_task_one_branch
  base: main
  naming: "task-{id}/{slug}"
  require_pr: true
  repo: ""              # optional override

branch_context:         # readonly on TaskView
  branch_name: task-215/s1-q1-q2-resolver
  base_branch: main
  pr_url: null | https://github.com/org/repo/pull/42
  policy_source_epic_id: 236
  resolved_policy: { ... }
```

**Freeze rule (documented, enforced in #239):** if `task.branch` is set, use it
as `branch_name`; else render from policy.

## Files (expected)

| Area | Files |
|------|-------|
| Models | `hub/models.py` |
| Migration | `hub/db.py` |
| Repository | `hub/repository.py` |
| Service | `hub/services/branch_policy.py`, `hub/slugify.py` |
| Assembly | `hub/services/lifecycle.py` (`row_to_task`) |
| Refine | `hub/services/refinement.py` |
| MCP | `hub/mcp_server.py` |
| CLI | `hub/cli.py` (if refine exposes new field) |
| Docs | `docs/agent-context/contracts.md`, `change-map.md` |
| Tests | `tests/test_branch_policy.py`, extend refine/migration tests |

## Acceptance criteria

- [ ] Epic #236 AC-1: child task GET shows `branch_context.branch_name` and
      `base_branch` before `pair_start` when epic has policy
- [ ] `resolve_branch_policy` returns closest epic; `None` when no policy
- [ ] `branch_policy` refine on non-epic → HTTP 422
- [ ] Invalid policy JSON on write → 422; on read → treated as no policy
- [ ] `pr_url` built when `pr_number` and repo are known
- [ ] Slug algorithm unchanged from current `git_ops` behaviour

## Test plan

```bash
uv run ruff check hub tests
uv run ruff format --check hub tests
uv run pytest -q tests/test_branch_policy.py tests/test_api_refine.py tests/test_db_migrations.py
```

Manual:

1. Set `branch_policy` on epic #236 via `hub_refine_task`.
2. `GET /api/tasks/{child_id}` — verify `branch_context`.
3. Refine same policy on feature #237 — expect 422.

## Branch

`task-237/branch-policy-resolver`

## Depends on

- Epic #236 (parent)

## Blocks

- #238 Git ops integration via resolver
- #239 Lifecycle hooks
- #240 DoR & readiness

## Risks

- Breadcrumb walk on every task GET — acceptable for v1; optimize later if needed.
- Epic-only write — document; feature override is post-MVP.

## LLM hints

- Read ADR 0001 before coding.
- Do not wire `git_ops` in this PR (#238 owns that).
- Keep resolver free of subprocess/git calls.
- API + MCP + refine in same pass per repository rules.
