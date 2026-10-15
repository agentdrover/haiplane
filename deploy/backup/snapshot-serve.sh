#!/bin/sh
# ForceCommand of the `backup` user (#1707): serves the newest published copy.
#
#   snapshot latest | daily | latest-meta | daily-meta
#
# Anything else (a shell, scp, sftp, extra words, `;`, `..`) exits 2 and prints
# nothing. Exit codes: 0 served, 2 refused, 3 no such copy, 4 copy too old
# (latest: 90 minutes, daily: 26 hours).
#
# Usage in sshd: ForceCommand /usr/local/lib/haiplane-backup/snapshot-serve.sh [STATE_DIR]
# The state directory is an argument of the trusted sshd config, never of the client.
# SNAP_NOW (epoch seconds) exists for tests; sshd does not pass the client's
# environment to the command.

STATE=${1:-/var/lib/haiplane-backup}
LC_ALL=C
export LC_ALL
umask 077

case "${SSH_ORIGINAL_COMMAND-}" in
    "snapshot latest")     prefix=hub;  limit=5400;  want=data ;;
    "snapshot latest-meta")  prefix=hub;  limit=5400;  want=meta ;;
    "snapshot daily")      prefix=conf; limit=93600; want=data ;;
    "snapshot daily-meta") prefix=conf; limit=93600; want=meta ;;
    *) exit 2 ;;
esac

stamp_glob='[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z'

# shellcheck disable=SC2086 # the stamp pattern is a glob on purpose
newest=$(printf '%s\n' "$STATE"/"$prefix"-$stamp_glob.meta.json 2>/dev/null | sort | tail -n 1)
[ -f "$newest" ] || exit 3

base=${newest%.meta.json}
name=${base##*/}
stamp=${name#"$prefix"-}

# epoch of "now - limit" as a UTC stamp, GNU date or BSD date
now=${SNAP_NOW:-$(date +%s)}
threshold=$((now - limit))
oldest=$(date -u -d "@$threshold" +%Y%m%dT%H%M%SZ 2>/dev/null || date -u -r "$threshold" +%Y%m%dT%H%M%SZ 2>/dev/null) || exit 2
digits() { printf '%s' "$1" | tr -d 'TZ'; }
if [ "$(digits "$stamp")" -lt "$(digits "$oldest")" ]; then
    exit 4
fi

if [ "$want" = meta ]; then
    target=$newest
elif [ "$prefix" = hub ]; then
    target=$base.db.age
else
    target=$base.tar.age
fi

# Open first, then read through the descriptor: rotation after this point
# unlinks the name but the open file is still read in full.
( : <"$target" ) 2>/dev/null || exit 3
exec 3<"$target"
exec cat <&3
