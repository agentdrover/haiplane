# Haiplane cutover runbook

Operator runbook for taking the Haiplane rebrand from `develop` to production.
Source of truth: `docs/superpowers/specs/2026-08-22-haiplane-rebrand-design.md`.
The implementer of Tasks 1–7 does not execute anything in this file; every step
here is either an operator action or a later, separate PR.

---

## Release gate (Waves 1–3, before any merge to `main`)

`main` auto-deploys. Waves 1–3 are not a release until all of these are true:

1. Waves 1–3 are on `develop` and CI is green.
2. A prod-like **subprocess** started with **only** `OPENCLAW_*` (no `HAIPLANE_*`) boots, authenticates with only `OPENCLAW_HUB_TOKENS`, serves `/login` as Haiplane, and MCP `initialize` returns `haiplane-hub` with Haiplane instructions.
3. The same process with only `HAIPLANE_*` works.
4. An existing `openclaw_hub_session` cookie still signs in; logout clears both cookie names.
5. `git config --get openclaw.baseBranch` still has a value after `activate` (because both keys were written).
6. Default DB / workspace / dispatch paths are still the old family (no empty new directory created as the live store).
7. Operator sign-off on this list.

### Rollback note (Waves 1–3)

A revert of Waves 1–3 restores env and path behaviour, but **not** in-browser
sessions created after Wave 3: those cookies are named `haiplane_hub_session`.
Reverted code that only reads `openclaw_hub_session` logs those users out.
Either that session loss is accepted on a Wave 1–3 revert, **or** the revert
commit must keep dual-accept for one more release (a compatibility rollback
build). Say which one was chosen in the merge note.

---

## Wave 4 — structure

Wave 4 splits into four parts. Order matters: **4-code is written early but
merges to `main` last**, after 4a and 4b are done; 4-dispatch comes after
4-code.

### Wave 4-code (Task 8 of the plan — a later, separate PR on `develop`)

Wave 4-code is Task 8 in `docs/superpowers/plans/2026-08-22-haiplane-rebrand.md`.
It is a normal PR written against the spec — **not improvised during the
operator window**. This runbook must not be read as permission to hand-edit
path defaults, deploy scripts, or the `.github/workflows/ci.yml` secret switch
during the outage. The PR (see the spec, "4-code") covers:

1. Set `GITHUB_OWNER = "agentdrover"` in `hub/brand.py`. Tests monkeypatch empty **and** `"agentdrover"`. `require_github_owner()` is tested both ways.
2. Change default **state / workspace / transcripts** path strings to the Haiplane family. Do **not** change `DISPATCH_JOBS_DIR`, `DISPATCH_LOGS_DIR`, `DISPATCH_BIN`, or `VAST_JOB_BIN` here (that is Wave 4-dispatch). Rewrite the transitional "defaults stay on openclaw" tests so they assert the new hub-path family.
3. Edit `deploy.sh`, `deploy/remote-deploy.sh`, `deploy/run-local-hub.sh` to the new unit and `/opt` paths. Those names must already exist on the host (symlink is enough) **before** this PR merges to `main` (see 4b step 7).
4. Switch `.github/workflows/ci.yml` **secret expressions only**, keeping the `env:` key names `OPENCLAW_HUB_URL` / `OPENCLAW_HUB_CI_TOKEN` (the deploy-callback `run:` body reads `$OPENCLAW_HUB_URL`): `${{ secrets.HAIPLANE_HUB_URL || secrets.OPENCLAW_HUB_URL }}` and the token analog.
5. Point `hub/workflow_templates/ci.yml` `uses:` at `agentdrover/haiplane/.github/actions/hub-ci-report@main` via a `workflow_seed.render()` placeholder fed from `require_github_owner()`; same `HAIPLANE_* || OPENCLAW_*` expressions in the template's `with:` values. Update `docs/satellite-ci-report.md`.
6. Do not merge this PR to `main` until the Wave 4 release gate below is signed.

### Cutover pip order (server, during the window)

```bash
pip uninstall -y openclaw-hub
pip install -e "$DEST"
```

**In this order.** The reverse order (`install` the new distribution first,
then `uninstall` the old one) can delete shared console scripts: the old
package's RECORD still lists them, and `pip uninstall openclaw-hub` removes
files the new `haiplane-hub` install just wrote.

### Operator note: PyPI name reservation

Reserving `haiplane-hub` on PyPI is a **manual operator action, not a PR**.
This plan does not publish to PyPI; the reservation only protects the name.

---

## Wave 4a — new GitHub home (operator)

1. Confirm the GitHub user `agentdrover` exists. Do not reuse `mrPDA` as the public owner.
2. Import this repository into `agentdrover/haiplane`, or create it empty (no README) and push existing history. Default branch `main`. Add `develop`. No `filter-repo`. No `gh repo rename` on `mrPDA`.
3. Push the existing history as-is: `main`, `develop`, and any live task branches still needed. No `filter-repo`. No force-push of rewritten history.
4. Recreate on the **new** repo, do not assume transfer copied them:
   - Actions secrets: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, **both** `HAIPLANE_HUB_URL` / `HAIPLANE_HUB_CI_TOKEN` **and** the existing `OPENCLAW_*` names <!-- pragma: allowlist secret -->
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

---

## Wave 4b — DNS, hosts, systemd Environment, data, dispatch producer (operator)

Do these **before** Wave 4-code (new path defaults) merges to `main`.

1. Set `HAIPLANE_HUB_ALLOWED_HOSTS` (fallback `OPENCLAW_HUB_ALLOWED_HOSTS`) to include **both** `haiplane.com` and `agenthai.ru` (and any Tailscale / unit names already listed). `HostAllowlistMiddleware` returns 421 when the set is non-empty and the Host header is missing. DNS cutover without this step takes the new name down — the allowlist changes **before** DNS points the new name at the server.
2. Point `haiplane.com` at the existing server (Cloudflare DNS + TLS). Keep `agenthai.ru` as a redirect or alias until clients move. Host-scoped cookies will not follow the hostname change; users may need to sign in again. That is accepted.
3. Set `HAIPLANE_HUB_URL=https://haiplane.com` on the server (or keep `OPENCLAW_HUB_URL` until the env file is rewritten).
4. Inventory every `Environment=` and log path in the live unit (`openclaw-hub.service` today sets `OPENCLAW_WORKSPACE_REPO` and logs under `~/.local/state/openclaw-hub`). Those lines override code defaults. Update them only after the files they name have been moved or symlinked. A code-default change that leaves an old `Environment=` pointing at a moved-away path is as bad as the reverse.
5. Stop the service, move or symlink DB / workspace / transcripts, start the service, confirm `/healthz`.
6. Dispatch catalog: `oc-dev-dispatch` and `vast-openclaw` are **external** binaries. Do not change `DISPATCH_*` / `VAST_JOB_BIN` defaults until (a) those projects read `HAIPLANE_*` or the new paths, or (b) a symlink keeps the old catalog and binary names working. Proof is a live job appearing in the catalog the hub still reads. (This unblocks Wave 4-dispatch / Task 9 later, not Task 8.)
7. Prepare the **new** systemd unit name and `/opt/haiplane-hub` (symlink to the current tree is enough) so Wave 4-code deploy scripts have a live target. `systemctl is-active haiplane-hub` (or the aliased unit) must succeed **before** Wave 4-code (Task 8) merges. Full unix-user cutover can finish in the same window, with rollback to `openclaw-hub`. Do not merge deploy scripts that `systemctl restart haiplane-hub` against a host that still only has `openclaw-hub`.
8. Update Cursor MCP configs to `https://haiplane.com/mcp`.

Do not merge Wave 4-code until steps 1–8 are done. Deploy scripts in that PR
will `systemctl restart haiplane-hub` and write `/opt/haiplane-hub`.

Waves 1–3 already accept both env/git/workflow/cookie names. Wave 4-code is the
remaining code. The operator window is allowlist, remotes, secrets, data, and
the external dispatcher — not a hotfix.

---

## Wave 4-dispatch — external catalog (after Wave 4-code)

A later PR (Task 9). Split the gates so a symlink that preserves *old* binary
names cannot unlock *new* binary defaults.

**9a — catalog.** `test -d` on `~/.local/state/haiplane-dev-dispatch/jobs` (real dir or symlink) **and** a live job JSON is visible on that **future** path. Only then change `DISPATCH_JOBS_DIR` / `DISPATCH_LOGS_DIR`.

**9b — binaries.** `command -v hp-dev-dispatch` and `command -v vast-haiplane` both succeed. Only then change `DISPATCH_BIN` / `VAST_JOB_BIN`.

If 9a is true and 9b is not, change only the catalog dirs. An external producer
that merely reads `HAIPLANE_*` is not enough. A wave that cannot start is
better than a conditional inside Wave 4-code.

---

## Release gate (Wave 4-code, before merge to `main`)

1. Wave 4-operator steps that mutate allowlist, systemd `Environment=`, remotes, and data/symlinks are done.
2. External dispatch producer still delivers a job into the catalog the hub reads.
3. `HUB_ALLOWED_HOSTS` contains both public hostnames.
4. New-repo secrets include `HAIPLANE_*` and leftover `OPENCLAW_*`.
5. Wave 4-code CI is green, including the rewritten owner/path tests.
6. Operator sign-off.

### Wave 4 rollback

Old systemd unit (including its `Environment=` lines), old remote, old
allowlist, old cookie names still accepted until Wave 5.
