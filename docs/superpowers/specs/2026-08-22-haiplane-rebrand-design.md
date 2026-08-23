# Haiplane Rebrand Design

Date: 2026-08-22
Status: draft for review (revision 5 — 2026-08-23 review corrections applied)
Domain: `haiplane.com` (Cloudflare Registrar, registered 2026-08-22, expires 2027-08-22)

## Problem

The repository, package, CLI, MCP server, environment variables, git-config keys, CI templates, and production paths still use **OpenClaw / Claw**. That name is a legal risk for a public launch. A display-only rename is not enough: agents, CI, and the live server keep speaking the old name.

This is an identifier rename with a compatibility layer. It is not a rewrite of lifecycle, schema, MCP tools, or REST.

## Goals

1. Public surfaces say **Haiplane** / **Haiplane Hub**. No OpenClaw, Claw, or crab imagery.
2. New canonical identifiers use the `haiplane` / `HAIPLANE_` / `hp-` family.
3. A running production install that still has `OPENCLAW_*` env files, old git-config keys, old session cookies, and seeded `openclaw-ci.yml` files keeps working.
4. Python import path `hub`, MCP tool names `hub_*`, and REST paths `/api/...` do not change.
5. Git history is not rewritten. A short “formerly OpenClaw Hub” notice is enough.
6. `agenthai.ru` stays the production URL until a later DNS cutover. `haiplane.com` is the public brand and future canonical host.
7. Public git hosting moves to a **new GitHub account and a new repository**. This is not `gh repo rename` on `mrPDA`.

## Non-goals

- Changing task lifecycle, DoR, review gate, or SQLite schema.
- Renaming MCP tools or REST routes.
- Rewriting git history or old commit messages (history may still say OpenClaw; that is acceptable).
- Renaming the systemd unit, unix user, `/opt` paths, or default local state/workspace/transcript/dispatch directories in Waves 1–3.
- Editing executable commands in `deploy.sh`, `deploy/remote-deploy.sh`, or `deploy/run-local-hub.sh` in Waves 1–3.
- Pointing `haiplane.com` at production before TLS and the code rename are ready.
- Deleting `mrPDA/openclaw-hub-standalone` in the same window as the first push to the new repo.
- Dropping `OPENCLAW_*` / `oc-hub` aliases in the first landing.
- Mass-updating live database rows (task titles, skill bodies, `projects.repo`) in Waves 1–3.
- Publishing to PyPI in this plan.
- Trademark legal opinion. This spec is engineering only.

## Locked names

| Role | Value |
|---|---|
| Product | Haiplane |
| Product + hub | Haiplane Hub |
| Public domain | `haiplane.com` |
| Current prod URL | `https://agenthai.ru` until Wave 4 |
| Python distribution | `haiplane-hub` |
| Python distribution (legacy lookup) | `openclaw-hub` |
| Python package (imports) | `hub` (unchanged) |
| Server console script | `haiplane-hub` |
| CLI console script | `hp-hub` |
| MCP console script | `haiplane-hub-mcp` |
| Git-policy console script | `hp-git-policy` |
| Compatibility scripts | `openclaw-hub`, `oc-hub`, `openclaw-hub-mcp`, `oc-git-policy` |
| MCP `serverInfo.name` | `haiplane-hub` |
| FastAPI `title` | `Haiplane Hub` |
| FastAPI `version` | `hub.version.get_app_version()` (not a hardcoded string) |
| Env prefix | `HAIPLANE_` |
| Env fallback prefix | `OPENCLAW_` |
| git-config keys | `haiplane.baseBranch`, `haiplane.releaseBranch` |
| git-config fallback | `openclaw.baseBranch`, `openclaw.releaseBranch` |
| Session cookie default (from Wave 3) | `haiplane_hub_session` |
| Session cookie fallback | `openclaw_hub_session` |
| CSRF cookie default (from Wave 3) | `haiplane_csrf` |
| CSRF cookie fallback | `openclaw_csrf` |
| Seeded workflows | `haiplane-ci.yml`, `haiplane-stale.yml` |
| Legacy seeded workflows | `openclaw-ci.yml`, `openclaw-stale.yml` (still recognized as hub-owned) |
| **Current** default state dir | `~/.local/state/openclaw-hub` — **do not change until Wave 4** |
| **Current** default workspace dir | `~/.openclaw/workspace/repo` — **do not change until Wave 4** |
| **Current** default transcripts dir | `~/.openclaw/transcripts` — **do not change until Wave 4** |
| **Current** dispatch catalog | `~/.local/state/openclaw-dev-dispatch/{jobs,logs}` — **do not change until Wave 4** |
| **Current** dispatch binary default | `~/.local/bin/oc-dev-dispatch` — **do not change until Wave 4-code, and only after the external producer understands the new name** |
| **Current** vast binary default | `~/.local/bin/vast-openclaw` — same gate |
| Future default state dir | `~/.local/state/haiplane-hub` (Wave 4-code only) |
| Future default workspace dir | `~/.haiplane/workspace/repo` (Wave 4-code only) |
| Future default transcripts dir | `~/.haiplane/transcripts` (Wave 4-code only) |
| Future dispatch catalog | `~/.local/state/haiplane-dev-dispatch/{jobs,logs}` (Wave 4-code only) |
| Future dispatch binary | `hp-dev-dispatch` (external project; do not rename the default until that binary exists or a symlink is in place) |
| Future vast binary | `vast-haiplane` (external project; same gate) |
| Future GitHub owner | `agentdrover` (**not** `mrPDA`) |
| Future GitHub repo | `haiplane` |
| Future GitHub slug | `agentdrover/haiplane` |
| Legacy GitHub slugs | `mrPDA/openclaw-hub-standalone` (this repo), `mrPDA/openclaw-hub` (docs/clone leftovers) |
| Composite CI action | `agentdrover/haiplane/.github/actions/hub-ci-report@main` |
| Legacy composite CI action | `mrPDA/openclaw-hub-standalone/.github/actions/hub-ci-report@main` |
| Future systemd unit | `haiplane-hub` (operator cutover) |
| Future install root | `/opt/haiplane-hub` (operator cutover) |
| License | MIT, copyright year 2026, “Haiplane contributors” |

`GITHUB_OWNER` is locked: `agentdrover`. The public git home is `https://github.com/agentdrover/haiplane`. Waves 1–3 must not write `mrPDA/haiplane`. `hub/brand.py` may keep `GITHUB_OWNER = ""` until `agentdrover/haiplane` exists (import or empty-create + push). After that write, `github_slug()` is `agentdrover/haiplane`. Clone URLs on the old remote may still show `mrPDA/openclaw-hub-standalone` until remotes move.

**Import-first (preferred now that the owner is known).** GitHub → Import repository (or `git push` of existing history into an empty `agentdrover/haiplane`, no README init, no `filter-repo`). Then Waves 1–3 land on that repo’s `develop`. This does **not** move production: `agenthai.ru` still deploys from `mrPDA/openclaw-hub-standalone` until secrets, deploy key, and remotes are recreated. Keep the old repo up for satellite `uses: mrPDA/openclaw-hub-standalone/.github/actions/hub-ci-report@main`. One source of truth after import: do not keep a second live `develop` on `mrPDA`.

The GitHub repository is `haiplane`, not `haiplane-hub`. The PyPI/distribution name, CLI, MCP server id, and systemd unit stay `haiplane-hub`. Repo name matches the product and `haiplane.com`; the `-hub` suffix stays on installable artifacts so `pip install haiplane-hub` and `systemctl start haiplane-hub` stay unambiguous.

## Env mapping

Every `OPENCLAW_<SUFFIX>` has a canonical twin `HAIPLANE_<SUFFIX>`. The suffix is unchanged.

Examples:

| Canonical | Fallback |
|---|---|
| `HAIPLANE_HUB_URL` | `OPENCLAW_HUB_URL` |
| `HAIPLANE_HUB_TOKEN` | `OPENCLAW_HUB_TOKEN` |
| `HAIPLANE_HUB_TOKENS` | `OPENCLAW_HUB_TOKENS` |
| `HAIPLANE_HUB_DB` | `OPENCLAW_HUB_DB` |
| `HAIPLANE_WORKSPACE_REPO` | `OPENCLAW_WORKSPACE_REPO` |
| `HAIPLANE_HUB_CI_TOKEN` | `OPENCLAW_HUB_CI_TOKEN` |
| `HAIPLANE_N4L_BIN` | `OPENCLAW_N4L_BIN` |

Resolution rule, used everywhere (config, CLI, MCP, CI scripts):

1. If `HAIPLANE_<SUFFIX>` is set and non-empty, use it.
2. Else if `OPENCLAW_<SUFFIX>` is set and non-empty, use it.
3. Else use the **current** default (old path family until Wave 4).

If both are set, the Haiplane value wins. Do not merge lists or concatenate paths.

Empty string counts as unset. This is a deliberate behaviour change for existing installs: today `os.environ.get("OPENCLAW_X", default)` returns `""` when the variable is exported empty, and that empty string overrides the default; after Wave 3 an empty value falls through to the other prefix and then to the default. An operator who relies on `OPENCLAW_X=""` meaning "empty, not default" must switch to an explicit value before Wave 3 lands (see Risks).

`env_get` must type-check under `uv run mypy hub` (CI hard gate, `.github/workflows/ci.yml`). `os.environ.get` is overloaded so a `str` default narrows to `str`; a single `-> str | None` signature breaks every `int(env_get(...))` and `Path(env_get(...))` in `hub/config.py`. Use:

```python
from typing import overload

@overload
def env_get(suffix: str) -> str | None: ...
@overload
def env_get(suffix: str, default: str) -> str: ...
```

`HOME` today is `Path(os.environ.get("OPENCLAW_HUB_HOME", Path.home()))` — a `Path` default. Under `env_get` the default must be `str(Path.home())`.

### Identifier inventory (complete)

Every production reader of `OPENCLAW_*` must go through `env_get` in Wave 3. This list is the contract, not an example.

**Python readers (Wave 3 must switch all of these):**

| Location | Suffixes today |
|---|---|
| `hub/config.py` | `HUB_HOME`, `HUB_REPO`, `WORKSPACE_REPO`, `DISPATCH_BIN`, `N4L_BIN`, `N4L_SPACE`, `VAST_JOB_BIN`, `VAST_ENABLED`, `TRANSCRIPTS_DIR`, `HUB_DB`, `HUB_HOST`, `HUB_PORT`, `MAX_REVIEW_CYCLES`, `REVIEW_SELF_APPROVE`, `ALLOW_AGENT_PROJECTS`, `MACHINE_REVIEW`, `SDD_AC_LOCATOR`, `SDD_AC_TESTS`, `SDD_VALIDATION`, `COMMIT_SCOPE`, `SDD_SURFACES`, `SUBMIT_RULES`, `AUTO_APPROVE_MAX_CLASS`, `REVIEW_TOKEN_BUDGET`, `EMPTY_REVIEW_MIN_USAGE`, `PROVEN_EMPTY_MAX_CLASS`, `REVIEW_LITE_TOKEN_BUDGET`, `MAX_CI_FIX_CYCLES`, `REVIEW_RUNTIME`, `REVIEW_AGENT`, `ARBITER_RUNTIME`, `ARBITER_AGENT`, `ARBITER_DISPATCH_GRACE_MINUTES`, `STALE_MINUTES`, `STALE_REVIEW_MINUTES`, `UNREFINED_DRAFT_MINUTES`, `STALE_CLAIMED_MINUTES`, `STALE_NEEDS_INFO_MINUTES`, `STALE_CI_CHECK_MINUTES`, `STALE_FIX_REQUESTED_MINUTES`, `STALE_PENDING_REPORT_MINUTES`, `MISSING_JOB_GRACE_MINUTES`, `CLAIM_LEASE_MINUTES`, `DEADLINE_CI_CHECK_MINUTES`, `DEADLINE_FIX_REQUESTED_MINUTES`, `DEADLINE_PENDING_REPORT_MINUTES`, `DEADLINE_RUNNING_MINUTES`, `DEADLINE_REVIEW_MINUTES`, `SESSION_TTL_MINUTES`, `SESSION_RETENTION_DAYS`, `MESSAGE_MAX_CHARS`, `DIFF_MAX_LINES`, `DIFF_MAX_BYTES`, `MESSAGE_RATE_PER_MINUTE`, `MESSAGE_RETENTION_DAYS`, `MCP_TELEMETRY`, `MCP_TELEMETRY_RETENTION_DAYS`, `MCP_TELEMETRY_MAX_WINDOW_DAYS`, `MCP_PROFILE`, `RELEASE_BRANCH`, `PAIR_BASE_BRANCH`, `HUB_TOKENS`, `HUB_AUTH_DISABLED`, `HUB_ALLOW_UNAUTHENTICATED_NETWORK`, `HUB_ALLOWED_HOSTS`, `HUB_COOKIE`, `HUB_COOKIE_MAX_AGE`, `HUB_COOKIE_SECURE`, `HUB_BOOTSTRAP_ADMIN_TOKEN` |
| `hub/cli.py` | `HUB_URL`, `HUB_TOKEN` |
| `hub/mcp_server.py` | `HUB_URL`, `HUB_TOKEN` |
| `hub/hub_instance.py` | `HUB_URL` |
| `hub/app.py` | `WORKSPACE_HEALTHCHECK` |
| `hub/services/orchestration.py` | `WORKTREE_PER_TASK` |
| `scripts/ci_report_to_hub.py` | `HUB_URL`, `HUB_CI_TOKEN`, `HUB_CI_PYTEST`, `HUB_CI_CHECKS` |
| `scripts/ci_report_audit_to_hub.py` | `HUB_URL`, `HUB_CI_TOKEN` |
| `scripts/roadmap_analyst_fill.py` | `HUB_URL`, `HUB_TOKEN`, `HUB_MCP_TOKEN` |

`CURSOR_*` and `GH_BIN` are not in this family. Leave them.

**Outbound child env (Wave 3 writes both names; do not drop the old keys):**

| Location | Keys |
|---|---|
| `hub/integrations/dispatch.py` | `OPENCLAW_OPENROUTER_DEV_AGENT`, `OPENCLAW_VAST_DEV_AGENT` — also set `HAIPLANE_*` twins |

**Hardcoded path defaults (read via `env_get`, default string stays old until Wave 4):**

| Constant | Current default |
|---|---|
| `WORKSPACE_REPO` | `~/.openclaw/workspace/repo` |
| `TRANSCRIPTS_DIR` | `~/.openclaw/transcripts` |
| `HUB_DB` | `~/.local/state/openclaw-hub/hub.db` |
| `DISPATCH_JOBS_DIR` / `DISPATCH_LOGS_DIR` | `~/.local/state/openclaw-dev-dispatch/{jobs,logs}` (not env today; stay) |
| `DISPATCH_BIN` | `~/.local/bin/oc-dev-dispatch` |
| `VAST_JOB_BIN` | `~/.local/bin/vast-openclaw` |

**CI / deploy executable zone (Wave 4-code for command and secret *names*; Wave 3 only documents that both prefixes work):**

| Location | What stays in Waves 1–3 | Wave 4-code |
|---|---|---|
| `.github/actions/hub-ci-report/action.yml` | still exports `OPENCLAW_HUB_*` into the reporter; may also export `HAIPLANE_HUB_*` | keep exporting both |
| `.github/workflows/ci.yml` | still reads `secrets.OPENCLAW_HUB_URL` / `OPENCLAW_HUB_CI_TOKEN` at report (lines ~118–119), audit (~145–146), and deploy callback (~236–253) | switch those three sites to `secrets.HAIPLANE_*` with `secrets.OPENCLAW_*` fallback expressions; do not drop the old secrets in the same window |
| `hub/workflow_templates/ci.yml` | `uses:` stays the legacy action slug; `with:` still `secrets.OPENCLAW_*` | `uses:` becomes `{GITHUB_OWNER}/haiplane/...` via a `workflow_seed.render()` placeholder from `require_github_owner()`; the two `with:` values become `${{ secrets.HAIPLANE_HUB_URL \|\| secrets.OPENCLAW_HUB_URL }}` (and the same for `HUB_CI_TOKEN`) |
| `deploy.sh`, `deploy/remote-deploy.sh`, `deploy/run-local-hub.sh` | **do not edit executable lines** | edit in Wave 4-code, merge only after the operator window is ready |
| `openclaw-hub.service` | filename and `ExecStart` / `WorkingDirectory` / `Environment=` stay; Description may change | every `Environment=` and log path is updated in the operator window, not guessed |

**Operator-facing strings (Wave 3 names `HAIPLANE_*` first and mentions the `OPENCLAW_*` fallback):**

| Location | Today | Wave 3 |
|---|---|---|
| `hub/config.py` unauthenticated-network error | `OPENCLAW_HUB_TOKENS` / `OPENCLAW_HUB_ALLOW_UNAUTHENTICATED_NETWORK` | canonical then fallback |
| `hub/app.py` auth / bootstrap / workspace-healthcheck logs | `OPENCLAW_HUB_TOKENS`, `OPENCLAW_HUB_BOOTSTRAP_ADMIN_TOKEN`, `OPENCLAW_WORKSPACE_HEALTHCHECK` | same |
| `hub/integrations/notes.py` | `OPENCLAW_N4L_SPACE` | same |
| `hub/integrations/git_ops.py` | `OPENCLAW_WORKSPACE_REPO` | same |
| `hub/db.py` `MACHINE_REVIEW_CYCLE_SKILL` source | `OPENCLAW_MACHINE_REVIEW` | `HAIPLANE_MACHINE_REVIEW` (fallback `OPENCLAW_MACHINE_REVIEW`) |
| `hub/services/lifecycle.py` | `OPENCLAW_REVIEW_SELF_APPROVE` in agent-facing text | canonical then fallback |
| `hub/services/auto_approve.py` | `OPENCLAW_AUTO_APPROVE_MAX_CLASS` | same |
| `hub/services/auto_verdict.py` | `OPENCLAW_PROVEN_EMPTY_MAX_CLASS` | same |
| `hub/services/orchestration.py` | `OPENCLAW_COMMIT_SCOPE` | same |
| `hub/actionable_errors.py` | `OPENCLAW_REVIEW_SELF_APPROVE` | same |
| `hub/repository.py` | `OPENCLAW_REVIEW_SELF_APPROVE` comment/help | same |
| `hub/templates/agent_api.html` | telemetry-off notice names `OPENCLAW_MCP_TELEMETRY=0` | canonical then fallback |
| `hub/templates/task_detail.html` | self-approved badge `title` names `OPENCLAW_REVIEW_SELF_APPROVE=allow` | canonical then fallback |
| `hub/services/review_dispatch.py` | agent prompt “хаба OpenClaw” | Wave 1 product copy → Haiplane |
| `hub/workflow_reference.py` | `OPENCLAW_MACHINE_REVIEW` in workflow reference text | `HAIPLANE_MACHINE_REVIEW` (fallback `OPENCLAW_MACHINE_REVIEW`) — this is initialize-adjacent copy, not a tool docstring |
| `deploy/TAILSCALE.md` | documents `OPENCLAW_HUB_DB_PATH` (code never reads this) | correct to `HUB_DB` / `HAIPLANE_HUB_DB` |
| `deploy/local-hub.env.example` | comments `OPENCLAW_HUB_REVIEWER_TOKEN` (code reads `CURSOR_REVIEWER_HUB_TOKEN`) | drop the fake name; document the real Cursor key |

MCP **tool docstrings** that name `OPENCLAW_*` stay until Wave 5 (catalog budget). Python docstrings in `hub/models.py` that mention `OPENCLAW_*` are also Wave 5 copy — they are developer-facing, not rendered anywhere. The table above is the Wave 3 operator/agent-facing list.

**Seeded workflow copy (Wave 3, with the seed behaviour change):**

| Location | Today |
|---|---|
| `hub/services/workflow_seed.py` commit subject | `ci: add OpenClaw workflows` → `ci: add Haiplane workflows` |
| `hub/workflow_templates/ci.yml` / `stale.yml` headers | “owns `openclaw-ci.yml`” / `name: OpenClaw CI` → new filenames and `Haiplane CI` so a newly seeded file does not lie |

**Other identifiers (not env, but must be dual-compatible):**

| Surface | Wave | Rule |
|---|---|---|
| `hub/git_policy.py` + `.githooks/pre-push` | 3 | read new then old; **write both** keys |
| Session cookie + CSRF cookie | 3 | one commit: new default + dual-accept + logout/login delete both |
| `hub/auth.py` Bearer realm | 1 | `haiplane-hub` |
| `hub/integrations/cursor_cloud.py` MCP `name` | 3 | `haiplane-hub` (Cursor Cloud agent payload) |
| `hub/services/workflow_seed.py` `user.name` / `user.email` | 3 | `Haiplane Hub` / `hub@haiplane.local` |
| `hub/integrations/git_ops.py` PR footer | 1 | `Created automatically by Haiplane Hub` |
| Seed skill source in `hub/db.py` | 3 | update **source string** for new installs only; no `UPDATE` of live skill rows |
| `projects.repo` rows | 4a | operator `UPDATE` of legacy slugs |
| Tests that pin `os.environ.get("OPENCLAW_PAIR_BASE_BRANCH"...)` | 3 | rewrite to the helper, not a leftover literal |
| `tests/test_surface_check.py` reload test (`OPENCLAW_SDD_SURFACES` + `importlib.reload(config)`) | 3 | delenv **both** prefixes before the reload, or a developer with `HAIPLANE_SDD_SURFACES` exported gets a red test |

## Compatibility contract

Keep both names until a later soak task (Wave 5), not in the first merge.

| Surface | New | Old during soak |
|---|---|---|
| Console scripts | `haiplane-hub`, `hp-hub`, `haiplane-hub-mcp`, `hp-git-policy` | old names remain as extra `[project.scripts]` entries pointing at the same functions |
| Env | `HAIPLANE_*` | `OPENCLAW_*` fallback |
| Default filesystem paths | unchanged | current `openclaw` path family until Wave 4 |
| git-config | write **both** `haiplane.*` and `openclaw.*` | read `haiplane.*` then `openclaw.*` (Python and `.githooks/pre-push`) |
| Seeded workflows | write `haiplane-ci.yml` / `haiplane-stale.yml` on empty repos | treat existing `openclaw-*.yml` as hub-owned; do **not** add a second pair; do not rewrite a foreign workflow |
| Session cookie | if `HAIPLANE_HUB_COOKIE` / `OPENCLAW_HUB_COOKIE` is unset, default becomes `haiplane_hub_session` | accept incoming `openclaw_hub_session`; `Set-Cookie` uses the new name; logout deletes **both** |
| CSRF cookie | default `haiplane_csrf` | accept incoming `openclaw_csrf`; set the new name; login/logout delete **both** |
| Docs | Haiplane product name | one “formerly OpenClaw Hub” sentence in README and NOTICE; operator runbooks still show the commands that work on the live server |

Do not keep the crab favicon as an alias.

### Cookie and CSRF (Wave 3, one commit)

Do **not** change the cookie default in Wave 1 or in the Task 1 env-helper commit. A default change without dual-accept and dual-logout logs everyone out and can leave a live `openclaw_hub_session` on the browser after “logout”.

In one Wave 3 commit:

1. `HUB_COOKIE_NAME_EXPLICIT = bool(env_get("HUB_COOKIE"))`.
2. `HUB_COOKIE_NAME = env_get("HUB_COOKIE") or brand.COOKIE_NAME`.
3. If explicit, use only that name (operator override).
4. If not explicit: `Set-Cookie` uses `haiplane_hub_session`; `_extract_cookie` / login session lookup tries the new name then `openclaw_hub_session`; logout revokes whichever token was present and `delete_cookie`s **both** names.
5. CSRF: `CSRF_COOKIE_NAME = "haiplane_csrf"`, `CSRF_COOKIE_NAME_LEGACY = "openclaw_csrf"`. The real reader is `hub/web.py` `web_login_submit` (`request.cookies.get(...)`), not `verify_csrf()` — that function only compares token *values*. `web_login_submit` must **verify against both cookies** — `verify_csrf(token, new) or verify_csrf(token, legacy)` — not pick the first non-empty cookie: a browser can hold both cookies at once (a login tab opened before the deploy plus a later one), and pick-first would fail the older form even though its token matches the legacy cookie. `Set-Cookie` uses the new name. Login success and logout `delete_cookie` **both** CSRF names. An in-flight login form that still holds `openclaw_csrf` must succeed after deploy, including when the new cookie is also present.

### git-config (Wave 3)

`record_branch_policy` / `activate` write **both** key families. Clones that have not pulled the new `.githooks/pre-push` still read `openclaw.*`. `_recorded_base` and the hook itself read `haiplane.*` then `openclaw.*`.

Writing only the new keys would silently disable branch policy on any clone whose hook file is still the old one.

The hook must not use `git config --get haiplane.baseBranch || git config --get openclaw.baseBranch`. If the new key exists and is empty, `git config` succeeds and the legacy value is skipped. Read the new value, test `-n`, then read legacy.

### Paths (Waves 1–3)

Wave 1 may **add** `env_get` as an unused helper. It must **not** replace the `os.environ.get("OPENCLAW_...")` readers in `hub/config.py` until Wave 3. Wiring `env_get` earlier is an env-behaviour change and contradicts Wave 1.

When Wave 3 does wire it, the **default string** when neither prefix is set stays the current `openclaw` path. Changing the default would make a freshly started process after auto-deploy look for `~/.local/state/haiplane-hub/hub.db` while production data is still under `openclaw-hub`.

Dispatch catalog directories are not env-driven today. Leave them. They are a live job/log inbox shared with an **external** producer (`oc-dev-dispatch`, not in this repository). Moving only the hub's reader path makes live jobs disappear.

## Source of truth

Add `hub/brand.py` as the only module that stores display strings and identifier constants. Templates, FastAPI, MCP, CLI help, and version lookup import from it. Do not scatter `"Haiplane Hub"` literals across twenty files after Wave 1.

`hub/brand.py` exposes at least:

- `PRODUCT_NAME = "Haiplane"`
- `PRODUCT_TITLE = "Haiplane Hub"`
- `PACKAGE_NAME = "haiplane-hub"`
- `PACKAGE_NAME_LEGACY = "openclaw-hub"`
- `MCP_SERVER_NAME = "haiplane-hub"`
- `PUBLIC_DOMAIN = "haiplane.com"`
- `FORMER_TITLE = "OpenClaw Hub"`
- `ENV_PREFIX = "HAIPLANE_"`
- `ENV_PREFIX_LEGACY = "OPENCLAW_"`
- `GIT_BASE_BRANCH_KEY = "haiplane.baseBranch"`
- `GIT_RELEASE_BRANCH_KEY = "haiplane.releaseBranch"`
- `GIT_BASE_BRANCH_KEY_LEGACY = "openclaw.baseBranch"`
- `GIT_RELEASE_BRANCH_KEY_LEGACY = "openclaw.releaseBranch"`
- `COOKIE_NAME = "haiplane_hub_session"`
- `COOKIE_NAME_LEGACY = "openclaw_hub_session"`
- `CSRF_COOKIE_NAME = "haiplane_csrf"`
- `CSRF_COOKIE_NAME_LEGACY = "openclaw_csrf"`
- `SEEDED_CI = "haiplane-ci.yml"`
- `SEEDED_STALE = "haiplane-stale.yml"`
- `SEEDED_CI_LEGACY = "openclaw-ci.yml"`
- `SEEDED_STALE_LEGACY = "openclaw-stale.yml"`
- `GITHUB_OWNER` — `"agentdrover"` once `agentdrover/haiplane` exists; empty string until that write (Wave 1 may leave it empty)
- `GITHUB_REPO = "haiplane"`
- `GITHUB_OWNER_LEGACY = "mrPDA"`
- `GITHUB_REPO_LEGACY = "openclaw-hub-standalone"`
- `GITHUB_SLUG_LEGACY = "mrPDA/openclaw-hub-standalone"`
- `CI_REPORT_ACTION_LEGACY = "mrPDA/openclaw-hub-standalone/.github/actions/hub-ci-report@main"`
- `github_slug()` — legacy slug while owner is empty (Waves 1–3 read path)
- `ci_report_action()` — legacy action while owner is empty
- `require_github_owner()` — raises if empty; Wave 4 writers call this. Do not silently emit `mrPDA/haiplane`.

`hub/config.py` keeps reading the environment. Wave 1 may import `brand` only to define `env_get`; existing `OPENCLAW_*` readers stay until Wave 3. From Wave 3 it uses `hub.brand` for prefixes and cookie names. It does not own product copy. It does not adopt future path defaults until Wave 4-code.

## What does not change

- Import path `hub.*`
- MCP tools `hub_list_tasks`, `hub_start_task`, and the rest of `hub_*`
- MCP tool **signatures and docstrings** in Waves 1–3. `initialize` instructions **do** change in Wave 1. They are counted in `model_visible_chars` (`hub/mcp_catalog.py`); a docstring edit also moves `tools/list`. The operative gate is `uv run python scripts/mcp_catalog_budget.py` exiting 0 without `--update` — ceilings carry declared headroom (#829), so the product swap plus the small growth of naming `HAIPLANE_HUB_URL` with its `OPENCLAW_HUB_URL` fallback fits. Length-neutrality is not the requirement; spending the remaining headroom on unrelated copy is still forbidden.
- REST paths and JSON field names
- Task / review / DoR behaviour
- `agenthai.ru` as the live origin until Wave 4
- Historical ADRs and task URLs that already point at `https://agenthai.ru/tasks/...`
- Git commit history (copied as-is onto the new remote; no `filter-repo`)
- Default local state / workspace / transcripts / dispatch directories until Wave 4
- Dispatch catalog path `~/.local/state/openclaw-dev-dispatch/**`
- Executable lines in `deploy/**` scripts until Wave 4
- Live `projects.repo` values until Wave 4a
- Live skill row bodies until a later soak (source templates in `hub/db.py` may change for **new** installs)

## Visual identity

- Sidebar and login: `Haiplane` + `Hub`
- Page titles: `Haiplane Hub`
- Favicon: replace the crab (`U+1F980`) with a simple geometric mark. No claw, crab, or lobster. A small triangle / plane-bar is enough; do not add an image pipeline.
- No “OpenClaw” in HTML `title`, `aria-label`, or visible copy except the one historical NOTICE line.

## Documentation policy

Public and operator docs (`README.md`, `AGENTS.md`, `docs/agent-*.md`, `docs/repository-rules.md`, `docs/agent-context/*`, skills, agent roles) switch the **product name** to Haiplane.

They do **not** rewrite executable operator commands that still work only under the old names (`systemctl restart openclaw-hub`, `/opt/openclaw-hub`, `uv run openclaw-hub` on the live host) until Wave 4. Those lines stay, marked as current production commands. After Wave 3 they may add “`HAIPLANE_*` is accepted; `OPENCLAW_*` still works”.

ADRs keep historical task links. They may add a one-line header that the product is now Haiplane.

Do not mass-edit old task titles in the production database.

README and clone docs may say the public home is moving to `https://github.com/agentdrover/haiplane`. The live clone URL stays `mrPDA/openclaw-hub-standalone` until remotes move. After import, new clones use `agentdrover/haiplane`.

`deploy/**` is a Wave 4 zone for anything an operator would paste into a shell. Waves 1–3 may add comments in `deploy/local-hub.env.example` that `HAIPLANE_*` is accepted; the working example values stay `OPENCLAW_*`.

## License and notice

Add `LICENSE` (MIT, 2026, “Haiplane contributors”) and `NOTICE`:

```text
Haiplane Hub
Formerly published as OpenClaw Hub.
```

No other legal claims in-repo. Wave 2 sets `license = "MIT"` on `[project]` (the file has no license field today). PyPI project URLs in `pyproject.toml` become `https://haiplane.com` and, after `agentdrover/haiplane` exists, `https://github.com/agentdrover/haiplane`. Do not invent `mrPDA/haiplane`. Reserving `haiplane-hub` on PyPI is an operator action, not a code PR. This plan does not publish.

## Waves

Work lands on `develop` first. Merge to `main` is a release and auto-deploys. Waves 1–3 are the first code landing. Wave 4-code is a **later, separate** PR on `develop` (not improvised in the operator window). Wave 4-operator is a coordinated window that must finish its host/path/secret prep **before** Wave 4-code merges to `main`. Wave 5 is a later cleanup.

Waves 1–3 stay off `main` until the **Waves 1–3 release gate** below is signed. Wave 4-code stays off `main` until the **Wave 4 release gate** is signed.

### Wave 1 — public face

`hub/brand.py`, `env_get` helper **defined but not wired** into existing readers, UI templates, FastAPI title, MCP display name **and** `build_mcp_instructions()` product copy, README, AGENTS, skills, agent docs, LICENSE, NOTICE, favicon, Bearer realm, PR footer copy. No cookie, path, or env-reader behaviour change yet. `hub/version.py` keeps looking up `openclaw-hub` until Wave 2. Decorated MCP **tool docstrings** stay untouched.

Done when **tests** say so, not a repo-wide grep:

- `GET /login` body contains `Haiplane` and does not contain `OpenClaw` or the crab entity.
- MCP `initialize` `serverInfo.name == "haiplane-hub"`.
- MCP `initialize` instructions contain `Haiplane` and do not contain `OpenClaw Hub` as the current product (`hub/workflow_reference.py` `build_mcp_instructions()`).
- FastAPI title is `Haiplane Hub`.
- `docs/agent-context/mcp-catalog-budget.json` is unchanged (`uv run python scripts/mcp_catalog_budget.py` exits 0 without `--update`).

`OPENCLAW_*` mentions in docs and operator runbooks are expected to remain. Existing `hub/config.py` readers still call `os.environ.get("OPENCLAW_...")` directly.

### Wave 2 — package and CLI

`pyproject.toml` name `haiplane-hub`; `license = "MIT"`; new scripts plus old aliases; `hub/version.py` tries `haiplane-hub`, then `openclaw-hub`, then `"0.1.0"` on `PackageNotFoundError` (keep the fallback).

Done when: `hp-hub --help` and `haiplane-hub --help` work; `oc-hub --help` and `openclaw-hub --help` still work; `get_app_version()` returns the installed version under either distribution name.

### Wave 3 — compatibility layer

`env_get` **wired** through the identifier inventory (this is the first env-behaviour change); cookie **and** CSRF default change + dual-accept + dual-delete in **one commit**, including `web_login_submit`; git-config dual-read / **dual-write**; pre-push dual-read with `-n` on the new key; workflow seed dual-recognize (no dual-seed) plus commit subject and template headers; CI reporter scripts; Cursor MCP example; `cursor_cloud` MCP name; seed-skill **source** product name **and** both env prefixes for new installs; operator-facing error strings.

Import-time constants (`hub.config.HUB_*`, `hub.cli.HUB_URL` / `HUB_TOKEN`) are captured at module import. Tests that claim to see env changes must import the module in a **subprocess** (or an isolated reload contract). Monkeypatching env after `import hub.config` and reading the already-bound constants is not a test.

Done when **tests** say:

- Subprocess with only `OPENCLAW_HUB_URL` / `OPENCLAW_HUB_TOKENS` (no `HAIPLANE_*`) boots and authenticates.
- Subprocess with only `HAIPLANE_*` works.
- Both-set prefers Haiplane (including CLI module-level URL/token).
- Incoming legacy session cookie authenticates when the new cookie is absent.
- `POST /logout` deletes both session cookie names (and both CSRF names).
- `POST /login` with only `openclaw_csrf` plus the matching form token succeeds; success deletes both CSRF names.
- `POST /login` with **both** CSRF cookies present and a form token matching only the legacy cookie succeeds (verify-both, not pick-first).
- `record_branch_policy` writes both git-config key families; `_recorded_base` / hook read new then old; empty new key falls through to legacy.
- Empty repo gets `haiplane-*.yml` whose headers and commit subject say Haiplane; repo that already has only `openclaw-*.yml` is `PRESENT` and does not grow a second pair.
- `tests/test_base_branch_from_project.py` no longer pins the raw `os.environ.get("OPENCLAW_PAIR_BASE_BRANCH"...)` literal.

Default filesystem paths are still the old family in this wave. Tests that lock those defaults must be **rewritten** in Wave 4-code, not left asserting `GITHUB_OWNER == ""` or “no `.haiplane` in config.py” forever.

### Wave 4 — two parts: code PR, then operator window

GitHub is a **new account + new repository**, not a rename of `mrPDA/openclaw-hub-standalone`.

**Wave 4-code** is a normal PR on `develop`, written against this spec, not invented during the outage window. It changes **hub/workspace/transcripts** path defaults (not dispatch/vast), deploy executables, `.github/workflows/ci.yml` *secret expressions*, seeded `uses:` + satellite secret fallbacks (via `workflow_seed.render()`), and the tests that Wave 1–3 used to pin empty owner / old paths. It does **not** merge to `main` until Wave 4-operator has already set allowlists, systemd `Environment=`, and data/symlink moves so the new defaults are not the first thing that finds an empty directory.

**Wave 4-dispatch** is a later PR after the external producer (`oc-dev-dispatch` / `vast-openclaw`) writes the new catalog or a symlink is in place. It is the only change that may move `DISPATCH_JOBS_DIR`, `DISPATCH_LOGS_DIR`, `DISPATCH_BIN`, and `VAST_JOB_BIN`. There is no “if the gate looks true” clause inside Wave 4-code.

**Wave 4-operator** is the window. The account `agentdrover` is known. Start with Import (or empty-create + push) of `agentdrover/haiplane`, then write `GITHUB_OWNER = "agentdrover"` in the Wave 4-code PR (or earlier if Waves 1–3 already run on that remote).

**4-code (separate develop PR, specified now so it is not improvised)**

1. Set `GITHUB_OWNER = "agentdrover"` in `hub/brand.py`. Tests monkeypatch empty **and** `"agentdrover"` (and any other set value). `require_github_owner()` is tested both ways.
2. Change default **state / workspace / transcripts** path strings to the Haiplane family. Do **not** change `DISPATCH_JOBS_DIR`, `DISPATCH_LOGS_DIR`, `DISPATCH_BIN`, or `VAST_JOB_BIN` here. Rewrite the transitional “defaults stay on openclaw” tests so they assert the new hub-path family.
3. Edit `deploy.sh`, `deploy/remote-deploy.sh`, `deploy/run-local-hub.sh` to the new unit and `/opt` paths. Those names must already exist on the host (symlink is enough) **before** this PR merges to `main`. Cutover pip order: `pip uninstall -y openclaw-hub` **then** `pip install -e "$DEST"`. The reverse can delete shared console scripts from the old package RECORD.
4. Switch `.github/workflows/ci.yml` **secret expressions only**. Keep the `env:` **key names** `OPENCLAW_HUB_URL` / `OPENCLAW_HUB_CI_TOKEN` because the deploy-callback `run:` body reads `$OPENCLAW_HUB_URL` (lines ~242–253). A renamed env key with an unchanged shell body makes CI green and silently skips deploy reporting. Pattern:

   ```yaml
   OPENCLAW_HUB_URL: ${{ secrets.HAIPLANE_HUB_URL || secrets.OPENCLAW_HUB_URL }}
   OPENCLAW_HUB_CI_TOKEN: ${{ secrets.HAIPLANE_HUB_CI_TOKEN || secrets.OPENCLAW_HUB_CI_TOKEN }}
   ```

   Report `with:` inputs and audit `env:` values use the same expression. The `||` operator is valid GHA (already used in this workflow).
5. Point `hub/workflow_templates/ci.yml` `uses:` at `agentdrover/haiplane/.github/actions/hub-ci-report@main` by adding a `workflow_seed.render()` placeholder (e.g. `@@CI_REPORT_ACTION@@`) fed from `require_github_owner()`. Update `docs/satellite-ci-report.md`. Change the template’s two `with:` values to the same `HAIPLANE_* || OPENCLAW_*` expressions. Add a test that a rendered template contains no `@@`.
6. Do not merge this PR to `main` until the Wave 4 release gate is signed.

**4a. New GitHub home (operator)**

1. Confirm the GitHub user `agentdrover` exists. Do not reuse `mrPDA` as the public owner.
2. Import this repository into `agentdrover/haiplane`, or create it empty (no README) and push existing history. Default branch `main`. Add `develop`. No `filter-repo`. No `gh repo rename` on `mrPDA`.
3. Push the existing history as-is: `main`, `develop`, and any live task branches still needed. No `filter-repo`. No force-push of rewritten history.
4. Recreate on the **new** repo, do not assume transfer copied them:
   - Actions secrets: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, **both** `HAIPLANE_HUB_URL` / `HAIPLANE_HUB_CI_TOKEN` **and** the existing `OPENCLAW_*` names
   - Environment `production` (required reviewers / wait timer if used today)
   - Branch protection for `main` and `develop`
   - Deploy key / machine user for the server clone (today `openclaw@agenthai` talks to `mrPDA/openclaw-hub-standalone`)
5. Point local and server remotes: `git remote set-url origin git@github.com:agentdrover/haiplane.git`.
6. Set `HAIPLANE_HUB_REPO=agentdrover/haiplane` on the hub (fallback `OPENCLAW_HUB_REPO` still works).
7. **Update live `projects.repo`** rows that still store `mrPDA/openclaw-hub-standalone` or `mrPDA/openclaw-hub` to `agentdrover/haiplane` (operator SQL / admin action; not a silent migration in Waves 1–3).
8. Retarget Cursor Cloud / other agent environments to `https://github.com/agentdrover/haiplane`.
9. Merge the already-reviewed Wave 4-code PR (template `uses:` + CI secret switch) only after remotes and new-repo secrets exist **and after every 4b step below**. 4b is a prerequisite of this merge, not a follow-up.
10. Archive `mrPDA/openclaw-hub-standalone` (and stop presenting `mrPDA/openclaw-hub` as current) **after** remotes, deploy keys, and satellite action consumers have moved. Put a one-line README on the archive: moved to `agentdrover/haiplane`. Do not delete the old repo in this window — the composite action URL still resolves there for unmigrated satellites.
11. Do **not** use `gh repo rename` on `mrPDA` as the public move. A rename keeps the old account in the slug and in every `uses:` line.

**4b. DNS, hosts, systemd Environment, data, dispatch producer**

Do these **before** Wave 4-code (new path defaults) merges to `main`.

1. Set `HAIPLANE_HUB_ALLOWED_HOSTS` (fallback `OPENCLAW_HUB_ALLOWED_HOSTS`) to include **both** `haiplane.com` and `agenthai.ru` (and any Tailscale / unit names already listed). `HostAllowlistMiddleware` returns 421 when the set is non-empty and the Host header is missing. DNS cutover without this step takes the new name down.
2. Point `haiplane.com` at the existing server (Cloudflare DNS + TLS). Keep `agenthai.ru` as a redirect or alias until clients move. Host-scoped cookies will not follow the hostname change; users may need to sign in again. That is accepted.
3. Set `HAIPLANE_HUB_URL=https://haiplane.com` on the server (or keep `OPENCLAW_HUB_URL` until the env file is rewritten).
4. Inventory every `Environment=` and log path in the live unit (`openclaw-hub.service` today sets `OPENCLAW_WORKSPACE_REPO` and logs under `~/.local/state/openclaw-hub`). Those lines override code defaults. Update them only after the files they name have been moved or symlinked. A code-default change that leaves an old `Environment=` pointing at a moved-away path is as bad as the reverse.
5. Stop the service, move or symlink DB / workspace / transcripts, start the service, confirm `/healthz`.
6. Dispatch catalog: `oc-dev-dispatch` and `vast-openclaw` are **external** binaries. Do not change `DISPATCH_*` / `VAST_JOB_BIN` defaults until (a) those projects read `HAIPLANE_*` or the new paths, or (b) a symlink keeps the old catalog and binary names working. Proof is a live job appearing in the catalog the hub still reads.
7. Prepare the new systemd unit name and `/opt/haiplane-hub` (symlink to the current tree is enough) so Wave 4-code deploy scripts have a live target. `systemctl is-active haiplane-hub` (or the aliased unit) must succeed **before** Wave 4-code merges. Full unix-user cutover can finish in the same window, with rollback to `openclaw-hub`.
8. Update Cursor MCP configs to `https://haiplane.com/mcp`.

Do not merge Wave 4-code until steps 1–8 are done. Deploy scripts in that PR will `systemctl restart haiplane-hub` and write `/opt/haiplane-hub`.

Waves 1–3 already accept both env/git/workflow/cookie names. Wave 4-code is the remaining code. The operator window is allowlist, remotes, secrets, data, and the external dispatcher — not a hotfix.

### Wave 4-dispatch — external catalog (after Wave 4-code)

A later PR. Split the gates so a symlink that preserves *old* binary names cannot unlock *new* binary defaults.

**9a — catalog.** `test -d` on `~/.local/state/haiplane-dev-dispatch/jobs` (real dir or symlink) **and** a live job JSON is visible on that **future** path. Only then change `DISPATCH_JOBS_DIR` / `DISPATCH_LOGS_DIR`.

**9b — binaries.** `command -v hp-dev-dispatch` and `command -v vast-haiplane` both succeed. Only then change `DISPATCH_BIN` / `VAST_JOB_BIN`.

If 9a is true and 9b is not, change only the catalog dirs. An external producer that merely reads `HAIPLANE_*` is not enough. A wave that cannot start is better than a conditional inside Wave 4-code.

### Wave 5 — drop aliases

Later task, after clients and satellites have moved: remove `OPENCLAW_*` fallback, `oc-hub`, old workflow filenames, old cookie accept, old git-config writes, leftover product strings in MCP tool docstrings (that pass updates the catalog budget on purpose). Not in the first implementation branch.

## Testing

Success is **pytest**, not `rg`. Grep is a sweep aid in the cutover runbook, not a Wave 1 done-criterion.

Required tests:

- Unit tests for the env helper: new only, old only, both (new wins), neither (current default), empty-new-falls-back-to-old.
- Import-time constants (`config.HUB_DB_PATH`, `cli.HUB_URL` / `HUB_TOKEN`) asserted via a **subprocess** with a controlled env. No `or True`. No monkeypatch-after-import of already-bound module globals.
- Cookie: new name set; old name still authenticates when no new cookie is present; logout deletes both names.
- CSRF: exercised through `web_login_submit` — legacy-only `openclaw_csrf` plus matching form token succeeds; both-cookies-present with a token matching only the legacy cookie succeeds; login and logout delete both CSRF names.
- git-policy: reads legacy keys; `activate` / `record_branch_policy` writes **both** key families; `_recorded_base` prefers new then old; empty new key falls through.
- `.githooks/pre-push` reads new, tests `-n`, then reads old.
- workflow_seed: empty repo gets `haiplane-*.yml` with Haiplane headers/commit subject; repo that already has `openclaw-*.yml` and no other workflows is `PRESENT`, not rewritten; repo with foreign CI is untouched. Dual-seed is a bug, not a requirement.
- Web: login/sidebar contain `Haiplane` and do not contain `OpenClaw` or `&#x1f980;`.
- MCP initialize: `serverInfo.name == "haiplane-hub"` **and** instructions contain Haiplane / do not contain `OpenClaw Hub` as the current product.
- `get_app_version()` works if either distribution name is installed; `PackageNotFoundError` still returns `"0.1.0"`.
- `brand.github_slug()` / `require_github_owner()` tested with monkeypatched empty owner **and** set owner. Do not permanently assert `GITHUB_OWNER == ""`.
- `tests/test_base_branch_from_project.py` asserts the helper / `PAIR_BASE_BRANCH` value, not a frozen `os.environ.get("OPENCLAW_...")` source line.
- Existing focused suites from `docs/agent-context/testing-playbook.md` plus `uv run ruff check hub tests`, `uv run ruff format --check hub tests`, `uv run mypy hub`, and `uv run pytest -q`.
- `uv run python scripts/surface_parity.py` before submit.
- `uv run python scripts/mcp_catalog_budget.py` exits 0 without `--update` after Waves 1–3.

## Release gate (Waves 1–3, before any merge to `main`)

`main` auto-deploys. Waves 1–3 are not a release until all of these are true:

1. Waves 1–3 are on `develop` and CI is green.
2. A prod-like **subprocess** started with **only** `OPENCLAW_*` (no `HAIPLANE_*`) boots, authenticates with only `OPENCLAW_HUB_TOKENS`, serves `/login` as Haiplane, and MCP `initialize` returns `haiplane-hub` with Haiplane instructions.
3. The same process with only `HAIPLANE_*` works.
4. An existing `openclaw_hub_session` cookie still signs in; logout clears both cookie names.
5. `git config --get openclaw.baseBranch` still has a value after `activate` (because both keys were written).
6. Default DB / workspace / dispatch paths are still the old family (no empty new directory created as the live store).
7. Operator sign-off on this list.

## Release gate (Wave 4-code, before merge to `main`)

1. Wave 4-operator steps that mutate allowlist, systemd `Environment=`, remotes, and data/symlinks are done.
2. External dispatch producer still delivers a job into the catalog the hub reads.
3. `HUB_ALLOWED_HOSTS` contains both public hostnames.
4. New-repo secrets include `HAIPLANE_*` and leftover `OPENCLAW_*`.
5. Wave 4-code CI is green, including the rewritten owner/path tests.
6. Operator sign-off.

## Rollback

A revert of Waves 1–3 restores env and path behaviour, but **not** in-browser sessions created after Wave 3: those cookies are named `haiplane_hub_session`. Reverted code that only reads `openclaw_hub_session` logs those users out. That session loss is accepted on a Wave 1–3 revert, **or** the revert commit must keep dual-accept for one more release (a compatibility rollback build). Say which one was chosen in the merge note.

Wave 4 rollback: old systemd unit (including its `Environment=` lines), old remote, old allowlist, old cookie names still accepted until Wave 5.

## Risks

- Cookie rename without dual-accept **and** dual-logout logs everyone out or leaves a live legacy cookie. Wave 3 is one commit for session + CSRF, and the CSRF reader is `web_login_submit`.
- Changing default paths in Waves 1–3 makes auto-deploy look at an empty `haiplane-hub` state dir. Defaults stay old.
- Changing default paths in Wave 4-code before systemd `Environment=` / data moves / dispatch-producer proof does the same thing. The Wave 4 release gate exists for that.
- A non-empty `HUB_ALLOWED_HOSTS` that omits `haiplane.com` returns 421 after DNS cutover.
- Editing `deploy/**` executables before the server paths move documents commands that do not exist yet, or breaks the ones that do. Those edits live in Wave 4-code and merge after the operator window is ready.
- workflow_seed owning only the new filenames would treat `openclaw-ci.yml` as foreign or seed a second workflow. Both names stay hub-owned; never dual-seed.
- Writing only `haiplane.*` git-config keys disables the old pre-push hook. Write both.
- `main` auto-deploys. The release gate above is mandatory.
- Pointing `haiplane.com` at the server before Wave 3 ships sends MCP clients to a process that still announces `openclaw-hub`. DNS follows code.
- Satellite repos and GitHub Actions still use `secrets.OPENCLAW_HUB_URL` until Wave 4-code switches `.github/workflows/ci.yml` with a fallback expression. Creating `HAIPLANE_*` secrets without that switch leaves the hub repo itself on the old names.
- The seeded workflow hardcodes `uses: mrPDA/openclaw-hub-standalone/.github/actions/hub-ci-report@main`. After the move, new seeds must use the new slug; old satellites keep working only while the archived repo still serves that action.
- A same-account `gh repo rename` would leave `mrPDA` in the public slug and break the “new account” goal.
- GitHub Environments, deploy keys, and Actions secrets do not follow a fresh repo. They must be recreated. A missed `DEPLOY_SSH_KEY` looks like “CI is green but prod did not update”.
- Server `origin` and `HAIPLANE_HUB_REPO` pointing at the old slug after archive: fetch/PR/CI integrations fail.
- Live `projects.repo` still pointing at `mrPDA/openclaw-hub-standalone` after the archive: PR/CI integrations for that project fail. Wave 4a updates those rows.
- Seed skill rows already in production keep historical “OpenClaw Hub” wording until a later soak. Only the source template for new installs changes in Wave 3.
- `env_get` treats an empty value as unset. An install that today exports `OPENCLAW_X=""` to mean “empty, not the default” silently starts seeing the default after Wave 3. The Wave 3 release-gate subprocess checks run with realistic prod env files; any deliberately-empty variable found there must be given an explicit value before the gate is signed.

## Success

A public clone of `agentdrover/haiplane`, the `README`, login page, MCP `initialize`, and `pip`/`uv` metadata say Haiplane. The live GitHub owner is `agentdrover`, not `mrPDA`, and the live repo name is not `openclaw-hub*`. A production host that has not yet renamed its env file, systemd unit, or state directory still runs. No OpenClaw/Claw/crab on the public face except the historical NOTICE. The old GitHub repos are archived, not advertised.

## Review corrections (revision 2)

Applied after the 2026-08-22 independent review (`changes_requested`):

1. Default paths and dispatch catalogs stay on the `openclaw` family through Waves 1–3.
2. Cookie default change, dual-accept, dual-logout, and CSRF dual-accept are one Wave 3 commit. `HUB_COOKIE_NAME_EXPLICIT` is defined.
3. git-config **writes both** key families; the pre-push hook dual-reads.
4. Identifier inventory is a complete table, not five example callers. `deploy/**` executables are a Wave 4 zone.
5. Wave done-criteria are tests, not grep. Release gate is explicit before `main`.
6. `version()` tries both distribution names and keeps the `PackageNotFoundError` fallback.
7. `ci_report_action()` may fall back to the legacy slug in Waves 1–3; Wave 4 writers call `require_github_owner()`.
8. Dual-seed of workflows is forbidden, not required.
9. `projects.repo` update is a Wave 4a operator step.
10. Seed skill **source** may change for new installs; live rows are not mass-updated.
11. FastAPI `version=` uses `get_app_version()`.
12. MCP tool docstrings stay untouched in Waves 1–3 so the catalog budget does not move.

## Review corrections (revision 3)

Applied after the second independent review (`changes_requested`):

1. Wave 1 adds `env_get` but does not wire `hub/config.py` readers; wiring is Wave 3.
2. CSRF dual-accept is specified on `web_login_submit`, not only inside `verify_csrf`.
3. Wave 4 is split into a specified Wave 4-code PR and an operator window. Allowlist, systemd `Environment=`, data/symlinks, and the external dispatch producer are on the Wave 4 release gate.
4. Future dispatch/vast names are locked; defaults do not move until those external binaries (or a symlink) exist.
5. `.github/workflows/ci.yml` secret switch is a Wave 4-code step with fallback expressions.
6. `build_mcp_instructions()` product copy is a Wave 1 requirement; the “if unsure, leave it” hatch is removed.
7. Import-time env tests use a subprocess. Transitional owner/path tests monkeypatch both states and are rewritten in Wave 4-code.
8. Rollback acknowledges Wave 3 session-cookie loss on a naive revert.
9. Operator-facing strings and the two wrong documented env names (`HUB_DB_PATH`, `HUB_REVIEWER_TOKEN`) are in the inventory.
10. Pre-push treats an empty new git-config key as missing.
11. `pyproject.toml` gets `license = "MIT"` in Wave 2.

## Review corrections (revision 4)

Applied after the third independent review (`changes_requested`):

1. `env_get` uses `@overload` so a `str` default stays `str`. CI validation includes `uv run mypy hub`. `HUB_HOME` default is `str(Path.home())`.
2. Deploy-callback keeps `env:` key names `OPENCLAW_HUB_*`; only the `secrets.*` expressions change.
3. Seeded `uses:` goes through a `workflow_seed.render()` placeholder; a rendered template must contain no `@@`. Satellite `with:` values use the same `HAIPLANE_* || OPENCLAW_*` fallback.
4. `DISPATCH_*` / `VAST_*` path and binary defaults are **not** in Wave 4-code. They are Wave 4-dispatch, which cannot start until the external producer or a symlink exists.
5. Task 2 updates `tests/test_auth.py` (`"OpenClaw Hub" in resp.text`) and renders the sidebar version from `get_app_version()`.
6. Callers use `github_slug()` / `require_github_owner()`; they do not `from hub.brand import GITHUB_OWNER`.
7. Initialize instructions count toward `model_visible_chars`. The product swap must stay inside the frozen budget.
8. Agent-facing service strings (`lifecycle`, `auto_approve`, `auto_verdict`, `orchestration`, `actionable_errors`, `repository`) are in the Wave 3 operator-facing table.
9. Spec 4a step 9 merges Wave 4-code only after every 4b step.
10. Cutover notes `pip uninstall -y openclaw-hub` **then** `pip install -e`.
11. Wave 4-code merges only after the new unit/`/opt` names exist on the host.
12. Wave 4-dispatch is 9a (future catalog path + live job there) and 9b (`command -v` both future binaries).
13. `review_dispatch.py` product copy and `workflow_reference.py` `MACHINE_REVIEW` env name are in the inventory.

## Review corrections (revision 5)

Applied after the 2026-08-23 review (approve with minor findings; the reviewer re-verified the full identifier inventory against `develop` — 69/69 `hub/config.py` suffixes matched):

1. Operator-facing table gains `hub/templates/agent_api.html` (`OPENCLAW_MCP_TELEMETRY=0`) and `hub/templates/task_detail.html` (self-approved badge `title`). `hub/models.py` docstrings are recorded as Wave 5 copy.
2. `tests/test_surface_check.py` reload test is in the Wave 3 inventory: delenv both prefixes around `importlib.reload(config)`.
3. CSRF dual-accept is verify-both (`verify_csrf(token, new) or verify_csrf(token, legacy)`), not pick-first-cookie, so a browser holding both cookies can still submit the older form. A both-cookies test is required.
4. The catalog-budget requirement is stated as the operative gate (budget script exits 0; ceilings carry headroom per #829), not as length-neutrality.
5. `env_get` empty-counts-as-unset is documented as a deliberate behaviour change for `OPENCLAW_X=""` installs, with a Risks entry and a release-gate check.
