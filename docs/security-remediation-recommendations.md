# Security Remediation Recommendations

This document turns the security review findings into implementation guidance
for a developer. Keep fixes narrow and update API, Web, MCP, CLI, and tests
together when behavior or contracts change.

## Priority Order

1. Prevent unauthenticated network exposure by default.
2. Escape the HTMX completion fragment to close stored XSS.
3. Add role/scope boundaries for human-only operations.
4. Cap collection payload sizes and consider request body limits.

## Finding 1: Network-Open Unauthenticated Default

**Risk:** `OPENCLAW_HUB_HOST` defaults to `0.0.0.0`, while an empty
`OPENCLAW_HUB_TOKENS` enables open mode. A default `openclaw-hub` run can expose
REST, Web, and `/mcp` to the LAN without authentication.

**Files to inspect/change:**

- `hub/config.py`
- `hub/app.py`
- `tests/test_auth.py`
- `README.md`
- `deploy/TAILSCALE.md`

**Recommended fix:**

- Change the default host to `127.0.0.1`.
- Add a startup guard that rejects non-loopback binds when auth is open, unless
an explicit unsafe override is set, for example
`OPENCLAW_HUB_ALLOW_UNAUTHENTICATED_NETWORK=1`.
- Keep Tailscale/team deployment documented, but require tokens for non-loopback
examples.

**Acceptance criteria:**

- Default `openclaw-hub` binds to localhost only.
- `OPENCLAW_HUB_HOST=0.0.0.0` with no tokens fails startup unless the explicit
unsafe override is set.
- `OPENCLAW_HUB_HOST=0.0.0.0` with `OPENCLAW_HUB_TOKENS` configured starts.
- Tests cover localhost default, guarded non-loopback open mode, and tokenized
non-loopback mode.

**Suggested validation:**

```bash
uv run pytest tests/test_auth.py -q
uv run pytest tests/test_api.py tests/test_mcp_server.py -q
```

## Finding 2: No Role Boundary Between Agents and Humans

**Risk:** Every valid token has full access. A compromised or limited agent token
can call human-only operations such as approve, reject, start, decide,
force-complete, and Vast instance management.

**Files to inspect/change:**

- `hub/auth.py`
- `hub/app.py`
- `hub/web.py`
- `hub/mcp_server.py`
- `hub/cli.py`
- `hub/config.py`
- `tests/test_auth.py`
- `tests/test_api.py`
- `tests/test_web.py`
- `tests/test_mcp_server.py`

**Recommended fix:**

- Extend token parsing to include roles/scopes, for example:
`alice:token:human`, `agent-ci:token:agent`, `admin:token:admin`.
- Store authenticated identity on request state as a small structured object
instead of only a username.
- Add dependencies/helpers such as `require_human_or_admin` and
`require_admin`.
- Restrict human/admin operations:
  - approve/reject/start
  - answer/decide/force-complete
  - Vast up/down
  - any future admin/config endpoints
- Leave agent-scoped operations available for propose/update/question/report
workflows.
- Ensure MCP and CLI can send the configured token and surface 403 errors
clearly.

**Acceptance criteria:**

- Agent role cannot approve, force-complete, decide, start, or manage Vast.
- Human/admin role can perform current human workflows.
- Existing open-mode tests remain explicit and do not silently imply production
safety.
- REST, Web, MCP, and CLI behavior stays aligned.

**Suggested validation:**

```bash
uv run pytest tests/test_auth.py -q
uv run pytest tests/test_api.py tests/test_web.py tests/test_mcp_server.py tests/test_cli.py -q
```

## Finding 3: HTMX Fragment Stored XSS

**Risk:** `_htmx_task_done_fragment` builds HTML with an f-string containing
persisted `t.title`. A task title with HTML or event handlers can execute when
the fragment is returned after an HTMX action.

**Files to inspect/change:**

- `hub/web.py`
- `tests/test_web.py`

**Recommended fix:**

- Prefer rendering a Jinja partial for this fragment so Jinja autoescaping
applies.
- If keeping the f-string, escape all interpolated text with `html.escape()`.
- Escape status text too, even if it currently comes from an enum, to keep the
fragment safe if fields change later.

**Acceptance criteria:**

- A title such as `<img src=x onerror=alert(1)>` is returned escaped in the HTMX
fragment.
- Existing HTMX approve/reject/start/answer/decide/force-complete flows still
render the done indicator.

**Suggested validation:**

```bash
uv run pytest tests/test_web.py -q
```

## Finding 4: Unbounded Collection Payloads

**Risk:** `PUT /api/tasks/{id}/acceptance_criteria` accepts an unbounded
top-level list. `TaskRefine.risks` and `TaskRefine.acceptance_criteria` are also
unbounded. Field lengths are capped, but item counts can still grow the DB and
slow responses/renders.

**Files to inspect/change:**

- `hub/models.py`
- `hub/app.py`
- `hub/services/refinement.py`
- `tests/test_models.py`
- `tests/test_api_refine.py`

**Recommended fix:**

- Define explicit limits near the model layer, for example:
  - max acceptance criteria per task: 50
  - max risks per task: 50
- Apply limits to:
  - `TaskCreate.acceptance_criteria` if added later
  - `TaskRefine.acceptance_criteria`
  - `TaskRefine.risks`
  - direct `PUT /acceptance_criteria`
- Consider a FastAPI/ASGI request body size limit as a broader hardening item.

**Acceptance criteria:**

- Oversized AC replacement returns 422.
- Oversized `TaskRefine.acceptance_criteria` returns 422.
- Oversized `TaskRefine.risks` returns 422.
- Normal payloads under the limit continue to work.

**Suggested validation:**

```bash
uv run pytest tests/test_models.py tests/test_api_refine.py -q
```

## Additional Hardening

- Add `OPENCLAW_HUB_COOKIE_SECURE=1` support and set the cookie `secure` flag
for TLS deployments.
- Prefer opaque browser session IDs or signed session cookies instead of storing
the bearer token directly in the browser cookie.
- Add `OPENCLAW_HUB_TOKEN` support to MCP and CLI HTTP clients so authenticated
deployments are usable consistently.
- Add optional dependency/security checks to CI if the project wants automated
coverage:

```bash
uv run pip-audit
uv run bandit -r hub
```

## Final Validation Before Merge

For a full security remediation branch, run:

```bash
uv run ruff check hub tests
uv run ruff format hub tests
uv run pytest -q
```

