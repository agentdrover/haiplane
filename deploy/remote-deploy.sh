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
HEALTH_URL="http://127.0.0.1:8080/healthz"

if [ ! -d "$STAGING" ]; then
  echo "staging directory $STAGING is missing; rsync step did not run" >&2
  exit 1
fi

sudo rsync -a --delete "$STAGING/" "$DEST/"
sudo chown -R openclaw:openclaw "$DEST"
sudo -u openclaw /opt/openclaw-hub/venv/bin/pip install -e "$DEST" -q
sudo systemctl restart openclaw-hub

sleep 2

state="$(systemctl is-active openclaw-hub || true)"
echo "service=$state"
if [ "$state" != "active" ]; then
  echo "openclaw-hub is not active after restart" >&2
  sudo journalctl -u openclaw-hub -n 40 --no-pager || true
  exit 1
fi

code="$(curl -sf -o /dev/null -w '%{http_code}' "$HEALTH_URL" || true)"
echo "healthz=$code"
if [ "$code" != "200" ]; then
  echo "healthz did not return 200" >&2
  exit 1
fi

echo "deploy ok"
