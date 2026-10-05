#!/usr/bin/env bash
# Remote deploy step for Haiplane Hub, executed on the target server via `ssh ... 'bash -s'`.
#
# Mirrors docs/agent-deploy-runbook.md section 4. The CI runner first rsyncs the
# working tree into the user's staging directory; this script promotes it into the
# service source tree, reinstalls the package, restarts systemd and probes /healthz.
#
# Assumptions (see docs/agent-deploy-runbook.md):
#   - staging dir:     $HOME/haiplane-hub-src-staging (the rsync side of the
#                      CI job writes it)
#   - service source:  /opt/haiplane-hub/src
#   - service venv:    /opt/haiplane-hub/venv
#   - runtime user:    haiplane
#   - systemd unit:    haiplane-hub
#   - the deploy SSH user has passwordless sudo for the commands below, and for
#     `sudo -n -u <runtime user> env … bash -c "<drain script text>" review-drain <command>` (see "Drain" below).
#
# Drain (#1588): before the rsync this script asks deploy/review-drain.sh to wait
# for running LOCAL deep reviews (up to ONE budget, DRAIN_BUDGET_SECONDS, 1800 s)
# and to keep new local orders out meanwhile. The marker that does this is kept
# alive by a background renewer until the health check is over, and removed on
# success, failure and signals (only our own). The drain never fails the deploy:
# its outcome is one log line, `drain ok`, `drain timeout (...)` or
# `drain degraded (...)`. The rsync, pip and restart times are NOT part of the
# guarantee. The drain script is read from staging (the CI rsync has already put
# it there) and run as the hub's unix user, because the spool belongs to it.
set -euo pipefail

STAGING="$HOME/haiplane-hub-src-staging"
DEST=/opt/haiplane-hub/src
# Overridable so the failure branch can be exercised against a dead port instead
# of staging an outage on the live service to prove the gate still bites (#547).
# CI never sets it; the default is the real endpoint.
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/healthz}"

if [ ! -d "$STAGING" ]; then
  echo "staging directory $STAGING is missing; rsync step did not run" >&2
  exit 1
fi

# The runtime user's NAME is a server fact, not this script's business: it is
# read from the owner of the live service tree, so the script keeps working
# both before and after any unix-user rename on the host.
SERVICE_USER="$(sudo stat -c %U "$DEST" 2>/dev/null || true)"
if [ -z "$SERVICE_USER" ] || [ "$SERVICE_USER" = "root" ]; then
  echo "cannot resolve service user from $DEST (got '${SERVICE_USER:-none}')" >&2
  exit 1
fi

# --- Drain of local deep reviews (#1588) -------------------------------------
REVIEW_DRAIN_SCRIPT="${REVIEW_DRAIN_SCRIPT:-$STAGING/deploy/review-drain.sh}"
DRAIN_BUDGET_SECONDS="${DRAIN_BUDGET_SECONDS:-1800}"
DRAIN_OWNER="${DRAIN_OWNER:-deploy-$(date +%s)-$$}"
# ONE budget for every wait of the drain, the lock included.
DRAIN_DEADLINE="${DRAIN_DEADLINE:-$(($(date +%s) + DRAIN_BUDGET_SECONDS))}"
DRAIN_ON=0
DRAIN_CHILD=""
RENEW_PID=""
RENEW_LOG=""
LIVE_DIR=""

# The drain script as the hub's unix user. The script text goes in as the
# argument of `bash -c` (so stdin stays free for the liveness channel below) and
# the state in the environment. The text is read by the DEPLOY user on purpose:
# staging is in its home, which the hub's unix user may not be able to read.
#
# LIVENESS IS A CHANNEL, NOT A PID. This script runs as the deploy user and the
# drain as the hub's user; kill(2) across them is EPERM (and the sudo process
# itself is root's), which a `kill -0` check reads as "dead". So the long drain
# commands get a fifo as stdin and this script holds its write end open (fd 7
# for the renewer, fd 8 for the current step). Closing it - or dying, under any
# uid - gives EOF, and the drain side stops and takes its own marker back.
drain_run() {
  local envs=("DRAIN_OWNER=$DRAIN_OWNER" "DRAIN_DEADLINE=$DRAIN_DEADLINE" "DRAIN_BUDGET_SECONDS=$DRAIN_BUDGET_SECONDS")
  local name
  for name in DRAIN_TTL_SECONDS DRAIN_RENEW_SECONDS DRAIN_POLL_SECONDS \
    DRAIN_PROGRESS_SECONDS DRAIN_MAX_HOLD_SECONDS DRAIN_STDIN_LIVENESS \
    HAIPLANE_LOCAL_REVIEW_SPOOL_DIR; do
    if [ -n "${!name:-}" ]; then
      envs+=("$name=${!name}")
    fi
  done
  sudo -n -u "$SERVICE_USER" env "${envs[@]}" bash -c "$(cat "$REVIEW_DRAIN_SCRIPT")" review-drain "$@"
}

# One drain command in the BACKGROUND and a `wait`: a signal reaches the trap
# at once instead of after a foreground wait of up to the whole budget. The
# waiting commands carry the liveness channel; `release` is short and does not.
drain_step() {
  local rc=0
  case "$1" in
    acquire | recheck)
      mkfifo "$LIVE_DIR/step"
      DRAIN_STDIN_LIVENESS=1 drain_run "$@" <"$LIVE_DIR/step" &
      DRAIN_CHILD=$!
      exec 8>"$LIVE_DIR/step"
      wait "$DRAIN_CHILD" || rc=$?
      exec 8>&-
      rm -f "$LIVE_DIR/step"
      ;;
    *)
      drain_run "$@" </dev/null &
      DRAIN_CHILD=$!
      wait "$DRAIN_CHILD" || rc=$?
      ;;
  esac
  DRAIN_CHILD=""
  if [ "$rc" -ne 0 ]; then
    echo "drain degraded (команда '$1' не отработала, rc=$rc: sudo -u $SERVICE_USER bash недоступен или скрипт упал)"
  fi
  return 0
}

drain_cleanup() {
  local code=$?
  trap - EXIT
  if [ -n "$DRAIN_CHILD" ]; then
    # EOF, not a signal: the child is the hub user's (and sudo is root's).
    exec 8>&-
    wait "$DRAIN_CHILD" 2>/dev/null || true
  fi
  if [ -n "$RENEW_PID" ]; then
    exec 7>&-
    wait "$RENEW_PID" 2>/dev/null || true
    if ! grep -q 'stopped by parent EOF\|drain degraded' "$RENEW_LOG" 2>/dev/null; then
      echo "drain degraded (продление маркера оборвалось до конца выкладки: окно запрета не гарантировано)"
    fi
  fi
  if [ -n "$RENEW_LOG" ] && [ -s "$RENEW_LOG" ]; then
    cat "$RENEW_LOG"
  fi
  if [ "$DRAIN_ON" = 1 ]; then
    drain_step release || true
  fi
  [ -z "$RENEW_LOG" ] || rm -f "$RENEW_LOG"
  [ -z "$LIVE_DIR" ] || rm -rf "$LIVE_DIR"
  exit "$code"
}

trap drain_cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT HUP

# The review spool is a server fact: it comes from the environment of this script
# or, failing that, from the hub unit's own Environment (the drop-in that turns
# the runner transport on). Only that ONE key is extracted; the rest of the
# unit environment (tokens included) is never stored or printed.
if [ -z "${HAIPLANE_LOCAL_REVIEW_SPOOL_DIR:-}" ]; then
  HAIPLANE_LOCAL_REVIEW_SPOOL_DIR="$(systemctl show haiplane-hub -p Environment --value 2>/dev/null |
    tr ' ' '\n' | sed -n 's/^HAIPLANE_LOCAL_REVIEW_SPOOL_DIR=//p' | head -n 1 || true)"
fi
export HAIPLANE_LOCAL_REVIEW_SPOOL_DIR

if [ -z "$HAIPLANE_LOCAL_REVIEW_SPOOL_DIR" ]; then
  echo "drain degraded (каталог очереди не задан: нет HAIPLANE_LOCAL_REVIEW_SPOOL_DIR ни в окружении, ни в юните haiplane-hub; локальный путь, видимо, не включён)"
elif [ -r "$REVIEW_DRAIN_SCRIPT" ]; then
  DRAIN_ON=1
  RENEW_LOG="$(mktemp)"
  LIVE_DIR="$(mktemp -d)"
  mkfifo "$LIVE_DIR/renew"
  # The renewer starts FIRST and keeps the marker fresh for the whole deploy
  # (rsync, pip, restart, health): it is independent of the wait loop below.
  DRAIN_STDIN_LIVENESS=1 drain_run renew-loop >"$RENEW_LOG" 2>&1 <"$LIVE_DIR/renew" &
  RENEW_PID=$!
  exec 7>"$LIVE_DIR/renew"
  drain_step acquire
else
  echo "drain degraded (скрипт $REVIEW_DRAIN_SCRIPT недоступен)"
fi

sudo rsync -a --delete "$STAGING/" "$DEST/" 7>&- 8>&-
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$DEST" 7>&- 8>&-
sudo -u "$SERVICE_USER" /opt/haiplane-hub/venv/bin/pip install -e "$DEST" -q 7>&- 8>&-
if [ "$DRAIN_ON" = 1 ]; then
  # Jobs that slipped in (an old hub ignores the marker) and the time pip took:
  # the same single budget, what is left of it.
  drain_step recheck
fi
sudo systemctl restart haiplane-hub 7>&- 8>&-

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
  state="$(systemctl is-active haiplane-hub || true)"
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
state="$(systemctl is-active haiplane-hub || true)"

echo "service=$state healthz=${code:-000} elapsed=${elapsed}s budget=${HEALTH_BUDGET_SECONDS}s"

# Three outcomes, deliberately distinguishable in the job log: the service never
# came up, it came up but never answered, or it is serving. Previously the last
# two collapsed into the same silent `exit 1`.
if [ "$state" != "active" ]; then
  echo "FAIL: haiplane-hub is not active after restart (state=$state)" >&2
  sudo journalctl -u haiplane-hub -n 40 --no-pager || true
  exit 1
fi

if [ "$code" != "200" ]; then
  echo "FAIL: haiplane-hub is active but /healthz never returned 200 within ${HEALTH_BUDGET_SECONDS}s (last=${code:-000})" >&2
  # The unit log belongs on this branch too. It used to be printed only when the
  # service was down — that is, only when the cause was already obvious.
  sudo journalctl -u haiplane-hub -n 40 --no-pager || true
  exit 1
fi

echo "deploy ok"
