# OpenClaw Hub

Task orchestration server for AI agent pipelines. Manages task lifecycle, dispatches work to agents, runs automated code review cycles, and provides a web dashboard.

## Features

- **Task hierarchy**: Epic → Feature → Task → Subtask
- **Full lifecycle**: draft → open → running → review → completed (with CI checks, arbiter, Q&A)
- **Structured task form**: Kanban-style fields (work_type, scope, acceptance criteria, risks, validation), deterministic Definition of Ready gate, readiness score and recommendations — see [Structured task form & readiness](#structured-task-form--readiness)
- **Plugin architecture**: integrations (dispatch, git_ops, GitHub, Vast.ai, notes) are pluggable
- **Web dashboard**: HTMX-powered UI with inbox, kanban, task detail, log viewer
- **MCP server**: Model Context Protocol tools for Cursor/remote agents
- **CLI**: `oc-hub` command for agents and humans
- **Background poller**: auto-sync with dispatch jobs, stale detection, review dispatch

For the recommended human + AI-agent software delivery flow, see
[docs/software-development-workflow.md](docs/software-development-workflow.md).
For repository branching, commit, validation, and review rules, see
[docs/repository-rules.md](docs/repository-rules.md).
For the planned admin section covering users, AI agents, roles, keys, and
passwords, see [docs/admin-section-design.md](docs/admin-section-design.md).

## Quick Start

```bash
# Clone
git clone https://github.com/mrPDA/openclaw-hub.git
cd openclaw-hub

# Install
uv venv && uv pip install -e .

# Run
openclaw-hub
# → http://localhost:8080
```

## Cursor + Hub Workflow

Run the hub, connect Cursor to its MCP endpoint, and treat OpenClaw Hub as the
task state source. Step-by-step MCP setup (streamable URL, headers, stdio vs
HTTP, troubleshooting) is in
[docs/agent-mcp-operator-guide.md](docs/agent-mcp-operator-guide.md).
The delivery process is documented in
[docs/software-development-workflow.md](docs/software-development-workflow.md);
the staged rollout lives in
[docs/software-development-workflow-implementation-plan.md](docs/software-development-workflow-implementation-plan.md).
Cursor-specific rules are in
[docs/cursor-agent-rules.md](docs/cursor-agent-rules.md), with an installable
template at
[docs/templates/cursor/openclaw-hub.mdc](docs/templates/cursor/openclaw-hub.mdc).

Branch and workspace safety invariants for multi-agent work are in
[docs/workspace-safety-policy.md](docs/workspace-safety-policy.md).

Minimum MCP tools for daily work:

- `hub_project_status`, `hub_list_tasks`, `hub_task_status`
- `hub_my_context`, `hub_refine_task`, `hub_get_readiness`
- `hub_add_acceptance_criterion`, `hub_replace_acceptance_criteria`, `hub_add_risk`
- `hub_approve_task`, `hub_reject_task`, `hub_start_task`
- `hub_task_update`, `hub_ask_question`, `hub_report_done`
- `hub_force_complete_task`, `hub_decide_task`

## Configuration

All configuration via environment variables:

| Variable | Default | Description |
|---|---|---|
| `OPENCLAW_HUB_REPO` | `""` | GitHub repo (e.g. `owner/repo`) for PR/commit integration |
| `OPENCLAW_WORKSPACE_REPO` | `~/.openclaw/workspace/repo` | Path to the workspace git repo |
| `OPENCLAW_DISPATCH_BIN` | `~/.local/bin/oc-dev-dispatch` | Path to dispatch binary |
| `OPENCLAW_HUB_DB` | `~/.local/state/openclaw-hub/hub.db` | SQLite database path |
| `OPENCLAW_HUB_HOST` | `0.0.0.0` | Server bind host |
| `OPENCLAW_HUB_PORT` | `8080` | Server bind port |
| `OPENCLAW_TRANSCRIPTS_DIR` | `~/.openclaw/transcripts` | Agent transcript directory |
| `OPENCLAW_N4L_BIN` | `~/.local/bin/n4l` | notesforllm CLI path |
| `OPENCLAW_N4L_SPACE` | `""` | notesforllm space ID |
| `OPENCLAW_VAST_JOB_BIN` | `~/.local/bin/vast-openclaw` | Vast.ai CLI path |
| `GH_BIN` | `gh` | GitHub CLI binary |
| `OPENCLAW_MAX_REVIEW_CYCLES` | `3` | Max automated review cycles |
| `OPENCLAW_REVIEW_SELF_APPROVE` | `forbid` | `allow` lets the implementing agent submit its own review verdict (solo mode); such verdicts are audited: marked `self_approved`, logged as a warning, and badged in Web/MCP |
| `OPENCLAW_MAX_CI_FIX_CYCLES` | `3` | Max CI fix attempts |
| `OPENCLAW_STALE_MINUTES` | `30` | Minutes before a running task is flagged stale |
| `OPENCLAW_STALE_REVIEW_MINUTES` | `120` | Minutes before a client-driven review without a verdict is flagged stale |
| `OPENCLAW_STALE_CLAIMED_MINUTES` | `240` | Minutes before a claim without pair start is flagged stale |
| `OPENCLAW_STALE_NEEDS_INFO_MINUTES` | `480` | Minutes before an unanswered question is flagged stale |

## Structured task form & readiness

Hub treats each task as a Kanban work item with a structured form, a
deterministic **Definition of Ready** gate, and a numeric readiness
score that drives recommendations. The goal: by the time a task moves
from `draft` to `open`, an Analyst (human or agent) can hand it to a
Developer-agent without follow-up Q&A round-trips.

All structured fields live on the same `tasks` row (no extra entity for
the form itself). Acceptance criteria live in a separate
`acceptance_criteria` table; risks are stored as JSON on the task row.

### Work types and DoR profiles

`work_type` selects which DoR checks are required. Optional checks are
still computed and shown, but they don't block approval.

The Discovery checks (`has_outcome_hypothesis`, `has_redesign_decision`,
`has_agent_fit`) are advisory: they appear in the table and earn a
suggestion on `feature` tasks, but they never block and never move the
readiness score.

| work_type   | required DoR checks                                                                                                                  | typical class_of_service |
|-------------|---------------------------------------------------------------------------------------------------------------------------------------|--------------------------|
| `feature`   | user_story, problem_statement, business_value, scope_in, AC, validation, size, wip_tag                                                | standard                 |
| `bug`       | problem_statement, business_value, scope_in, AC, validation, size, wip_tag                                                            | standard / expedite      |
| `refactor`  | problem_statement, scope_in, AC, validation, size, wip_tag (`tech_debt`)                                                              | standard                 |
| `chore`     | scope_in, validation, size                                                                                                            | standard                 |
| `docs`      | scope_in, size                                                                                                                        | standard                 |
| `spike`     | problem_statement, AC, size (AC = "we have a documented answer to <Q>")                                                               | standard                 |
| `incident`  | problem_statement, AC, validation                                                                                                     | expedite                 |

Source of truth: `hub/services/dor.py` (`DOR_REQUIRED_BY_WORK_TYPE`).
Unknown work types fall back to the strict `feature` profile.

### Task fields

Set on `POST /api/tasks` (creation) or via `POST /api/tasks/{id}/refine`
(PATCH semantics — only sent fields change):

- **Classification**: `work_type`, `class_of_service`
  (`standard | expedite | fixed_date | intangible`), `size` (`XS|S|M|L|XL`),
  `wip_tag` (`feature_work | bugfix | tech_debt | support`), `due_date`.
- **Why & what**: `user_story`, `problem_statement`, `business_value`.
- **Discovery** (all optional): `outcome_metric` — which number should move
  and from what to what; `outcome_indicator` — what moves before it does;
  `outcome_deadline` — when we check; `outcome_revisit_condition` — what
  would reopen the question; `redesign_decision`
  (`adapt | redesign`) + `redesign_rationale` — whether the work fits the
  current process or reshapes it; `agent_fit`
  (`deterministic | assistant | sdd_native | agentic`) — how much agency the
  work wants. These make `business_value` checkable instead of merely
  arguable. They are suggested on `feature` tasks and never block or cost
  readiness points.
- **Scope**: `scope_in[]`, `scope_out[]`, `affected_areas[]`.
- **How we'll know it works**: `validation_commands[]`, `acceptance_criteria[]`
  (each AC has Given/When/Then + `verifiable_by` and optional `test_ref`).
- **Constraints / hints**: `constraints[]`, `assumptions[]`,
  `technical_hints`, `out_of_scope_for_review[]`.
- **Risks**: `risks[]` — each entry has `kind`, `severity`, `description`,
  `mitigation`. The `risks` payload is fed back into the readiness
  recommender.

Full Pydantic schema in `hub/models.py` (`TaskCreate`, `TaskRefine`,
`AcceptanceCriterion`, `TaskRisk`).

### Readiness score and recommendations

`GET /api/tasks/{id}/readiness` (and `oc-hub readiness <id>`) returns a
deterministic 0–100 score:

- start at 100;
- subtract `penalty_required` per missing required DoR check (default 10);
- subtract `penalty_optional` per missing optional DoR check (default 2);
- subtract per-risk penalty by severity (low=1, medium=4, high=8 by default).

The same endpoint also returns a list of `recommendations` — actionable
hints with severity (`blocking | high | medium | low`), generated by
`hub/services/recommendations.py` from the failed checks and risks. Use
`--explain` to get the score breakdown.

### Approval gate

`POST /api/tasks/{id}/approve` evaluates the DoR for the task's
`work_type`. If any required check fails, the response is **422
Unprocessable Entity** with a structured detail:

```json
{
  "detail": {
    "error": "dor_failed",
    "task_id": 17,
    "score": 62,
    "missing_required": ["has_acceptance_criteria", "has_validation_commands"],
    "recommendations": [
      {"severity": "blocking", "field": "acceptance_criteria",
       "message": "Add at least one Given/When/Then acceptance criterion."}
    ],
    "hint": "Pass force=true to override (audited)."
  }
}
```

Approval also uses an atomic `UPDATE ... WHERE status='draft'` to avoid
double-processing on concurrent calls; a losing caller gets **409
Conflict**.

`force=true` bypasses the gate but **always** logs an audit entry (an
`alert` task update + activity-log suffix) — even when DoR happens to
pass — so explicit human overrides are never invisible.

### CLI cheatsheet

```bash
# 1. Pick a template for the work type and write it to a file
oc-hub template list
oc-hub template feature --out task-17.yaml

# 2. Edit task-17.yaml, then push it onto an existing draft (PATCH semantics)
oc-hub refine 17 --from-file task-17.yaml

# 3. Add or replace acceptance criteria
oc-hub ac add 17 --id AC-1 --given "..." --when "..." --then "..." --by test
oc-hub ac list 17
oc-hub ac replace 17 --from-file acs.yaml         # atomic replace
oc-hub ac delete 17 AC-1

# 4. Add risks (read-modify-write)
oc-hub risk add 17 --kind security --severity high \
                    --description "auth bypass" --mitigation "add audit + 2FA"

# 5. Check readiness before approving
oc-hub readiness 17                # human summary
oc-hub readiness 17 --explain      # full JSON with score breakdown

# 6. Approve (or force-approve, audited)
oc-hub approve 17 --comment "ready"
oc-hub approve 17 --force --comment "deploy now, will fix gaps in PR"
```

`--from-file` accepts JSON or YAML (`.json`, `.yaml`, `.yml`). YAML
support requires `pyyaml`, which ships with the package.

### MCP tools

The same surface is exposed to LLM agents via FastMCP
(`hub/mcp_server.py`):

| Tool                              | Maps to                                                  |
|-----------------------------------|----------------------------------------------------------|
| `hub_refine_task`                 | `POST /api/tasks/{id}/refine`                            |
| `hub_list_acceptance_criteria`    | `GET  /api/tasks/{id}/acceptance_criteria`               |
| `hub_add_acceptance_criterion`    | `POST /api/tasks/{id}/acceptance_criteria`               |
| `hub_replace_acceptance_criteria` | `PUT  /api/tasks/{id}/acceptance_criteria`               |
| `hub_delete_acceptance_criterion` | `DELETE /api/tasks/{id}/acceptance_criteria/{ac_id}`     |
| `hub_add_risk`                    | read-modify-write via `/refine`                          |
| `hub_get_readiness`               | `GET  /api/tasks/{id}/readiness` (compact text by default; `explain=True` for full JSON) |

Tools take explicit, optional fields rather than free-form payloads —
that way the contract is visible in the MCP descriptor an Analyst-agent
sees, and only provided fields hit the API.

## Plugin System

Hub uses a plugin architecture for external integrations. Each integration implements a `typing.Protocol` and is registered at startup.

**Bundled plugins** (auto-registered when binaries exist):
- `DispatchPlugin` — task dispatch via `oc-dev-dispatch`
- `GitOpsPlugin` — git branch/PR/merge via local git + `gh` CLI
- `GitHubPlugin` — commits/PRs via `gh` CLI
- `NotesPlugin` — decisions via `n4l` CLI
- `VastPlugin` — GPU instance management via `vast-openclaw`
- `TranscriptsPlugin` — agent transcript viewer

**Without plugins**: Hub starts with noop implementations — all features work, integrations gracefully return empty data.

**Custom plugins**: implement the protocol from `hub/integrations/protocols.py` and register in `app.py` lifespan.

## Development

```bash
# Install with dev dependencies
uv pip install -e . && uv pip install pytest pytest-asyncio pytest-cov ruff

# Tests
.venv/bin/pytest tests/ -q

# Lint
.venv/bin/ruff check hub/ tests/
.venv/bin/ruff format hub/ tests/
```

## Use as Submodule

```bash
# In your project
git submodule add git@github.com:mrPDA/openclaw-hub.git hub
git submodule update --init --recursive

# Install
cd hub && uv venv && uv pip install -e .
```

## Architecture

```
hub/
├── app.py              # FastAPI app, lifespan, REST API routes
├── web.py              # HTMX/HTML web routes
├── services/           # Business logic (lifecycle, orchestration, dashboard)
├── repository.py       # SQL data access layer
├── db.py               # Schema, migrations, hierarchy helpers
├── models.py           # Pydantic models and enums
├── poller.py           # Background task sync
├── config.py           # Environment-based configuration
├── cli.py              # oc-hub CLI
├── mcp_server.py       # MCP tools for agents
├── integrations/
│   ├── protocols.py    # Plugin protocol definitions
│   ├── noop.py         # No-op (null) implementations
│   ├── registry.py     # Central plugin registry
│   ├── dispatch.py     # oc-dev-dispatch integration
│   ├── git_ops.py      # Git + GitHub operations
│   ├── github.py       # GitHub API (commits, PRs)
│   ├── notes.py        # notesforllm bridge
│   ├── vast.py         # Vast.ai management
│   └── transcripts.py  # Agent transcript reader
├── templates/          # Jinja2 templates
├── static/             # CSS
├── cli_templates/      # YAML work_type templates shipped as package data
└── tests/              # pytest test suite (304 tests)
```

## License

MIT
