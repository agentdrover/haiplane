#!/usr/bin/env bash
# Remote deploy step for OpenClaw Hub, executed on the target server via `ssh ... 'bash -s'`.
#
# Mirrors docs/agent-deploy-runbook.md section 4. The CI runner first rsyncs the
# working tree into the user's staging directory; this script promotes it into the
# service source tree, reinstalls the package, restarts systemd and probes /healthz.
#
# Assumptions (already true on the current server, see docs/agent-deploy-runbook.md):
#   - staging dir:     $HOME/openclaw-hub-src-staging
#   - service source:  /opt/openclaw-hub/src
#   - service venv:    /opt/openclaw-hub/venv
#   - runtime user:    openclaw
#   - systemd unit:    openclaw-hub
#   - the deploy SSH user has passwordless sudo for the commands below.
set -euo pipefail

STAGING="$HOME/openclaw-hub-src-staging"
DEST=/opt/openclaw-hub/src
# Overridable so the failure branch can be exercised against a dead port instead
# of staging an outage on the live service to prove the gate still bites (#547).
# CI never sets it; the default is the real endpoint.
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/healthz}"

if [ ! -d "$STAGING" ]; then
  echo "staging directory $STAGING is missing; rsync step did not run" >&2
  exit 1
fi

sudo rsync -a --delete "$STAGING/" "$DEST/"
sudo chown -R openclaw:openclaw "$DEST"
sudo -u openclaw /opt/openclaw-hub/venv/bin/pip install -e "$DEST" -q
sudo systemctl restart openclaw-hub

# Readiness is polled, not slept for. The unit is Type=simple, so systemd calls
# it active the moment the process spawns — uvicorn may still be minutes away
# from accepting a connection. The old `sleep 2` + single probe made every
# release a coin flip: on 28.07 the deploy succeeded in full and the job still
# went red (run 30399012885, service=active, healthz=000).
#
# The budget is generous on purpose, so a slow start is not reported as a
# failure. That trade has a cost — a genuinely degrading start would be
# swallowed — so the elapsed time is printed on success. A number creeping
# towards the budget is visible; a silent success is not.
HEALTH_BUDGET_SECONDS="${HEALTH_BUDGET_SECONDS:-60}"
started_at="$SECONDS"
code=""
state=""

# The budget is checked AFTER probing, never before. Testing it first ends the
# loop strictly inside the budget: with a 5s budget the last probe lands at 4s,
# so a service that starts serving at 4.3s — inside the budget it was given —
# is reported as a failure. That is the false red this task exists to remove.
# `SECONDS` is integer and an iteration costs more than its sleep, so the loop
# also drifts: a measured run probed at 0s, 1s, 2s, 4s and skipped 3s entirely.
while :; do
  state="$(systemctl is-active openclaw-hub || true)"
  # `failed` is terminal — waiting out the budget would only delay the report.
  if [ "$state" = "failed" ]; then
    break
  fi
  # Every probe is bounded. curl has no default overall timeout: against a socket
  # that accepts the connection and then never answers it blocks forever —
  # measured at over 12s and still waiting. The budget below would never be
  # consulted, so the gate would hang on exactly the pathology it exists to
  # catch, and the job would sit there until the runner's own timeout.
  code="$(curl -sf --max-time 5 --connect-timeout 3 -o /dev/null -w '%{http_code}' "$HEALTH_URL" || true)"
  if [ "$code" = "200" ]; then
    break
  fi
  # Deliberately after the 200 check, not before. The budget bounds how long we
  # WAIT; it is not a deadline the service must beat. A 200 we actually observed
  # means the service is serving — failing the deploy because it arrived a
  # fraction past an internal counter would be a false red, which is the whole
  # point of this task. Checking the budget first also puts the last probe
  # strictly inside the budget, the off-by-one already fixed in 23d8bc5.
  # Overshoot is bounded by one probe plus --max-time.
  if [ $(( SECONDS - started_at )) -ge "$HEALTH_BUDGET_SECONDS" ]; then
    break
  fi
  sleep 1
done

elapsed=$(( SECONDS - started_at ))

# Re-read the unit state before classifying. Inside the loop it is sampled
# before the probe, and a probe may take up to --max-time: a service that dies
# during the last one would be reported as "active but unhealthy" from a stale
# reading, sending the reader after a health bug that is really a crash.
state="$(systemctl is-active openclaw-hub || true)"

echo "service=$state healthz=${code:-000} elapsed=${elapsed}s budget=${HEALTH_BUDGET_SECONDS}s"

# Three outcomes, deliberately distinguishable in the job log: the service never
# came up, it came up but never answered, or it is serving. Previously the last
# two collapsed into the same silent `exit 1`.
if [ "$state" != "active" ]; then
  echo "FAIL: openclaw-hub is not active after restart (state=$state)" >&2
  sudo journalctl -u openclaw-hub -n 40 --no-pager || true
  exit 1
fi

if [ "$code" != "200" ]; then
  echo "FAIL: openclaw-hub is active but /healthz never returned 200 within ${HEALTH_BUDGET_SECONDS}s (last=${code:-000})" >&2
  # The unit log belongs on this branch too. It used to be printed only when the
  # service was down — that is, only when the cause was already obvious.
  sudo journalctl -u openclaw-hub -n 40 --no-pager || true
  exit 1
fi

echo "deploy ok"
