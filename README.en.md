# Haiplane Hub

Task orchestration for AI-agent development, with the gates a human actually
needs: a task is not ready until its acceptance criteria are written down, and
not done until the hub can name the commit that carries it.

![Dashboard](docs/assets/dashboard.png)

> **This project is Russian-first.** The web interface, the seeded demo data and
> the full documentation are in Russian; this page is a summary for everyone
> else. Interface localisation has not been done yet — if you need an English
> UI, that is a contribution the project would welcome (see
> [CONTRIBUTING.md](CONTRIBUTING.md)).
>
> **Full documentation (Russian): [README.md](README.md).**

## Claimed is not delivered

An agent reporting "done" is a claim, not a fact. The work behind it may sit on
a branch nobody merged, in a PR that CI never turned green, or in a merge that
no release has taken to production yet. In a tracker whose last column is
called Done, all three look the same.

Haiplane Hub keeps the claim and the fact apart:

- a task cannot leave `draft` until the Definition of Ready passes — acceptance
  criteria in Given/When/Then, a way to validate them, a stated scope;
- a task cannot reach `completed` without a current review verdict, and the
  agent that implemented it cannot be the one who approves it;
- a `completed` task still answers *is the work in production?* from recorded
  facts — the merge the hub performed and the deploy CI reported. When it
  cannot tell, it says so, and never mistakes silence for a denial.

![Delivery panel](docs/assets/delivery.png)

## Try it

```bash
git clone https://github.com/agentdrover/haiplane.git
cd haiplane
docker compose up
```

The dashboard is on <http://localhost:8080> with a seeded demo project. The
compose file runs the hub without authentication for local demo use only — see
[README.md](README.md) for how to configure tokens before exposing it.

Agents connect over MCP at `/mcp`; the tool surface is `hub_*` (list tasks,
refine, claim, submit for review, report done). Setup for Cursor and other
clients: [docs/agent-mcp-operator-guide.md](docs/agent-mcp-operator-guide.md)
(in Russian).

## Status

Solo-maintained, best-effort support, PRs welcome. The hub runs its own
development: every change in this repository passed through the gates described
above.

Full documentation, configuration reference and architecture notes:
[README.md](README.md) · [docs/](docs/) · [CONTRIBUTING.md](CONTRIBUTING.md)

MIT licensed.
