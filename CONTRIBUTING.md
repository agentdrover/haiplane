# Contributing

Thanks for looking. This is a solo-maintained project, so the fastest way to
get a change in is to make it easy to review: small, explained, and green.

## Dev environment

Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/agentdrover/haiplane.git
cd haiplane
make setup          # uv venv + editable install + arms the pre-push hook
```

`make setup` also arms the pre-push hook that enforces the branch policy. It is
part of the install on purpose: a hook you have to remember to activate is a
hook nobody activated.

Run the hub:

```bash
haiplane-hub        # → http://127.0.0.1:8080
```

## Tests and linters

The same four commands CI runs, in the same order:

```bash
uv run ruff check hub tests
uv run ruff format --check hub tests
uv run mypy hub
uv run pytest -q
```

`make check` runs lint, format and tests together. A single test file while you
work: `uv run pytest tests/test_web.py -q`.

## What a PR goes through

The same gates the hub applies to its own tasks — this repository is orchestrated
by the product it contains:

- **Definition of Ready** — a change that comes from a hub task carries
  acceptance criteria in Given/When/Then before anyone writes code.
- **CI** — ruff, ruff format, mypy and pytest must be green. A red job blocks the
  merge; nothing is merged "with a known failure".
- **Review** — a human verdict, and for a task the hub ran, a machine review as
  well. In-scope findings are fixed in the same branch and resubmitted; anything
  out of scope becomes its own issue rather than growing the PR.
- **Tests** — behaviour changes come with a test that fails before the change.
  If a defect could reappear silently, that is exactly the test to write.

Branches are cut from `develop` and merged back into it: `task-<id>/<slug>` for
hub tasks, `fix/<slug>` or `chore/<slug>` otherwise. `main` is what releases
carry; do not target it directly.

## Commit style

One behaviour per commit, and a subject that says what changed for a user of the
code rather than which files moved:

```text
<type>: <short summary>
```

`type` is one of `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `ci`.

```text
fix: pass force flag through mcp approve tool
test: cover pending report force complete api
docs: add human agent workflow implementation plan
```

The body is where the reasoning goes — why this way, what was rejected, what the
change does not cover. Reviewers read it, and so does whoever hits the same code
in six months.

The full rules (branch lifecycle, review routing, PR conventions) live in
[docs/repository-rules.md](docs/repository-rules.md).
