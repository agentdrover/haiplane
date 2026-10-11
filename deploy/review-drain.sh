#!/usr/bin/env bash
# Drain of local deep reviews before a hub restart (#1588).
#
# A deploy restarts the hub, and the restart cancels a running local review
# (Qwen via haiplane-review-runner). This script makes the deploy WAIT for the
# running jobs, up to one shared budget, and keeps NEW local orders out while it
# waits. The protocol is two files in the review spool (deploy/LOCAL-REVIEW.md,
# docs/review-deep-lifecycle.md, scenario 1):
#
#   <spool>/.drain.lock   inter-process lock (flock). Permanent inode: it is
#                         created once and never deleted or replaced. Held ONLY
#                         for short operations (marker set / renew / release,
#                         job count, hub's check+publish) and released before
#                         any sleep, rsync or restart.
#   <spool>/draining      marker: "owner=<deploy id>" and "expires=<epoch>".
#                         Written atomically, renewed independently of the wait
#                         loop for the whole deploy, ignored once expired.
#
# The hub (hub/integrations/local_reviewer.py) refuses a new local run while a
# FRESH marker exists: before it waits for the host slot and again, under the
# same lock, at the moment it publishes job.json. A hub start never removes the
# marker; only the deploy that wrote it does (own owner id only).
#
# Commands (state comes from the environment):
#   acquire       set the marker, wait for unfinished jobs, print the outcome
#   recheck       renew, then wait again for jobs within the REMAINING budget
#   renew-loop    renew the marker every DRAIN_RENEW_SECONDS until stopped;
#                 prints "drain degraded (...)" if the marker could not be kept
#                 alive
#   release       remove the marker if it is ours
#
# Liveness of the deploy is a CHANNEL, never a pid or a signal. The deploy runs
# as one unix user and this script as another (the hub's), so kill(2) across
# them answers EPERM, which reads exactly like "process gone". With
# DRAIN_STDIN_LIVENESS=1 the caller keeps stdin (a pipe/fifo) open; EOF on it,
# which the death of the caller causes under ANY uid, makes acquire/recheck
# stop (acquire takes its own marker back) and renew-loop remove its marker.
#
# Outcomes, one line each, always exit 0 (a drain must never fail a deploy):
#   drain ok (...)          no unfinished job (or all finished in time)
#   drain timeout (jobs)    budget spent, listed jobs still running
#   drain degraded (why)    drain impossible or not guaranteed (no spool, no
#                           lock, foreign marker, renewal failed)
#
# "Unfinished job" = spool/job-<16 hex> holding job.json or claimed and no
# result.json. The guarantee ends at result.json: the hub may still need to
# process it, and a restart at that point can lose the report (see the doc).
#
# Environment:
#   HAIPLANE_LOCAL_REVIEW_SPOOL_DIR  spool dir (required; no default)
#   DRAIN_OWNER             id of this deploy (default: deploy-<pid>-<epoch>)
#   DRAIN_BUDGET_SECONDS    one budget for every wait, lock included (1800)
#   DRAIN_DEADLINE          epoch end of that budget; set by the caller so that
#                           acquire and recheck share ONE budget
#   DRAIN_TTL_SECONDS       marker lifetime (2700)
#   DRAIN_RENEW_SECONDS     renewal period (TTL / 3)
#   DRAIN_MAX_HOLD_SECONDS  the renew loop stops after this (budget + 3600)
#   DRAIN_RENEW_LOCK_WAIT   lock wait of one renewal (20)
#   DRAIN_PROGRESS_SECONDS  progress line period (30)
#   DRAIN_POLL_SECONDS      pause between job counts (2)
#
# Runs as the hub's unix user (see remote-deploy.sh): the spool is
# <hub user>:haiplane-review 2770 and the lock/marker are 0660 in that group.
# Compatible with bash 3.2 (macOS) so the tests run on a laptop.
# shellcheck disable=SC2329  # functions run through locked(), i.e. indirectly
set -u

# No default path: the spool is a server fact. remote-deploy.sh resolves it from
# the hub unit; an empty value is a named "drain degraded", not a guess.
SPOOL="${HAIPLANE_LOCAL_REVIEW_SPOOL_DIR:-}"
LOCK="$SPOOL/.drain.lock"
MARKER="$SPOOL/draining"
BUDGET="${DRAIN_BUDGET_SECONDS:-1800}"
TTL="${DRAIN_TTL_SECONDS:-2700}"
RENEW_EVERY="${DRAIN_RENEW_SECONDS:-$((TTL / 3))}"
PROGRESS_EVERY="${DRAIN_PROGRESS_SECONDS:-30}"
POLL="${DRAIN_POLL_SECONDS:-2}"
OWNER="${DRAIN_OWNER:-deploy-$$-$(date +%s)}"
DEADLINE="${DRAIN_DEADLINE:-$(($(date +%s) + BUDGET))}"
MAX_HOLD="${DRAIN_MAX_HOLD_SECONDS:-$((BUDGET + 3600))}"
# Lock wait of one renewal; a busy lock is retried on the next round.
RENEW_LOCK_WAIT="${DRAIN_RENEW_LOCK_WAIT:-20}"
# Lock wait for cleanup work, which must still happen after the budget is gone.
CLEANUP_LOCK_WAIT=30

umask 007

now() { date +%s; }

remaining() {
  local left=$((DEADLINE - $(now)))
  [ "$left" -gt 0 ] || left=0
  echo "$left"
}

# Lock wait inside the budget, never less than one second: with the budget
# spent, a lock the hub holds for a few milliseconds must not read as "busy".
lock_wait() {
  local left
  left="$(remaining)"
  [ "$left" -ge 1 ] || left=1
  echo "$left"
}

# locked TIMEOUT cmd args...: run a function under the lock. rc 75 = the lock
# stayed busy for TIMEOUT seconds; any other non-zero rc comes from the function
# or from not being able to open the lock file at all.
locked() {
  local wait="$1"
  shift
  (
    flock -w "$wait" 9 || exit 75
    "$@"
  ) 9>>"$LOCK"
}

# The lock file is created once, here, and then only opened. 0660 + the group of
# the (setgid) spool directory is what lets the hub and the deploy share it.
ensure_lock() {
  [ -e "$LOCK" ] && return 0
  : >>"$LOCK" 2>/dev/null || return 1
  chmod 0660 "$LOCK" 2>/dev/null || true
}

spool_problem() {
  if [ -z "$SPOOL" ]; then
    echo "каталог очереди не задан (HAIPLANE_LOCAL_REVIEW_SPOOL_DIR пуст)"
  elif [ ! -d "$SPOOL" ]; then
    echo "каталог очереди $SPOOL не найден"
  elif [ ! -r "$SPOOL" ] || [ ! -w "$SPOOL" ] || [ ! -x "$SPOOL" ]; then
    echo "нет прав на каталог очереди $SPOOL (нужны rwx)"
  elif ! ensure_lock; then
    echo "не удалось создать замок $LOCK"
  fi
}

marker_field() {
  [ -f "$MARKER" ] || return 1
  sed -n "s/^$1=//p" "$MARKER" 2>/dev/null | head -n 1
}

# 0 when a marker of someone else is fresh. Prints its owner.
foreign_fresh_marker() {
  local owner expires
  owner="$(marker_field owner || true)"
  expires="$(marker_field expires || true)"
  [ -n "$owner" ] || return 1
  [ "$owner" != "$OWNER" ] || return 1
  case "$expires" in
    '' | *[!0-9]*) return 1 ;;
  esac
  [ "$expires" -gt "$(now)" ] || return 1
  echo "$owner"
}

# Atomic: write beside the marker, then rename. Needs the lock.
write_marker() {
  local tmp="$SPOOL/draining.tmp.$$"
  printf 'owner=%s\nexpires=%s\n' "$OWNER" "$(($(now) + TTL))" >"$tmp" || {
    rm -f "$tmp"
    return 1
  }
  chmod 0660 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$MARKER" || {
    rm -f "$tmp"
    return 1
  }
}

# Set our marker unless someone else's fresh one stands. rc 3 = foreign marker.
set_marker_unless_foreign() {
  local other
  if other="$(foreign_fresh_marker)"; then
    echo "$other" >&2
    return 3
  fi
  write_marker
}

# Renew our marker, but never take over a foreign one. rc 4 = not ours.
renew_marker() {
  local owner
  owner="$(marker_field owner || true)"
  if [ "$owner" = "$OWNER" ]; then
    write_marker
    return $?
  fi
  return 4
}

remove_own_marker() {
  [ "$(marker_field owner || true)" = "$OWNER" ] || return 4
  rm -f "$MARKER"
}

active_jobs() {
  local d name
  for d in "$SPOOL"/job-*; do
    [ -d "$d" ] && [ ! -L "$d" ] || continue
    name="${d##*/}"
    [[ "$name" =~ ^job-[0-9a-f]{16}$ ]] || continue
    [ -e "$d/result.json" ] && continue
    if [ -e "$d/job.json" ] || [ -e "$d/claimed" ]; then
      echo "$name"
    fi
  done
}

emit() { echo "$*"; }

# Marker first: from here on the hub refuses new local orders. A foreign fresh
# marker is waited out inside the budget (it is not ours to touch).
take_marker() {
  local other rc
  while :; do
    other="$(locked "$(lock_wait)" set_marker_unless_foreign 2>&1)"
    rc=$?
    case "$rc" in
      0) return 0 ;;
      3)
        if [ "$(remaining)" -le 0 ]; then
          emit "drain degraded (чужой свежий маркер выкладки владельца '$other' не снят за срок ${BUDGET} с; он не тронут)"
          return 1
        fi
        sleep "$POLL"
        ;;
      75)
        emit "drain degraded (замок $LOCK занят дольше срока ${BUDGET} с)"
        return 1
        ;;
      *)
        emit "drain degraded (маркер не записан в $SPOOL, rc=$rc)"
        return 1
        ;;
    esac
  done
}

# Count jobs under the lock, sleep OUTSIDE it. Prints the outcome line.
wait_for_jobs() {
  local label="$1" jobs rc last_progress
  last_progress="$(now)"
  while :; do
    jobs="$(locked "$(lock_wait)" active_jobs)"
    rc=$?
    if [ "$rc" -eq 75 ]; then
      emit "drain degraded (замок $LOCK занят дольше срока ${BUDGET} с, задания не пересчитаны)"
      return 0
    elif [ "$rc" -ne 0 ]; then
      emit "drain degraded (задания в $SPOOL не пересчитаны, rc=$rc)"
      return 0
    fi
    if [ -z "$jobs" ]; then
      emit "drain ok ($label: незавершённых заданий нет, осталось срока $(remaining) с)"
      return 0
    fi
    if [ "$(remaining)" -le 0 ]; then
      emit "drain timeout ($(echo "$jobs" | tr '\n' ' ' | sed 's/ $//'))"
      return 0
    fi
    if [ $(($(now) - last_progress)) -ge "$PROGRESS_EVERY" ]; then
      emit "drain: ждём $(echo "$jobs" | wc -l | tr -d ' ') зад.: $(echo "$jobs" | tr '\n' ' ')осталось срока $(remaining) с"
      last_progress="$(now)"
    fi
    sleep "$POLL"
  done
}

# EOF on stdin = the deploy is gone. The signal goes to ourselves, so it never
# crosses a uid boundary. An async child of a non-interactive bash gets
# /dev/null for stdin, hence the explicit duplicate.
start_liveness() {
  [ "${DRAIN_STDIN_LIVENESS:-0}" = 1 ] || return 0
  local self=$$ sig="$1"
  exec 3<&0
  (
    cat <&3 >/dev/null 2>&1
    kill "-$sig" "$self" 2>/dev/null
  ) &
}

cmd_acquire() {
  local problem
  problem="$(spool_problem)"
  if [ -n "$problem" ]; then
    emit "drain degraded ($problem)"
    return 0
  fi
  KEEP_MARKER=0
  MARKER_SET=0
  # A kill during the wait must not leave our marker behind.
  trap 'cleanup_acquire' EXIT
  trap 'exit 143' TERM
  trap 'exit 130' INT HUP
  start_liveness TERM
  # Raised BEFORE the write: a TERM that lands between the rename and the next
  # statement must still find the flag. An ordinary refusal lowers it again.
  MARKER_SET=1
  take_marker || {
    MARKER_SET=0
    return 0
  }
  emit "drain: маркер $MARKER поставлен (владелец $OWNER, срок $BUDGET с)"
  wait_for_jobs acquire
  KEEP_MARKER=1
}

cleanup_acquire() {
  # Nothing to take back unless the marker may have been written, and a kept
  # one is the deploy's to release.
  [ "${MARKER_SET:-0}" = 1 ] || return 0
  [ "${KEEP_MARKER:-0}" = 1 ] && return 0
  # The flag goes up before the write, so there may be nothing of ours yet (a
  # stop during the lock wait or behind a foreign marker): leave without waiting
  # for the lock. The final owner check stays under the lock.
  [ "$(marker_field owner || true)" = "$OWNER" ] || return 0
  locked "$CLEANUP_LOCK_WAIT" remove_own_marker >/dev/null 2>&1 || true
}

cmd_recheck() {
  local problem rc
  trap 'exit 143' TERM
  trap 'exit 130' INT HUP
  start_liveness TERM
  problem="$(spool_problem)"
  if [ -n "$problem" ]; then
    emit "drain degraded ($problem)"
    return 0
  fi
  locked "$(lock_wait)" renew_marker
  rc=$?
  if [ "$rc" -eq 4 ]; then
    emit "drain degraded (повторная проверка: маркер не наш, повторная проверка заданий не гарантирует очередь)"
    return 0
  elif [ "$rc" -ne 0 ]; then
    emit "drain degraded (повторная проверка: маркер не продлён, rc=$rc)"
    return 0
  fi
  wait_for_jobs "перед restart"
}

cmd_renew_loop() {
  local started failed owned rc sleeper
  started="$(now)"
  owned=0
  failed=0
  sleeper=""
  trap '[ -n "$sleeper" ] && kill "$sleeper" 2>/dev/null; exit 0' TERM INT HUP
  # EOF on stdin: the deploy is gone, with or without its own cleanup. Do not
  # leave a marker that no one is going to renew OR remove.
  trap '[ -n "$sleeper" ] && kill "$sleeper" 2>/dev/null
    emit "drain: renew-loop stopped by parent EOF"
    locked "$CLEANUP_LOCK_WAIT" remove_own_marker >/dev/null 2>&1 || true
    exit 0' USR1
  start_liveness USR1
  while :; do
    if [ $(($(now) - started)) -ge "$MAX_HOLD" ]; then
      if [ "$owned" = 1 ]; then
        emit "drain degraded (продление маркера остановлено: прошло ${MAX_HOLD} с, дальше маркер истечёт сам)"
      fi
      exit 0
    fi
    if [ -d "$SPOOL" ] && ensure_lock; then
      locked "$RENEW_LOCK_WAIT" renew_marker
      rc=$?
      case "$rc" in
        0) owned=1; failed=0 ;;
        4) ;; # not ours (yet, or a foreign one stands): nothing to keep alive
        *)
          if [ "$owned" = 1 ] && [ "$failed" = 0 ]; then
            emit "drain degraded (маркер не продлён, rc=$rc: окно запрета новых заказов не гарантировано)"
            failed=1
          fi
          ;;
      esac
    fi
    sleep "$RENEW_EVERY" &
    sleeper=$!
    wait "$sleeper" 2>/dev/null
    sleeper=""
  done
}

cmd_release() {
  local problem rc
  problem="$(spool_problem)"
  if [ -n "$problem" ]; then
    emit "drain: маркер не снят ($problem)"
    return 0
  fi
  locked "$CLEANUP_LOCK_WAIT" remove_own_marker
  rc=$?
  case "$rc" in
    0) emit "drain: маркер снят (владелец $OWNER)" ;;
    4) emit "drain: маркер не наш или его нет — оставлен как есть" ;;
    *) emit "drain: маркер не снят, rc=$rc (истечёт сам через TTL ${TTL} с)" ;;
  esac
  return 0
}

main() {
  local cmd="${1:-}"
  shift || true
  case "$cmd" in
    acquire) cmd_acquire ;;
    recheck) cmd_recheck ;;
    renew-loop) cmd_renew_loop ;;
    release) cmd_release ;;
    *)
      echo "usage: review-drain.sh acquire|recheck|renew-loop|release" >&2
      return 2
      ;;
  esac
}

main "$@"
exit "$?"
