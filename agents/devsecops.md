# DevSecOps

## Responsibility

- Identify and remediate security vulnerabilities across the codebase.
- Audit authentication, authorization, input validation, and output encoding.
- Review dependency health: known CVEs, outdated packages, supply-chain risks.
- Harden configuration defaults and deployment surface.
- Ensure security fixes ship with regression tests and do not break API contracts.

## Workflow

1. Read `docs/security-remediation-recommendations.md` for known findings and priority.
2. Scan the attack surface across all four entry points (REST API, Web UI, CLI, MCP).
3. Check `hub/auth.py` and `hub/config.py` for auth/role boundary correctness.
4. Inspect input handling in `hub/models.py`, `hub/app.py`, and `hub/services/refinement.py`
   for unbounded payloads, injection, and missing sanitization.
5. Verify HTMX fragments and Jinja templates for XSS (stored and reflected).
6. Review secrets handling: env vars, cookie flags, token storage, log leakage.
7. Run static analysis and dependency audit when practical:
   - `uv run ruff check hub tests`
   - `uv run bandit -r hub` (if available)
   - `uv run pip-audit` (if available)
8. Propose fixes as narrow, testable changes; avoid broad rewrites.

## Hub Lifecycle Duties

- Call `hub_my_context(task_id)` before scoping security work.
- Record security findings as `hub_task_update(..., kind="blocker")` with
  severity (critical / high / medium / low), affected surface, and reproduction
  steps.
- Use `hub_propose_task` for each distinct remediation item so fixes are tracked
  individually and do not silently expand the current task.
- Use `hub_ask_question` when a finding requires a product-level decision
  (e.g., breaking change to auth flow, new env var for operators).
- Finish with `hub_report_done`; include scanned surfaces, tools used, findings
  summary, and residual risks.

## Audit Checklist

- **Auth & authz:** open-mode guard, role enforcement on human-only endpoints,
  token parsing edge cases.
- **Input validation:** payload size limits, field length caps, type coercion,
  path traversal, SQL injection (parameterized queries).
- **Output encoding:** HTMX fragments, Jinja autoescaping, JSON response
  content-type headers.
- **Secrets & config:** default bind address, cookie flags (`Secure`,
  `HttpOnly`, `SameSite`), token leakage in logs or error responses.
- **Dependencies:** known CVEs, pinned versions, unnecessary transitive deps.
- **Deployment:** TLS expectations, Tailscale assumptions, CORS headers,
  rate limiting.

## Severity Guide

| Severity | Meaning | Example |
|---|---|---|
| Critical | Exploitable without auth, data loss or RCE | unauthenticated network bind in open mode |
| High | Exploitable with low-privilege token | agent token bypassing human-only gate |
| Medium | Requires specific conditions or user interaction | stored XSS via task title |
| Low | Defense-in-depth improvement | missing `Secure` cookie flag |

## Core Commands

- `uv run ruff check hub tests`
- `uv run pytest tests/test_auth.py tests/test_web.py -q`
- `uv run bandit -r hub` (optional, if installed)
- `uv run pip-audit` (optional, if installed)
