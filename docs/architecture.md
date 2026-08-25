# Architecture and plugins

Moved out of the README (#953): the front page shows what the hub does, this
page shows how it is put together. Nothing below changed with the move.

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
├── cli.py              # hp-hub CLI
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

## Plugin System

Hub uses a plugin architecture for external integrations. Each integration implements a `typing.Protocol` and is registered at startup.

**Bundled plugins** (auto-registered when binaries exist):
- `DispatchPlugin` — task dispatch via `oc-dev-dispatch`
- `GitOpsPlugin` — git branch/PR/merge via local git + `gh` CLI
- `GitHubPlugin` — commits/PRs via `gh` CLI
- `NotesPlugin` — decisions via `n4l` CLI
- `VastPlugin` — GPU instance management via `vast-haiplane`
- `TranscriptsPlugin` — agent transcript viewer

**Without plugins**: Hub starts with noop implementations — all features work, integrations gracefully return empty data.

**Custom plugins**: implement the protocol from `hub/integrations/protocols.py` and register in `app.py` lifespan.

## Use as Submodule

```bash
# In your project
git submodule add git@github.com:agentdrover/haiplane.git hub
git submodule update --init --recursive

# Install
cd hub && make setup
```
