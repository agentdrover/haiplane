# Haiplane Hub

Task orchestration for AI-agent development, with the gates a human actually
needs: a task is not ready until its acceptance criteria are written down, and
not done until the hub can name the commit that carries it.

![Dashboard](docs/assets/dashboard.png)

*(Русская версия: [README.ru.md](README.ru.md).)*

## Claimed is not delivered

An agent reporting "done" is a claim, not a fact. The work behind it may sit on
a branch nobody merged, in a PR that CI never turned green, or in a merge that
no release has taken to production yet. Every one of those looks identical in a
tracker whose last column is called Done — and each of them has cost someone a
morning of reading CI logs to discover.

Haiplane Hub keeps the claim and the fact apart, and makes the gap visible:

- a task cannot leave `draft` until the Definition of Ready passes — acceptance
  criteria in Given/When/Then, a way to validate, a stated scope;
- a task cannot reach `completed` without a current review verdict, and the
  agent that implemented it cannot be the one who approves it;
- a task that is `completed` still answers a further question — *is the work
  in production?* — from recorded facts: the merge the hub performed and the
  deploy CI reported. When it cannot tell, it says so, and never mistakes
  silence for a denial.

![Delivery panel](docs/assets/delivery.png)

## Features

- **DoR gate with executable acceptance criteria** — Given/When/Then criteria
  that can name the test that proves them, a deterministic readiness score, and
  an approval that refuses a draft the criteria do not cover.
- **Review gate** — no `completed` without a current APPROVED verdict; verdicts
  from the implementing agent are refused by default and audited when allowed.
- **Delivery tracking down to the SHA** — merged, released, deployed, or
  unknown, each with the reason behind it.
- **MCP server for agents** — the same task surface over Model Context Protocol,
  for Cursor, Claude Code and any streamable-HTTP MCP client.
- **Web dashboard** — HTMX board, inbox, task detail, log viewer; no build step.
- **CLI** — `hp-hub` for humans and scripts.
- **Hierarchy and lifecycle** — Epic → Feature → Task → Subtask, moving through
  draft → open → running → review → completed with CI checks, questions and
  human decisions on the way.

![Task board](docs/assets/board.png)

## Quick Start

### Docker (fastest)

```bash
git clone https://github.com/agentdrover/haiplane.git
cd haiplane
docker compose up -d --build
# → http://localhost:8080
```

The container starts with demo data (project `demo`: an epic, a feature and
tasks across the whole lifecycle, one of them delivered to production) so the
dashboard is alive on first open. To start empty, set `HAIPLANE_DEMO_SEED: "0"`
in `docker-compose.yml`. The SQLite database persists in `./data` on the host.

### From source

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/agentdrover/haiplane.git
cd haiplane

# Install (also arms the pre-push hook that enforces branch policy)
make setup

# Run
haiplane-hub
# → http://localhost:8080
```

![Task card](docs/assets/task-card.png)

## Connect an agent (MCP)

The MCP server runs inside the same process as the web UI. The endpoint is
`/mcp` — a streamable HTTP transport, authenticated with a Bearer token from
`HAIPLANE_HUB_TOKENS`:

```jsonc
// ~/.cursor/mcp.json — or any streamable-HTTP MCP client
{
  "mcpServers": {
    "haiplane-hub": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:8080/mcp",
      "headers": { "Authorization": "Bearer <TOKEN>" }
    }
  }
}
```

For a local agent there is also a stdio transport — `haiplane-hub-mcp`, which
proxies to the hub's REST API using `HAIPLANE_HUB_URL` and `HAIPLANE_HUB_TOKEN`.

Full setup, a curl smoke test and the troubleshooting table (401, 406, 421,
missing session) are in
[docs/agent-mcp-operator-guide.md](docs/agent-mcp-operator-guide.md).

The tools an agent needs for daily work:

- `hub_my_context`, `hub_project_status`, `hub_list_tasks`, `hub_task_status`
- `hub_refine_task`, `hub_get_readiness`, `hub_add_acceptance_criterion`,
  `hub_replace_acceptance_criteria`, `hub_add_risk`
- `hub_claim_task`, `hub_pair_start`, `hub_task_update`, `hub_ask_question`
- `hub_submit_for_review`, `hub_submit_review`, `hub_report_done`

Human-only gates (`hub_approve_task`, `hub_reject_task`, `hub_start_task`,
`hub_decide_task`, `hub_force_complete_task`) require a human token — an agent
cannot walk itself through them, which is the point.

## Configuration

Everything is configured through environment variables under the `HAIPLANE_*`
prefix. The defaults below are the ones in `hub/config.py`; that file is the
source of truth and carries the full list.

| Variable | Default | Description |
|---|---|---|
| `HAIPLANE_HUB_HOST` | `127.0.0.1` | Bind address. Binding a non-loopback address with no tokens configured is refused at startup |
| `HAIPLANE_HUB_PORT` | `8080` | Bind port |
| `HAIPLANE_HUB_DB` | `~/.local/state/haiplane-hub/hub.db` | SQLite database path |
| `HAIPLANE_HUB_TOKENS` | `""` | `name:token[:role]`, comma-separated; roles are `human`, `agent`, `admin`. Empty means single-user open mode |
| `HAIPLANE_HUB_ALLOW_UNAUTHENTICATED_NETWORK` | `0` | `1` allows a network bind without tokens (the docker demo does this; unsafe on a shared host) |
| `HAIPLANE_DEMO_SEED` | `0` | `1` seeds the `demo` project on an empty database. Idempotent |
| `HAIPLANE_HUB_REPO` | `""` | GitHub repo (`owner/repo`) for PR and commit integration |
| `HAIPLANE_WORKSPACE_REPO` | `~/.haiplane/workspace/repo` | Path to the workspace git repo |
| `HAIPLANE_PAIR_BASE_BRANCH` | `develop` | Branch task branches are cut from and merged back into |
| `HAIPLANE_RELEASE_BRANCH` | `main` | Branch a release carries the base branch to |
| `HAIPLANE_REVIEW_SELF_APPROVE` | `forbid` | `allow` lets the implementing agent submit its own verdict (solo mode); such verdicts are marked `self_approved`, logged and badged |
| `HAIPLANE_MACHINE_REVIEW` | `warn` | `require` blocks the human verdict until a current machine-review report exists |
| `HAIPLANE_SDD_AC_LOCATOR` | `off` | `require` rejects a `verifiable_by=test` acceptance criterion without a resolvable pytest locator |
| `HAIPLANE_SDD_AC_TESTS` | `warn` | `require` blocks an APPROVED verdict while any AC test is red or absent |
| `HAIPLANE_SDD_VALIDATION` | `warn` | `require` blocks completion until the current validation run is green |
| `HAIPLANE_MAX_REVIEW_CYCLES` | `3` | Maximum automated review cycles per task |
| `HAIPLANE_STALE_MINUTES` | `30` | Minutes before a running task is flagged stale |
| `HAIPLANE_DISPATCH_BIN` | `~/.local/bin/hp-dev-dispatch` | Optional developer-agent dispatcher. Absent is normal — the hub runs its gates without one |
| `GH_BIN` | `gh` | GitHub CLI binary |

## Status / Support

Solo-maintained. Support is best-effort: issues and questions are read, answers
may take a while, and there is no SLA. PRs are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md).

MIT licensed.

## Documentation

- [Software development workflow](docs/software-development-workflow.md) — the
  human + agent delivery flow the hub implements.
- [Structured task form & readiness](docs/task-form-and-readiness.md) — DoR
  profiles per work type, the readiness score, the CLI and MCP surface.
- [MCP operator guide](docs/agent-mcp-operator-guide.md) — transports, headers,
  curl checks, troubleshooting.
- [Agent onboarding](docs/agent-onboarding.md) and
  [Cursor agent rules](docs/cursor-agent-rules.md) — what an agent needs to know
  before it touches a task.
- [Architecture and plugins](docs/architecture.md) — module map, the plugin
  protocols, using the hub as a submodule.
- [Repository rules](docs/repository-rules.md) — branches, commits, reviews.
- [Workspace safety policy](docs/workspace-safety-policy.md) — the invariants
  that keep parallel agents out of each other's checkouts.
