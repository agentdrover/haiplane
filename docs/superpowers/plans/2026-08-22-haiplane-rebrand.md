# Haiplane Rebrand Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace public OpenClaw/Claw identifiers with Haiplane while keeping `hub` imports, `hub_*` MCP tools, REST paths, a live `OPENCLAW_*` production env, current filesystem defaults, and existing session cookies working.

**Architecture:** One module (`hub/brand.py`) owns display names and identifier constants. One helper (`hub.config.env_get`) reads `HAIPLANE_<SUFFIX>` then `OPENCLAW_<SUFFIX>`. Waves 1–3 are the first code landing on `develop`. Wave 4-code is a **later separate PR** (Task 8). Wave 4-dispatch is Task 9. Wave 4-operator is a window. Wave 5 (drop aliases) is out of the first landing.

**Tech Stack:** Python 3.11, FastAPI, FastMCP, Jinja2, pytest, existing `uv` tooling.

**Spec:** `docs/superpowers/specs/2026-08-22-haiplane-rebrand-design.md` (revision 5)

## Global Constraints

- Product title is exactly `Haiplane Hub`; product name is exactly `Haiplane`.
- Public domain is `haiplane.com`. Production origin stays `https://agenthai.ru` until Wave 4.
- Python import package stays `hub`. MCP tools stay `hub_*`. REST paths stay `/api/...`.
- Env suffix is unchanged: `OPENCLAW_HUB_URL` maps to `HAIPLANE_HUB_URL`.
- `env_get` prefers non-empty `HAIPLANE_*`, else non-empty `OPENCLAW_*`, else the **current** default (old path family). Empty counts as unset — a deliberate behaviour change for installs that export `OPENCLAW_X=""` today (spec Risks records it; the release gate checks prod env files for deliberately-empty values).
- Console scripts add `haiplane-hub`, `hp-hub`, `haiplane-hub-mcp`, `hp-git-policy` and keep the four OpenClaw aliases.
- Seeded workflow write names are `haiplane-ci.yml` and `haiplane-stale.yml`. Existing `openclaw-ci.yml` / `openclaw-stale.yml` remain hub-owned and are not duplicated.
- git-config **writes both** `haiplane.*` and `openclaw.*`. Reads try `haiplane.*` then `openclaw.*` (Python and `.githooks/pre-push`).
- Cookie **and** CSRF default change, dual-accept, and dual-delete happen in **one Wave 3 commit**. Do not change the cookie default in Tasks 1–4.
- Default state / workspace / transcripts stay `openclaw*` until Wave 4-code (Task 8). Dispatch catalog and dispatch/vast binaries stay until Task 9 (and 9 only starts after the recorded artifact). Do not change those default strings in Tasks 1–7.
- Do not edit executable lines in `deploy.sh`, `deploy/remote-deploy.sh`, or `deploy/run-local-hub.sh` in Tasks 1–7.
- Do not edit MCP tool signatures or tool docstrings (catalog budget must stay frozen). Do **update** `build_mcp_instructions()` product copy in Task 2.
- Task 1 defines `env_get` but does **not** replace existing `hub/config.py` readers. Wiring is Task 4.
- Public git home is `agentdrover/haiplane`, not `gh repo rename` on `mrPDA`. `GITHUB_OWNER` is locked to `agentdrover`. Prefer Import (or empty-create + push) first, then land Waves 1–3 on that remote. Waves 1–3 must not write `mrPDA/haiplane`. `hub/brand.py` may keep `GITHUB_OWNER = ""` until `agentdrover/haiplane` exists. The GitHub repo name is `haiplane`; the Python distribution stays `haiplane-hub`.
- Land on `develop`. Do not merge Waves 1–3 to `main` until the spec **Waves 1–3 release gate** is signed. Do not merge Task 8 until the **Wave 4 release gate** is signed.
- Validation: `uv run ruff check hub tests`, `uv run ruff format --check hub tests`, `uv run mypy hub`, `uv run pytest -q`. CI runs `uv run mypy hub` as a hard gate.
- Do not edit `docs/agent-context/mcp-catalog-budget.json`. After Waves 1–3, `uv run python scripts/mcp_catalog_budget.py` must exit 0 without `--update`.
- License is MIT, copyright “Haiplane contributors”, year 2026.
- Wave done-criteria are **tests**, not `rg`.

---

## File structure

**Create**

- `hub/brand.py` — display strings and identifier constants
- `tests/test_brand.py` — brand constants and `env_get`
- `LICENSE` — MIT
- `NOTICE` — formerly OpenClaw Hub
- `docs/haiplane-cutover.md` — Wave 4 operator checklist + release gate

**Modify (canonical behaviour)**

- `hub/config.py` — define `env_get` in Task 1 without wiring readers; wire readers in Task 4; cookie default change waits for Task 5; **path defaults stay old until Task 8**
- `hub/hub_instance.py` — `HAIPLANE_HUB_URL` via `env_get`
- `hub/cli.py` — URL/token via `env_get`; help text Haiplane
- `hub/mcp_server.py` — server name, URL/token via `env_get`
- `hub/app.py` — FastAPI title from `brand.PRODUCT_TITLE`; version from `get_app_version()`
- `hub/web.py` — inject brand into Jinja globals (Task 2); `web_login_submit` CSRF dual-read and dual-delete (Task 5)
- `hub/workflow_reference.py` — `build_mcp_instructions()` product copy (Task 2)
- `hub/version.py` — try `haiplane-hub` then `openclaw-hub` then `"0.1.0"` (Task 3, not Task 2)
- `hub/auth.py` — Bearer realm in Wave 1; session-cookie dual-accept in Task 5 (`verify_csrf` stays a value compare)
- `hub/git_policy.py` — dual read + dual write from `brand`
- `.githooks/pre-push` — dual-read git-config keys
- `hub/services/workflow_seed.py` — new write names + legacy ownership; commit identity
- `hub/services/orchestration.py` — `WORKTREE_PER_TASK` via `env_get`
- `hub/integrations/dispatch.py` — write both `HAIPLANE_*` and `OPENCLAW_*` child env keys
- `hub/integrations/cursor_cloud.py` — MCP server `name` (Task 5)
- `pyproject.toml` — package name and scripts (Task 3)
- `scripts/ci_report_to_hub.py`, `scripts/ci_report_audit_to_hub.py`, `scripts/roadmap_analyst_fill.py`
- `.github/actions/hub-ci-report/action.yml` — export both prefixes; do not change `uses:` consumers
- `hub/workflow_templates/ci.yml` — comments only in Tasks 1–7; `uses:` stays legacy until Task 8
- `.cursor/mcp.json.example`
- `deploy/local-hub.env.example` — comments that `HAIPLANE_*` is accepted; working keys stay `OPENCLAW_*`

**Modify (copy — product name only)**

- `hub/templates/*.html` including `admin/*` and `login.html`
- `README.md`, `AGENTS.md`, `docs/**` (except historical ADR task URLs and executable runbook commands), `skills/**`, `agents/**`
- `openclaw-hub.service` — Description line only
- Tests listed per task

**Do not modify in Tasks 1–7 (first landing)**

- `hub/models.py` enums and REST models (except incidental mention strings)
- `hub/db.py` schema (seed **source text** for new installs may change in Task 5; no migration that UPDATEs live rows)
- MCP tool function signatures and tool docstrings
- `deploy.sh`, `deploy/remote-deploy.sh`, `deploy/run-local-hub.sh` executable lines **in Tasks 1–7** (Task 8, separate PR)
- Default path strings in `hub/config.py` **in Tasks 1–7**
- Creating the new GitHub account/repo, pushing remotes, or deleting `mrPDA/openclaw-hub-standalone` (operator; Task 8 does not do this either)
- systemd unit file name on the server, `/opt/openclaw-hub`
- Live `projects.repo` values
- `docs/agent-context/mcp-catalog-budget.json`

---

### Task 1: Brand constants and env helper

**Files:**
- Create: `hub/brand.py`
- Create: `tests/test_brand.py`
- Modify: `hub/config.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `hub.brand` constants listed in the spec
  - `hub.config.env_get` with `@overload`: no default → `str | None`; `default: str` → `str`
  - `hub.config.env_get` is used by later tasks instead of raw `os.environ.get("OPENCLAW_...")`

- [ ] **Step 1: Write the failing tests**

```python
from hub import brand
from hub.config import env_get


def test_product_title() -> None:
    assert brand.PRODUCT_TITLE == "Haiplane Hub"
    assert brand.PACKAGE_NAME == "haiplane-hub"
    assert brand.PACKAGE_NAME_LEGACY == "openclaw-hub"
    assert brand.MCP_SERVER_NAME == "haiplane-hub"
    assert brand.PUBLIC_DOMAIN == "haiplane.com"
    assert brand.ENV_PREFIX == "HAIPLANE_"
    assert brand.ENV_PREFIX_LEGACY == "OPENCLAW_"
    assert brand.COOKIE_NAME == "haiplane_hub_session"
    assert brand.COOKIE_NAME_LEGACY == "openclaw_hub_session"
    assert brand.CSRF_COOKIE_NAME == "haiplane_csrf"
    assert brand.CSRF_COOKIE_NAME_LEGACY == "openclaw_csrf"
    assert brand.GITHUB_REPO == "haiplane"
    assert brand.GITHUB_SLUG_LEGACY == "mrPDA/openclaw-hub-standalone"
    assert brand.CI_REPORT_ACTION_LEGACY == (
        "mrPDA/openclaw-hub-standalone/.github/actions/hub-ci-report@main"
    )


def test_env_get_prefers_haiplane(monkeypatch) -> None:
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://haiplane.com")
    monkeypatch.setenv("OPENCLAW_HUB_URL", "https://agenthai.ru")
    assert env_get("HUB_URL") == "https://haiplane.com"


def test_env_get_falls_back_to_openclaw(monkeypatch) -> None:
    monkeypatch.delenv("HAIPLANE_HUB_URL", raising=False)
    monkeypatch.setenv("OPENCLAW_HUB_URL", "https://agenthai.ru")
    assert env_get("HUB_URL") == "https://agenthai.ru"


def test_env_get_treats_empty_haiplane_as_missing(monkeypatch) -> None:
    monkeypatch.setenv("HAIPLANE_HUB_URL", "")
    monkeypatch.setenv("OPENCLAW_HUB_URL", "https://agenthai.ru")
    assert env_get("HUB_URL") == "https://agenthai.ru"


def test_env_get_default(monkeypatch) -> None:
    monkeypatch.delenv("HAIPLANE_HUB_URL", raising=False)
    monkeypatch.delenv("OPENCLAW_HUB_URL", raising=False)
    assert env_get("HUB_URL", "http://127.0.0.1:8080") == "http://127.0.0.1:8080"


def test_github_slug_uses_legacy_when_owner_empty(monkeypatch) -> None:
    monkeypatch.setattr(brand, "GITHUB_OWNER", "")
    assert brand.github_slug() == brand.GITHUB_SLUG_LEGACY
    assert brand.ci_report_action() == brand.CI_REPORT_ACTION_LEGACY


def test_github_slug_uses_new_when_owner_set(monkeypatch) -> None:
    monkeypatch.setattr(brand, "GITHUB_OWNER", "agentdrover")
    assert brand.github_slug() == "agentdrover/haiplane"
    assert brand.require_github_owner() == "agentdrover"


def test_require_github_owner_raises_while_empty(monkeypatch) -> None:
    monkeypatch.setattr(brand, "GITHUB_OWNER", "")
    try:
        brand.require_github_owner()
    except ValueError:
        return
    raise AssertionError("require_github_owner must refuse an empty owner")
```

Do **not** permanently assert `brand.GITHUB_OWNER == ""`. After `agentdrover/haiplane` exists the constant becomes `"agentdrover"`; these tests must survive that.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_brand.py -q`
Expected: FAIL with `ModuleNotFoundError: hub.brand` or `env_get` missing

- [ ] **Step 3: Implement `hub/brand.py` and `env_get`**

```python
# hub/brand.py
from __future__ import annotations

PRODUCT_NAME = "Haiplane"
PRODUCT_TITLE = "Haiplane Hub"
PACKAGE_NAME = "haiplane-hub"
PACKAGE_NAME_LEGACY = "openclaw-hub"
MCP_SERVER_NAME = "haiplane-hub"
PUBLIC_DOMAIN = "haiplane.com"
FORMER_TITLE = "OpenClaw Hub"
ENV_PREFIX = "HAIPLANE_"
ENV_PREFIX_LEGACY = "OPENCLAW_"
GIT_BASE_BRANCH_KEY = "haiplane.baseBranch"
GIT_RELEASE_BRANCH_KEY = "haiplane.releaseBranch"
GIT_BASE_BRANCH_KEY_LEGACY = "openclaw.baseBranch"
GIT_RELEASE_BRANCH_KEY_LEGACY = "openclaw.releaseBranch"
COOKIE_NAME = "haiplane_hub_session"
COOKIE_NAME_LEGACY = "openclaw_hub_session"
CSRF_COOKIE_NAME = "haiplane_csrf"
CSRF_COOKIE_NAME_LEGACY = "openclaw_csrf"
SEEDED_CI = "haiplane-ci.yml"
SEEDED_STALE = "haiplane-stale.yml"
SEEDED_CI_LEGACY = "openclaw-ci.yml"
SEEDED_STALE_LEGACY = "openclaw-stale.yml"
GITHUB_OWNER = ""  # "agentdrover" once agentdrover/haiplane exists; do not default to mrPDA
GITHUB_REPO = "haiplane"
GITHUB_OWNER_LEGACY = "mrPDA"
GITHUB_REPO_LEGACY = "openclaw-hub-standalone"
GITHUB_SLUG_LEGACY = f"{GITHUB_OWNER_LEGACY}/{GITHUB_REPO_LEGACY}"
CI_REPORT_ACTION_LEGACY = (
    f"{GITHUB_SLUG_LEGACY}/.github/actions/hub-ci-report@main"
)


def github_slug() -> str:
    if not GITHUB_OWNER:
        return GITHUB_SLUG_LEGACY
    return f"{GITHUB_OWNER}/{GITHUB_REPO}"


def ci_report_action() -> str:
    if not GITHUB_OWNER:
        return CI_REPORT_ACTION_LEGACY
    return f"{github_slug()}/.github/actions/hub-ci-report@main"


def require_github_owner() -> str:
    if not GITHUB_OWNER:
        raise ValueError(
            "GITHUB_OWNER is empty; refuse to emit a GitHub slug until the "
            "new account is written into hub.brand"
        )
    return GITHUB_OWNER
```

In `hub/config.py`, add at the top (with the other imports, not inline):

```python
from typing import overload

from hub import brand


@overload
def env_get(suffix: str) -> str | None: ...
@overload
def env_get(suffix: str, default: str) -> str: ...


def env_get(suffix: str, default: str | None = None) -> str | None:
    new = os.environ.get(brand.ENV_PREFIX + suffix)
    if new:
        return new
    old = os.environ.get(brand.ENV_PREFIX_LEGACY + suffix)
    if old:
        return old
    return default
```

Callers of `github_slug()` / `ci_report_action()` / `require_github_owner()` must call those functions. Do not `from hub.brand import GITHUB_OWNER` and do not bind `SLUG = brand.github_slug()` at import — the Wave 4-code tests monkeypatch the module global at call time.

When Task 4 later wires readers, `HOME` becomes `Path(env_get("HUB_HOME", str(Path.home())) or Path.home())`. Do not pass a `Path` as the `env_get` default.

**Do not replace** the existing `os.environ.get("OPENCLAW_<SUFFIX>", ...)` readers in `hub/config.py` in this task. Leave them. Wiring `env_get` through those readers is Task 4 (Wave 3). Wave 1 must not change env behaviour.

**Do not change default path strings. Do not change the cookie default.**

Do not add a source-scrape path test with `or True`. Import-time path/CLI tests belong in Task 4, via subprocess, after the readers are wired.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_brand.py -q && uv run mypy hub`
Expected: PASS / mypy clean (helper is present; readers are not yet wired)

- [ ] **Step 5: Commit**

```bash
git add hub/brand.py hub/config.py tests/test_brand.py
git commit -m "feat: add Haiplane brand constants and env fallback"
```

---

### Task 2: Public face (Wave 1)

**Files:**
- Create: `LICENSE`, `NOTICE`
- Modify: `hub/app.py`, `hub/web.py`, `hub/mcp_server.py`, `hub/cli.py`, `hub/workflow_reference.py`, `hub/services/review_dispatch.py`
- Modify: `hub/auth.py` (Bearer realm only)
- Modify: `hub/integrations/git_ops.py` (PR footer copy only)
- Modify: `hub/templates/base.html`, `hub/templates/login.html`, and every template that hardcodes `OpenClaw` as a **product title**
- Modify: `README.md`, `AGENTS.md`, `docs/**` (except historical ADR task URLs and executable runbook commands), `skills/**`, `agents/**`
- Test: `tests/test_web.py`, `tests/test_mcp_server.py` (initialize / title assertions), `tests/test_auth.py` (`test_open_mode_dashboard_renders` currently asserts `"OpenClaw Hub" in resp.text`)
- Do **not** modify `hub/version.py` in this task
- Do **not** modify MCP tool docstrings
- Do **not** modify `deploy.sh` / `deploy/remote-deploy.sh` / `deploy/run-local-hub.sh`

**Interfaces:**
- Consumes: `brand.PRODUCT_TITLE`, `brand.MCP_SERVER_NAME`, `get_app_version`
- Produces: Jinja global `product_title` / `product_name`; FastAPI title; MCP server name

- [ ] **Step 1: Add web and MCP assertions**

In `tests/test_web.py`, add:

```python
def test_login_page_uses_haiplane_brand(client) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    assert "Haiplane" in response.text
    assert "OpenClaw" not in response.text
    assert "&#x1f980;" not in response.text
```

Use the same client fixture the file already uses. If `/login` needs a different helper, follow the nearest existing login-page test.

In the existing MCP test module, add or update:

```python
def test_mcp_initialize_server_name_is_haiplane_hub(...):
    # use the suite's existing initialize helper
    assert payload["serverInfo"]["name"] == "haiplane-hub"
    instructions = payload.get("instructions") or ""
    assert "Haiplane" in instructions
    assert "OpenClaw Hub" not in instructions
```

If the suite already asserts `openclaw-hub`, change that assert in this commit. Also add a direct unit test on `build_mcp_instructions()` if initialize payload shape is awkward.

- [ ] **Step 2: Run them to see they fail**

Run:

```bash
uv run pytest tests/test_web.py::test_login_page_uses_haiplane_brand -q
```

Expected: FAIL on `Haiplane` missing or `OpenClaw` still present

- [ ] **Step 3: Wire brand into app, MCP, templates**

`hub/app.py`:

```python
from hub import brand
from hub.version import get_app_version

app = FastAPI(title=brand.PRODUCT_TITLE, version=get_app_version(), lifespan=lifespan)
```

`hub/mcp_server.py`:

```python
from hub import brand

mcp = InstrumentedFastMCP(
    brand.MCP_SERVER_NAME,
    instructions=build_mcp_instructions(),
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
```

**Required:** update `hub/workflow_reference.py` `build_mcp_instructions()` so it says `Haiplane Hub` and may name `HAIPLANE_HUB_URL` (fallback `OPENCLAW_HUB_URL`). Same file: the `machine_review` rule that names `OPENCLAW_MACHINE_REVIEW` becomes `HAIPLANE_MACHINE_REVIEW` (fallback `OPENCLAW_MACHINE_REVIEW`). `hub/services/review_dispatch.py` reviewer prompt “хаба OpenClaw” becomes Haiplane. This is initialize `instructions`, not a per-tool docstring. Instructions **are** counted in `model_visible_chars` (`hub/mcp_catalog.py`); the operative gate is `uv run python scripts/mcp_catalog_budget.py` exiting 0 without `--update` — ceilings carry declared headroom (#829), so the swap plus naming the env fallback fits. Do not spend the remaining headroom on unrelated copy. Leave decorated MCP tool docstrings untouched. There is no “if unsure, leave it” hatch for initialize copy.

`hub/templates/base.html` sidebar version: render `get_app_version()` (or the existing Jinja version global) instead of hardcoded `v0.2`. In `tests/test_auth.py::test_open_mode_dashboard_renders`, assert `"Haiplane"` / `product_title` and not `"OpenClaw Hub"`.

`hub/web.py`: register Jinja globals once where the templates env is created:

```python
from hub import brand

templates.env.globals["product_name"] = brand.PRODUCT_NAME
templates.env.globals["product_title"] = brand.PRODUCT_TITLE
```

If templates are constructed in more than one place, set the globals in each place.

Replace hardcoded titles:

```html
<title>{% block title %}{{ product_title }}{% endblock %}</title>
```

```html
<span class="logo-main">{{ product_name }}</span>
<span class="logo-sub">Hub</span>
```

Favicon in `base.html` and `login.html`: replace the crab SVG with a 100×100 mark that is a simple right-pointing triangle (plane bar), no emoji.

`hub/auth.py`: `Bearer realm="haiplane-hub"`.

`LICENSE` (MIT, year 2026, copyright “Haiplane contributors”).

`NOTICE`:

```text
Haiplane Hub
Formerly published as OpenClaw Hub.
```

README first heading becomes `# Haiplane Hub`. One sentence under it: `Formerly OpenClaw Hub.` Say the public home is moving to `https://github.com/agentdrover/haiplane`. The live clone URL stays `mrPDA/openclaw-hub-standalone` until remotes move. Do not write `mrPDA/haiplane`.

Docs, skills, and agent role files: replace product name `OpenClaw Hub` → `Haiplane Hub` in prose. Leave `https://agenthai.ru/tasks/...` links in ADRs. Leave executable operator commands (`systemctl restart openclaw-hub`, `/opt/openclaw-hub`, `uv run openclaw-hub` on the live host). Leave `OPENCLAW_*` env names; Wave 3 documents them as fallback.

- [ ] **Step 4: Run focused tests and catalog budget**

Run:

```bash
uv run pytest tests/test_web.py::test_login_page_uses_haiplane_brand tests/test_mcp_server.py tests/test_auth.py::test_open_mode_dashboard_renders -q --tb=no
uv run python scripts/mcp_catalog_budget.py
uv run mypy hub
```

Expected: the new web test PASS. MCP suite PASS except any assertion that `serverInfo` is `openclaw-hub` — update those asserts to `haiplane-hub` in the same commit. Catalog budget exits 0 without `--update`.

- [ ] **Step 5: Commit**

```bash
git add hub/app.py hub/web.py hub/mcp_server.py hub/workflow_reference.py hub/auth.py hub/integrations/git_ops.py hub/templates LICENSE NOTICE README.md AGENTS.md docs skills agents tests/test_web.py tests/test_mcp_server.py tests/test_auth.py
git commit -m "docs: switch public face to Haiplane"
```

---

### Task 3: Package and CLI aliases (Wave 2)

**Files:**
- Modify: `pyproject.toml`
- Modify: `hub/version.py`
- Modify: `hub/cli.py` (`ArgumentParser(prog="hp-hub", description=...)`)
- Modify: `hub/git_policy.py` (`prog="hp-git-policy"` only; do not change git-config keys yet)
- Test: `tests/test_cli.py`, `tests/test_git_policy.py`, `tests/test_brand.py` (version fallback)

**Interfaces:**
- Consumes: `brand.PACKAGE_NAME`, `brand.PACKAGE_NAME_LEGACY`, `brand.PRODUCT_TITLE`
- Produces: console scripts listed in Global Constraints

- [ ] **Step 1: Update `pyproject.toml`**

```toml
[project]
name = "haiplane-hub"
description = "Web dashboard + MCP server for Haiplane agent development"
license = "MIT"

[project.scripts]
haiplane-hub = "hub.app:main"
hp-hub = "hub.cli:main"
haiplane-hub-mcp = "hub.mcp_server:main"
hp-git-policy = "hub.git_policy:main"
openclaw-hub = "hub.app:main"
oc-hub = "hub.cli:main"
openclaw-hub-mcp = "hub.mcp_server:main"
oc-git-policy = "hub.git_policy:main"
```

Homepage / URLs: `https://haiplane.com` is allowed. After `agentdrover/haiplane` exists, the GitHub URL may be `https://github.com/agentdrover/haiplane`. Do not write `mrPDA/haiplane`.

Do not publish to PyPI. Reserving `haiplane-hub` on PyPI is an operator note in `docs/haiplane-cutover.md`.

- [ ] **Step 2: Reinstall and prove both names**

Run:

```bash
uv pip install -e .
uv run hp-hub --help
uv run oc-hub --help
uv run haiplane-hub --help
```

Expected: all three exit 0. Help text says Haiplane, not OpenClaw.

- [ ] **Step 3: Point version lookup at both distribution names**

`hub/version.py` must keep the `PackageNotFoundError` fallback:

```python
from importlib.metadata import PackageNotFoundError, version

from hub import brand


def get_app_version() -> str:
    for name in (brand.PACKAGE_NAME, brand.PACKAGE_NAME_LEGACY):
        try:
            return version(name)
        except PackageNotFoundError:
            continue
    return "0.1.0"
```

Add tests: installed new name works; if new name is missing, legacy name is tried; both missing → `"0.1.0"`.

- [ ] **Step 4: Run CLI tests**

Run: `uv run pytest tests/test_cli.py tests/test_git_policy.py tests/test_brand.py -q`
Expected: PASS. Update any assertion that the prog name is `oc-hub` so it accepts `hp-hub` as the primary name (help may still mention `oc-hub` as an alias in prose).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock hub/version.py hub/cli.py hub/git_policy.py tests
git commit -m "feat: rename distribution to haiplane-hub with legacy scripts"
```

If `uv.lock` does not change, omit it. Do not change git-config key constants in this commit.

---

### Task 4: Wire remaining env readers (Wave 3a)

**Files:**
- Modify: `hub/config.py` — replace every `os.environ.get("OPENCLAW_<SUFFIX>", ...)` with `env_get("<SUFFIX>", ...)`. Keep `CURSOR_*` and `GH_BIN`. **Path default strings stay on the openclaw family. Cookie default stays `openclaw_hub_session`.**
- Modify: `hub/hub_instance.py`, `hub/cli.py`, `hub/mcp_server.py`, `hub/app.py`, `hub/services/orchestration.py`
- Modify: `hub/integrations/dispatch.py` (write both child-env prefixes)
- Modify: `hub/integrations/notes.py`, `hub/integrations/git_ops.py`, `hub/services/lifecycle.py`, `hub/services/auto_approve.py`, `hub/services/auto_verdict.py`, `hub/services/orchestration.py`, `hub/actionable_errors.py`, `hub/repository.py` — operator/agent-facing env names: `HAIPLANE_*` first, `OPENCLAW_*` fallback
- Modify: `hub/templates/agent_api.html` (telemetry-off notice names `OPENCLAW_MCP_TELEMETRY=0`), `hub/templates/task_detail.html` (self-approved badge `title` names `OPENCLAW_REVIEW_SELF_APPROVE=allow`) — same canonical-then-fallback rule
- Modify: `scripts/ci_report_to_hub.py`, `scripts/ci_report_audit_to_hub.py`, `scripts/roadmap_analyst_fill.py`
- Modify: `.github/actions/hub-ci-report/action.yml` (export both prefixes)
- Modify: `hub/workflow_templates/ci.yml` (comment that `HAIPLANE_*` secrets will work after Wave 4-code; do not change `uses:` or secret names)
- Modify: `tests/test_hub_instance.py`, `tests/test_cli.py`, `tests/test_mcp_internal_auth.py`, `tests/test_ci_report_script.py`, `tests/test_ci_audit_report_script.py`, `tests/test_diagnostics.py`, `tests/test_base_branch_from_project.py`, `tests/test_brand.py`, `tests/test_surface_check.py` (the `importlib.reload(config)` test around line 228 must delenv **both** `OPENCLAW_SDD_SURFACES` and `HAIPLANE_SDD_SURFACES` before reloading)

**Interfaces:**
- Consumes: `hub.config.env_get`
- Produces: every reader in the spec identifier inventory goes through `env_get`

- [ ] **Step 1: Add one instance test for the new prefix**

In `tests/test_hub_instance.py`:

```python
def test_instance_from_haiplane_hub_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCLAW_HUB_URL", raising=False)
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://haiplane.com/mcp")
    fields = instance_echo_fields()
    assert fields["base_url"] == "https://haiplane.com/mcp"
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/test_hub_instance.py::test_instance_from_haiplane_hub_url -q`
Expected: FAIL because `hub_instance.py` still reads only `OPENCLAW_HUB_URL`

- [ ] **Step 3: Switch every inventory reader**

`hub/hub_instance.py`:

```python
explicit = (env_get("HUB_URL") or "").strip()
```

`hub/cli.py` module-level URL/token:

```python
HUB_URL = env_get("HUB_URL", "http://127.0.0.1:8080") or "http://127.0.0.1:8080"
HUB_TOKEN = env_get("HUB_TOKEN", "") or ""
```

`hub/mcp_server.py` `_hub_url` / `_hub_token` use `env_get("HUB_URL", ...)` and `env_get("HUB_TOKEN", "")`.

`hub/services/orchestration.py` worktree flag: `env_get("WORKTREE_PER_TASK") == "1"`.

`hub/app.py` workspace healthcheck: `env_get("WORKSPACE_HEALTHCHECK") == "1"`.

`hub/integrations/dispatch.py`: when setting child env, write both:

```python
env["HAIPLANE_OPENROUTER_DEV_AGENT"] = agent
env["OPENCLAW_OPENROUTER_DEV_AGENT"] = agent
env["HAIPLANE_VAST_DEV_AGENT"] = agent
env["OPENCLAW_VAST_DEV_AGENT"] = agent
```

CI scripts: import `env_get` if the script can import `hub`; if a script must stay standalone, duplicate the two-line fallback, do not invent a third prefix. Cover `HUB_CI_TOKEN`, `HUB_CI_PYTEST`, `HUB_CI_CHECKS`, `HUB_TOKEN`, `HUB_MCP_TOKEN`.

Workflow template and composite action: document both secret names. Keep reading `secrets.OPENCLAW_HUB_URL` until Task 8 switches `.github/workflows/ci.yml` with a fallback expression. Do **not** change `uses: mrPDA/openclaw-hub-standalone/.github/actions/hub-ci-report@main` in this task.

**Do not change cookie default or path defaults in this task.**

Rewrite `tests/test_base_branch_from_project.py::test_the_only_sanctioned_fallback_is_the_configured_one` so it asserts `config.PAIR_BASE_BRANCH` / `env_get("PAIR_BASE_BRANCH", "develop")`, not a frozen `os.environ.get("OPENCLAW_PAIR_BASE_BRANCH", "develop")` source line.

Operator-facing strings in `hub/config.py`, `hub/app.py`, `hub/integrations/notes.py`, `hub/integrations/git_ops.py`, `hub/services/lifecycle.py`, `hub/services/auto_approve.py`, `hub/services/auto_verdict.py`, `hub/services/orchestration.py`, `hub/actionable_errors.py`, and `hub/repository.py` name `HAIPLANE_*` first and mention the `OPENCLAW_*` fallback. Same rule for the two template spots: `hub/templates/agent_api.html` (telemetry-off notice) and `hub/templates/task_detail.html` (self-approved badge `title`). `hub/models.py` docstrings stay until Wave 5.

- [ ] **Step 4: Import-time and legacy-only process tests**

Leave `test_instance_from_openclaw_hub_url` in place.

`hub.config` and `hub.cli` bind URL/token/path at import. Tests that claim env precedence must spawn a subprocess (no `or True`, no monkeypatch-after-import of already-bound globals):

```python
def _module_attr(module: str, attr: str, env: dict[str, str]) -> str:
    # python -c "from {module} import {attr}; print({attr})" with env,
    # PYTHONPATH=repo root, and HAIPLANE_* / OPENCLAW_* deleted unless passed
    ...


def test_config_db_default_stays_openclaw_family() -> None:
    path = _module_attr("hub.config", "HUB_DB_PATH", {})
    assert "openclaw-hub" in path
    assert "haiplane-hub" not in path


def test_cli_prefers_haiplane_url() -> None:
    url = _module_attr(
        "hub.cli",
        "HUB_URL",
        {"HAIPLANE_HUB_URL": "https://haiplane.com", "OPENCLAW_HUB_URL": "https://agenthai.ru"},
    )
    assert url.strip() == "https://haiplane.com"


def test_legacy_only_tokens_authenticate(client_factory_or_subprocess) -> None:
    # process env has OPENCLAW_HUB_TOKENS only; no HAIPLANE_HUB_TOKENS
    # a request with that token is accepted
    ...
```

Run: `uv run pytest tests/test_hub_instance.py tests/test_ci_report_script.py tests/test_ci_audit_report_script.py tests/test_mcp_internal_auth.py tests/test_base_branch_from_project.py tests/test_brand.py tests/test_cli.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hub/config.py hub/hub_instance.py hub/cli.py hub/mcp_server.py hub/app.py hub/services hub/integrations/dispatch.py hub/integrations/notes.py hub/integrations/git_ops.py hub/actionable_errors.py hub/repository.py scripts tests .github/actions/hub-ci-report/action.yml hub/workflow_templates/ci.yml
git commit -m "feat: read HAIPLANE_ env with OPENCLAW_ fallback"
```

---

### Task 5: Cookie, CSRF, git-config, workflow seed (Wave 3b)

This is **one commit**. Do not land a cookie default change without dual-accept and dual-delete.

**Files:**
- Modify: `hub/auth.py`, `hub/web.py`, `hub/config.py` (cookie default + `HUB_COOKIE_NAME_EXPLICIT`)
- Modify: `hub/git_policy.py`
- Modify: `.githooks/pre-push`
- Modify: `hub/services/workflow_seed.py`
- Modify: `hub/integrations/cursor_cloud.py`
- Modify: `hub/db.py` — `MACHINE_REVIEW_CYCLE_SKILL` **source string** product name only (no schema, no live-row UPDATE)
- Modify: `tests/test_auth.py`, `tests/test_logout_revokes_session.py`, `tests/test_git_policy.py`, `tests/test_workflow_templates_provisioned.py`
- Modify: `.cursor/mcp.json.example`, `deploy/local-hub.env.example` (comments / documented names only)

**Interfaces:**
- Consumes: `brand.COOKIE_NAME`, `brand.COOKIE_NAME_LEGACY`, `brand.CSRF_COOKIE_NAME`, `brand.CSRF_COOKIE_NAME_LEGACY`, git and seed constants
- Produces: dual cookie/CSRF accept + dual delete; dual git-config read **and write**; dual workflow ownership without dual-seed

- [ ] **Step 1: Write the failing tests**

Cookie / logout:

```python
def test_legacy_session_cookie_still_authenticates(client, ...):
    # set only openclaw_hub_session; request a protected page; 200 not login


def test_logout_deletes_both_session_cookie_names(client, ...):
    # after POST /logout, Set-Cookie expires both haiplane_hub_session
    # and openclaw_hub_session


def test_login_accepts_legacy_csrf_cookie(client, ...):
    # POST /login with only openclaw_csrf plus the matching form token
    # succeeds (web_login_submit, not a direct verify_csrf call)


def test_login_accepts_legacy_csrf_when_both_cookies_present(client, ...):
    # both haiplane_csrf and openclaw_csrf set; form token matches only
    # the legacy cookie; POST /login still succeeds (verify-both,
    # not pick-first-cookie)


def test_login_and_logout_delete_both_csrf_names(client, ...):
    # Set-Cookie expires both haiplane_csrf and openclaw_csrf
```

git-policy:

```python
def test_record_branch_policy_writes_both_key_families(tmp_path):
    # after record_branch_policy / activate, both
    # haiplane.baseBranch and openclaw.baseBranch are set
```

workflow seed: keep recognizing legacy names and add:

```python
CI_FILE = "haiplane-ci.yml"
STALE_FILE = "haiplane-stale.yml"
CI_FILE_LEGACY = "openclaw-ci.yml"
STALE_FILE_LEGACY = "openclaw-stale.yml"
```

Add a test that a repo whose only workflows are the two legacy files returns `PRESENT` and does not grow a second pair.

- [ ] **Step 2: Run them to see the current behaviour**

Run:

```bash
uv run pytest tests/test_workflow_templates_provisioned.py tests/test_git_policy.py tests/test_auth.py tests/test_logout_revokes_session.py -q
```

Expected: FAIL on the new filenames, dual-write, and/or dual-logout

- [ ] **Step 3: Implement**

Cookie in `hub/config.py` (this is the moment the default changes):

```python
_cookie_explicit = env_get("HUB_COOKIE")
HUB_COOKIE_NAME_EXPLICIT = bool(_cookie_explicit)
HUB_COOKIE_NAME = _cookie_explicit or brand.COOKIE_NAME
```

`hub/auth.py`:

- If `HUB_COOKIE_NAME_EXPLICIT`, `_extract_cookie` uses only `config.HUB_COOKIE_NAME`.
- Else try `brand.COOKIE_NAME` then `brand.COOKIE_NAME_LEGACY`.
- `verify_csrf` stays a value compare. Do not pretend it selects cookies.

`hub/web.py` — this is the CSRF caller. Verify against **both** cookies, do not pick the first non-empty one (a browser can hold both — a login tab opened before the deploy plus a later one — and pick-first fails the older form whose token matches the legacy cookie):

```python
if not (
    verify_csrf(csrf_token, request.cookies.get(brand.CSRF_COOKIE_NAME, ""))
    or verify_csrf(
        csrf_token, request.cookies.get(brand.CSRF_COOKIE_NAME_LEGACY, "")
    )
):
    ...
```

- Login form `Set-Cookie` uses `brand.CSRF_COOKIE_NAME`.
- `Set-Cookie` session uses `config.HUB_COOKIE_NAME`.
- Logout: revoke whichever session token was found (new then legacy); `delete_cookie` both session names unless explicit override.
- Login success and logout: `delete_cookie` both CSRF names.

`workflow_seed.py`:

```python
from hub import brand

SEEDED_WORKFLOWS = {
    "ci.yml": brand.SEEDED_CI,
    "stale.yml": brand.SEEDED_STALE,
}
LEGACY_SEEDED = {brand.SEEDED_CI_LEGACY, brand.SEEDED_STALE_LEGACY}
```

When deciding “does this repo already have hub workflows?”, treat either the new names or the legacy names as hub-owned. If only legacy names exist, return `PRESENT` and write nothing. If the repo has no workflows, write the new names. Never write a second pair.

Commit identity in the same file: `user.name=Haiplane Hub`, `user.email=hub@haiplane.local`.
Commit subject: `ci: add Haiplane workflows (hub provisioning, #476)` (today: `ci: add OpenClaw workflows`).
Update `hub/workflow_templates/ci.yml` and `stale.yml` headers/`name:` so a newly seeded `haiplane-ci.yml` does not claim the hub owns `openclaw-ci.yml` or that the workflow is “OpenClaw CI”. Do not change the `uses:` slug in this task.

`git_policy.py`:

```python
from hub import brand

BASE_BRANCH_KEY = brand.GIT_BASE_BRANCH_KEY
RELEASE_BRANCH_KEY = brand.GIT_RELEASE_BRANCH_KEY
```

`record_branch_policy` writes **both** new and legacy keys for each non-empty value. `_recorded_base` reads new then old. Error text may name both keys.

`.githooks/pre-push` — do **not** use `new || old` (an empty new key would hide the legacy value):

```bash
base_branch="$(git config --get haiplane.baseBranch 2>/dev/null || true)"
if [ -z "$base_branch" ]; then
  base_branch="$(git config --get openclaw.baseBranch 2>/dev/null || true)"
fi
release_branch="$(git config --get haiplane.releaseBranch 2>/dev/null || true)"
if [ -z "$release_branch" ]; then
  release_branch="$(git config --get openclaw.releaseBranch 2>/dev/null || true)"
fi
```

`cursor_cloud.py` MCP `name`: `brand.MCP_SERVER_NAME`. This payload is built per request; it does not rename a persistent Cursor Cloud server. Still add a request-body assertion.

`hub/db.py` `MACHINE_REVIEW_CYCLE_SKILL`: change the product phrase `OpenClaw Hub` → `Haiplane Hub` in the source template. Write `HAIPLANE_MACHINE_REVIEW` (fallback `OPENCLAW_MACHINE_REVIEW`). Do **not** add a migration that UPDATEs existing `skills` rows.

`.cursor/mcp.json.example`: add a `haiplane-hub` server entry; keep the old key as a commented example or rename it. Env in the example: `HAIPLANE_HUB_URL` / `HAIPLANE_HUB_MCP_TOKEN` with a comment that `OPENCLAW_*` still works.

`deploy/local-hub.env.example`: keep `OPENCLAW_*` as the working example. Add a comment that `HAIPLANE_*` twins are accepted. **Delete** the commented `OPENCLAW_HUB_REVIEWER_TOKEN` line (code never reads it). Document `CURSOR_REVIEWER_HUB_TOKEN` if a reviewer token is shown.

`deploy/TAILSCALE.md`: replace the fake `OPENCLAW_HUB_DB_PATH` with `OPENCLAW_HUB_DB` / `HAIPLANE_HUB_DB`. This is a docs bugfix, not a path rename.

- [ ] **Step 4: Run**

```bash
uv run pytest tests/test_workflow_templates_provisioned.py tests/test_git_policy.py tests/test_auth.py tests/test_logout_revokes_session.py -q
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hub/auth.py hub/web.py hub/config.py hub/git_policy.py hub/services/workflow_seed.py hub/integrations/cursor_cloud.py hub/db.py hub/workflow_templates .githooks/pre-push tests .cursor/mcp.json.example deploy/local-hub.env.example deploy/TAILSCALE.md
git commit -m "feat: dual-read cookie, CSRF, git-config, and seeded workflows"
```

---

### Task 6: Remaining copy, cutover runbook, release gate

**Files:**
- Modify: remaining `docs/**` product-name copy; `openclaw-hub.service` Description line only
- Create: `docs/haiplane-cutover.md`
- Modify: `docs/agent-context/system-map.md`, `docs/repository-rules.md`
- Do **not** modify `deploy.sh`, `deploy/remote-deploy.sh`, `deploy/run-local-hub.sh`

**Interfaces:**
- Consumes: names from the spec
- Produces: operator checklist for Wave 4 and the release gate; no server commands executed by the implementer

- [ ] **Step 1: Write `docs/haiplane-cutover.md`**

The file must copy the spec Wave 4 split (4-code / 4a / 4b) and both release gates. The implementer of Tasks 1–7 does not execute operator commands and does not implement Task 8 in this branch.

**Waves 1–3 release gate (before merge to `main`)**

Copy the spec “Release gate (Waves 1–3)” verbatim, including the rollback note (Wave 3 session cookies are lost on a naive revert unless the revert keeps dual-accept).

**Wave 4-code** is Task 8: a later PR. This runbook must not tell the operator to improvise path-default edits, deploy-script edits, or the `.github/workflows/ci.yml` secret switch during the outage.

**4a / 4b operator checklist** — copy the spec lists. In particular 4b must require, in this order:

1. `HAIPLANE_HUB_ALLOWED_HOSTS` / `OPENCLAW_HUB_ALLOWED_HOSTS` contains both `haiplane.com` and `agenthai.ru` **before** DNS points the new name at the server.
2. Inventory every live systemd `Environment=` and log path. Update those lines only after the files they name have been moved or symlinked.
3. Stop service → move/symlink DB, workspace, transcripts → start → `/healthz`.
4. Proof that `oc-dev-dispatch` still drops a job into the catalog the hub reads (this unblocks Task 9 later, not Task 8).
5. Prepare the **new** unit name and `/opt/haiplane-hub` (symlink to the current tree is enough) so Task 8’s deploy scripts have a live target. `systemctl is-active haiplane-hub` (or the aliased unit) must succeed. Cursor MCP may already point at `https://haiplane.com/mcp`.
6. **Then** merge Task 8 to `main`. Do not merge deploy scripts that `systemctl restart haiplane-hub` against a host that still only has `openclaw-hub`.

- [ ] **Step 2: Sweep remaining OpenClaw **product** copy**

Run (aid only, not a pass/fail gate):

```bash
rg -n 'OpenClaw' --glob '!uv.lock' --glob '!.git/**' --glob '!docs/superpowers/**' --glob '!NOTICE' --glob '!docs/haiplane-cutover.md'
```

Expected: remaining hits are only (a) `FORMER_TITLE` / “formerly OpenClaw Hub”, (b) `OPENCLAW_*` fallback names, (c) `openclaw-hub` unit/path/script aliases until Wave 4, (d) historical ADR quotes. No current-product “хаба OpenClaw” / `OpenClaw Hub` in agent prompts (`hub/services/review_dispatch.py`). No `mrPDA/haiplane`.

- [ ] **Step 3: Full validation**

```bash
uv run ruff check hub tests
uv run ruff format --check hub tests
uv run mypy hub
uv run pytest -q
uv run python scripts/surface_parity.py
uv run python scripts/mcp_catalog_budget.py
```

Expected: ruff clean, mypy clean, pytest green, surface_parity exits 0, catalog budget exits 0 without `--update`. If surface_parity warns about missing CLI/MCP/Web updates, fix the miss or record why it does not apply.

- [ ] **Step 4: Commit**

```bash
git add docs openclaw-hub.service
git commit -m "docs: add Haiplane cutover runbook and finish copy sweep"
```

---

### Task 7: Stop line (first landing)

Tasks 1–6 are the first implementation branch. After Task 6, **stop**. Do **not** do these in that branch:

- Remove `oc-hub` / `OPENCLAW_*` / `openclaw-ci.yml`
- Change default filesystem paths or dispatch catalogs
- Edit executable lines in `deploy.sh` / `deploy/remote-deploy.sh` / `deploy/run-local-hub.sh`
- Switch `.github/workflows/ci.yml` secrets (Task 8)
- Set `GITHUB_OWNER` in `hub/brand.py` (Task 8)
- Change `DISPATCH_*` / `VAST_*` defaults (Task 9)
- `git filter-repo` or history rewrite
- `gh repo rename` on `mrPDA`
- Creating `{GITHUB_OWNER}/haiplane` or deleting the old repo
- `UPDATE` live `projects.repo` or skill rows
- Publish to PyPI
- `systemctl` changes on `agenthai`
- Cloudflare DNS writes
- Merge to `main` before the Waves 1–3 release gate
- Edit MCP tool docstrings or `mcp-catalog-budget.json`

Those are Wave 4-code (Task 8), Wave 4-dispatch (Task 9), Wave 4-operator, Wave 5, or out of scope.

---

### Task 8: Wave 4-code (later PR, not the first landing)

Do this on a **new branch from `develop` after Waves 1–3 have shipped**. Do not mix it into Tasks 1–6. Do not put `DISPATCH_*` / `VAST_*` defaults in this task (Task 9).

**Files:**
- Modify: `hub/brand.py` (`GITHUB_OWNER`)
- Modify: `hub/config.py` default path strings for **state / workspace / transcripts only**
- Modify: `deploy.sh`, `deploy/remote-deploy.sh`, `deploy/run-local-hub.sh`
- Modify: `.github/workflows/ci.yml` — **secret expressions only**. Keep `env:` key names `OPENCLAW_HUB_URL` / `OPENCLAW_HUB_CI_TOKEN` (the deploy-callback `run:` body reads `$OPENCLAW_HUB_URL`). Values become `${{ secrets.HAIPLANE_HUB_URL || secrets.OPENCLAW_HUB_URL }}` at report `with:` (~118), audit `env:` (~145), and deploy callback (~236).
- Modify: `hub/workflow_templates/ci.yml` — `uses: @@CI_REPORT_ACTION@@`; `with:` hub-url/token use the same `HAIPLANE_* || OPENCLAW_*` expressions
- Modify: `hub/services/workflow_seed.py` — plumb `@@CI_REPORT_ACTION@@` into `render()` / `render_all()` from `brand.ci_report_action()` **after** `require_github_owner()`
- Modify: `docs/satellite-ci-report.md`
- Modify: tests that pinned empty owner or old hub-path defaults — rewrite them; do not delete coverage
- Test: rendered template contains no `@@`

- [ ] **Step 1: Rewrite the transitional tests first**

Keep the monkeypatched empty/set owner tests. Replace any remaining “defaults stay on openclaw family” subprocess test with one that asserts the new hub-path family. Add:

```python
def test_rendered_ci_template_has_no_placeholders(tmp_path) -> None:
    # render_all / render of ci.yml after GITHUB_OWNER is set
    # assert "@@" not in text
    # assert brand.ci_report_action() in text
```

- [ ] **Step 2: Set owner, hub-path defaults, deploy scripts, CI secret expressions, seed placeholder**

`require_github_owner()` must succeed after the owner write. Do not emit `mrPDA/haiplane`.

Do **not** change `DISPATCH_JOBS_DIR`, `DISPATCH_LOGS_DIR`, `DISPATCH_BIN`, or `VAST_JOB_BIN`.

In `docs/haiplane-cutover.md` add this **order**, not the reverse: `pip uninstall -y openclaw-hub` **then** `pip install -e "$DEST"`. Uninstalling the old distribution *after* installing `haiplane-hub` can delete shared console-script names from the old package RECORD and remove the aliases the new distribution just installed. The new `pyproject.toml` re-declares both new and legacy scripts.

- [ ] **Step 3: Validate**

```bash
uv run ruff check hub tests
uv run ruff format --check hub tests
uv run mypy hub
uv run pytest -q
uv run python scripts/surface_parity.py
uv run python scripts/mcp_catalog_budget.py
```

- [ ] **Step 4: Commit on the Wave 4-code branch. Do not merge to `main` until the Wave 4 release gate in the spec is signed (including every 4b step).**

```bash
git commit -m "feat: Haiplane Wave 4 path defaults, CI secrets, and GitHub slug"
```

---

### Task 9: Wave 4-dispatch (later than Task 8)

A wave that cannot start is better than a conditional inside Task 8.

**Do not open this branch** until these artifacts are recorded in `docs/haiplane-cutover.md`:

**9a — catalog (required to change `DISPATCH_JOBS_DIR` / `DISPATCH_LOGS_DIR`):**
- `test -d` on the **future** catalog (`~/.local/state/haiplane-dev-dispatch/jobs`), via a real directory or a symlink from the old path, **and**
- a live job JSON is visible in that future path (the hub will read it after the default change).

**9b — binaries (required to change `DISPATCH_BIN` / `VAST_JOB_BIN`):**
- `command -v hp-dev-dispatch` and `command -v vast-haiplane` both succeed (install or symlink).

If 9a is true and 9b is not, this task changes **only** the two catalog dirs. Do not point defaults at executables that do not resolve. If neither artifact exists, stop — do not start the branch.

**Files (only the defaults whose artifact exists):**
- Modify: `hub/config.py` — the matching subset of `DISPATCH_JOBS_DIR`, `DISPATCH_LOGS_DIR`, `DISPATCH_BIN`, `VAST_JOB_BIN`
- Test: subprocess import asserts the new catalog path when 9a landed

- [ ] **Step 1: Attach the 9a / 9b artifacts** (paths + `ls` / `command -v` output) to the cutover runbook. If 9a is missing, stop.

- [ ] **Step 2: Change only the defaults the artifact covers. Commit on its own branch.**

```bash
git commit -m "feat: move dispatch catalog and/or binary defaults to Haiplane"
```

---

## Self-review

1. Spec coverage: Waves 1–3 are Tasks 1–6. Wave 4-code is Task 8. Wave 4-dispatch is Task 9. Wave 4-operator is the cutover runbook. CSRF is on `web_login_submit`. `env_get` is overloaded for mypy. Deploy-callback env **keys** stay `OPENCLAW_*`. Seeded `uses:` goes through `workflow_seed.render()`.
2. Placeholder scan: no TBD steps. Owner tests monkeypatch both states. `DISPATCH_*` has no “if the gate looks true” clause.
3. Type consistency: `env_get` overloads; `HOME` default is `str(Path.home())`; callers call `github_slug()` rather than importing `GITHUB_OWNER`.
4. Alignment with spec revision 5: mypy, satellite secret fallback, Task 9a/9b, `test_auth.py`, catalog-budget gate (exit 0, headroom per #829), unit/`/opt` before Task 8 merge, and uninstall-then-install match the spec.
5. Revision-5 corrections carried into tasks: CSRF verify-both in Task 5 (snippet + both-cookies test); `agent_api.html` / `task_detail.html` env-name copy and `tests/test_surface_check.py` dual-delenv in Task 4; empty-counts-as-unset recorded as a behaviour change in Global Constraints.
