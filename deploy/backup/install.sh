#!/bin/sh
# Installs the off-server copy service on the hub host (#1707). The owner runs it
# with sudo, once; a second run changes nothing. It is NOT part of the deploy.
#
#   sudo sh install.sh --ssh-pubkey-file PUB --age-recipient-file RECIPIENT
#   sudo sh install.sh --ssh-pubkey "ssh-ed25519 AAAA... name" --age-recipient age1...
#
# --ssh-pubkey[-file]      public ssh key of the machine that fetches the copies
# --age-recipient[-file]   public age recipient: the private key stays at home
# --site-host HOST         the nginx site of the hub is the one file in the sites
#                          directory whose server_name lists HOST (default agenthai.ru);
#                          no such file, or more than one, stops the install before any change
# --sites-dir DIR          that directory (default /etc/nginx/sites-available)
# --no-services            do not touch systemd and do not reload sshd (a host
#                          without a running systemd, e.g. a CI container)
# Keys are arguments or files; no key is kept in the repository. Without a key
# argument an earlier installed one is kept.
#
# What it sets up:
#   user `haiplane-backup` (not the distribution's `backup`) (shell /bin/sh, no password, home owned by root, authorized_keys
#   owned by root: the user reads it but cannot change it), sshd drop-in with
#   `Match User haiplane-backup` -> ForceCommand snapshot-serve.sh (no tty, no forwarding),
#   /var/lib/haiplane-backup (0750 root:haiplane-backup), scripts in /usr/local/lib/haiplane-backup,
#   haiplane-snapshot-hourly.timer and haiplane-snapshot-daily.timer.
# The nightly haiplane-hub-backup is not touched.

set -eu

SRC_DIR=$(cd "$(dirname "$0")" && pwd)
USER_NAME=haiplane-backup
HOME_DIR=/var/lib/haiplane-backup-home
STATE_DIR=/var/lib/haiplane-backup
ETC_DIR=/etc/haiplane-backup
LIB_DIR=/usr/local/lib/haiplane-backup
DROPIN=/etc/ssh/sshd_config.d/60-haiplane-backup.conf
UNIT_DIR=/etc/systemd/system

die() {
    echo "install.sh: $*" >&2
    exit "${2:-1}"
}

SSH_KEY=
RECIPIENT=
SERVICES=1
SITE_HOST=agenthai.ru
SITES_DIR=/etc/nginx/sites-available

# Reads the first value line of a file (not empty, not a comment).
first_line() {
    sed -n -e '/^[[:space:]]*$/d' -e '/^[[:space:]]*#/d' -e '1p' "$1"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --ssh-pubkey) [ $# -ge 2 ] || die "--ssh-pubkey needs a value" 2; SSH_KEY=$2; shift 2 ;;
        --ssh-pubkey-file) [ $# -ge 2 ] || die "--ssh-pubkey-file needs a path" 2
            [ -r "$2" ] || die "cannot read $2" 2
            SSH_KEY=$(first_line "$2"); shift 2 ;;
        --age-recipient) [ $# -ge 2 ] || die "--age-recipient needs a value" 2; RECIPIENT=$2; shift 2 ;;
        --age-recipient-file) [ $# -ge 2 ] || die "--age-recipient-file needs a path" 2
            [ -r "$2" ] || die "cannot read $2" 2
            RECIPIENT=$(sed -e '/^[[:space:]]*$/d' -e '/^[[:space:]]*#/d' "$2"); shift 2 ;;
        --site-host) [ $# -ge 2 ] || die "--site-host needs a value" 2; SITE_HOST=$2; shift 2 ;;
        --sites-dir) [ $# -ge 2 ] || die "--sites-dir needs a path" 2; SITES_DIR=$2; shift 2 ;;
        --no-services) SERVICES=0; shift ;;
        *) die "unknown argument: $1" 2 ;;
    esac
done

# ---- validate the keys before anything is touched (no root needed) ----------
if [ -n "$SSH_KEY" ]; then
    case "$SSH_KEY" in
        *'
'*) die "the ssh key must be one line" 2 ;;
    esac
    kind=${SSH_KEY%% *}
    case "$kind" in
        ssh-ed25519 | ssh-rsa | ecdsa-sha2-nistp256 | ecdsa-sha2-nistp384 | ecdsa-sha2-nistp521 | sk-ssh-ed25519@openssh.com | sk-ecdsa-sha2-nistp256@openssh.com) ;;
        *) die "the ssh key must be a bare public key (type, key, comment), without options" 2 ;;
    esac
    probe=$(mktemp)
    printf '%s\n' "$SSH_KEY" >"$probe"
    if ! ssh-keygen -l -f "$probe" >/dev/null 2>&1; then
        rm -f "$probe"
        die "the ssh key is not a valid public key" 2
    fi
    rm -f "$probe"
fi
if [ -n "$RECIPIENT" ]; then
    printf '%s\n' "$RECIPIENT" | while IFS= read -r line; do
        case "$line" in
            age1[0-9a-z]*) ;;
            *) exit 1 ;;
        esac
        case "$line" in
            *[!0-9a-z]*) exit 1 ;;
        esac
        [ "${#line}" -ge 24 ] || exit 1
    done || die "the age recipient must be public key lines starting with age1" 2
fi

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
command -v age >/dev/null 2>&1 || die "age is not installed (apt install age)"
SSHD=$(command -v sshd 2>/dev/null || true)
[ -n "$SSHD" ] || SSHD=/usr/sbin/sshd
[ -x "$SSHD" ] || die "sshd not found"
for f in hub-snapshot.py snapshot-serve.sh; do
    [ -f "$SRC_DIR/$f" ] || die "missing $SRC_DIR/$f"
done

# ---- the nginx site of the hub: found by server_name, before anything is touched ----
# A file counts when a server_name directive lists SITE_HOST as a whole name.
SITE_FILES=
SITE_COUNT=0
for site in "$SITES_DIR"/*; do
    [ -f "$site" ] || continue
    if sed -n 's/^[[:space:]]*server_name[[:space:]][[:space:]]*\([^;]*\);.*/\1/p' "$site" |
        tr -s ' \t' '\n\n' | grep -qxF -- "$SITE_HOST"; then
        SITE_FILES="$SITE_FILES $site"
        SITE_COUNT=$((SITE_COUNT + 1))
    fi
done
case "$SITE_COUNT" in
    1) SITE_FILE=${SITE_FILES# } ;;
    0) die "no nginx site in $SITES_DIR has server_name $SITE_HOST (--site-host / --sites-dir); nothing was changed" ;;
    *) die "$SITE_COUNT nginx sites in $SITES_DIR have server_name $SITE_HOST:$SITE_FILES; nothing was changed" ;;
esac

AUTH_KEYS=$HOME_DIR/.ssh/authorized_keys
# An earlier installed key is kept; its options are rebuilt below.
if [ -z "$SSH_KEY" ] && [ -f "$AUTH_KEYS" ]; then
    SSH_KEY=$(sed -n -e 's/^restrict,command="[^"]*" //p' -e 's/^restrict //p' "$AUTH_KEYS" | head -n 1)
fi
[ -n "$SSH_KEY" ] || die "no ssh key yet: pass --ssh-pubkey or --ssh-pubkey-file" 2
[ -n "$RECIPIENT" ] || [ -f "$ETC_DIR/recipient.txt" ] || die "no age recipient yet: pass --age-recipient or --age-recipient-file" 2

CHANGED=0
note() {
    CHANGED=1
    echo "updated: $1"
}

# put_file DEST MODE SOURCE: replace DEST (root:root) only when the content differs.
put_file() {
    if [ -f "$1" ] && cmp -s "$3" "$1"; then
        [ "$(stat -c %a "$1")" = "$(printf '%o' "$((0$2))")" ] || chmod "$2" "$1"
        return 0
    fi
    install -m "$2" -o root -g root "$3" "$1"
    note "$1"
}

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# ---- user -------------------------------------------------------------------
if ! id "$USER_NAME" >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir "$HOME_DIR" --shell /bin/sh --user-group "$USER_NAME"
    note "user $USER_NAME"
fi
entry=$(getent passwd "$USER_NAME")
cur_home=$(printf '%s' "$entry" | cut -d: -f6)
cur_shell=$(printf '%s' "$entry" | cut -d: -f7)
if [ "$cur_home" != "$HOME_DIR" ] || [ "$cur_shell" != /bin/sh ]; then
    usermod --home "$HOME_DIR" --shell /bin/sh "$USER_NAME"
    note "account $USER_NAME home and shell"
fi
# '*' and not '!': sshd without PAM treats a '!' lock as a ban even for a key.
if [ "$(getent shadow "$USER_NAME" | cut -d: -f2)" != '*' ]; then
    usermod -p '*' "$USER_NAME"
    note "password field of $USER_NAME"
fi

# ---- directories ------------------------------------------------------------
install -d -m 0755 -o root -g root "$HOME_DIR" "$HOME_DIR/.ssh" "$ETC_DIR" "$LIB_DIR"
install -d -m 0750 -o root -g "$USER_NAME" "$STATE_DIR"
install -d -m 0700 -o root -g root "$STATE_DIR/work"

put_file "$LIB_DIR/hub-snapshot.py" 0755 "$SRC_DIR/hub-snapshot.py"
put_file "$LIB_DIR/snapshot-serve.sh" 0755 "$SRC_DIR/snapshot-serve.sh"

# ---- keys -------------------------------------------------------------------
if [ -n "$RECIPIENT" ]; then
    printf '%s\n' "$RECIPIENT" >"$TMP/recipient"
    put_file "$ETC_DIR/recipient.txt" 0644 "$TMP/recipient"
fi
[ -f "$ETC_DIR/recipient.txt" ] || die "no age recipient yet: pass --age-recipient or --age-recipient-file" 2


# What the archive may contain. Written once; the owner edits it afterwards.
# the settings directory of the hub keeps its earlier name on the hosts
HUB_ETC=/etc/open""claw-hub
DEFAULT_ENTRIES="unit haiplane-hub
required /etc/georoute
required /etc/systemd/networkd.conf.d
required /etc/fail2ban/jail.local
required $SITE_FILE
required $HUB_ETC
optional /etc/haiplane-backup"
if [ ! -f "$ETC_DIR/paths.conf" ]; then
    cat >"$TMP/paths.conf" <<EOF
# Allowlist of the daily settings archive (#1707). Nothing outside it is read.
#   unit NAME            unit file, its drop-ins and its EnvironmentFiles (required)
#   optional-unit NAME   the same, skipped when the unit does not exist
#   required PATH        file or directory; missing = the snapshot fails
#   optional PATH        file or directory; missing = skipped
# Symlinks are never followed out of the allowlist. TLS keys are not listed on
# purpose: after a restore the certificate is issued again (certbot).
$DEFAULT_ENTRIES
# optional-unit haiplane-hub-poller
# optional /etc/haiplane-review-runner
EOF
    put_file "$ETC_DIR/paths.conf" 0644 "$TMP/paths.conf"
else
    # An existing paths.conf is the owner's: it is never rewritten. What differs from
    # the defaults is only reported (stderr), so the owner can correct it by hand.
    printf '%s\n' "$DEFAULT_ENTRIES" | while IFS= read -r entry; do
        grep -qxF -- "$entry" "$ETC_DIR/paths.conf" ||
            echo "warning: $ETC_DIR/paths.conf lacks the default entry: $entry" >&2
    done
    while read -r kind value; do
        [ "$kind" = required ] || continue
        [ -e "$value" ] && continue
        case "$value" in
            "$SITES_DIR"/* | */sites-available/*)
                echo "warning: $ETC_DIR/paths.conf: required path $value does not exist; the site of $SITE_HOST is $SITE_FILE: replace the entry with 'required $SITE_FILE'" >&2 ;;
            *) echo "warning: $ETC_DIR/paths.conf: required path $value does not exist on this machine" >&2 ;;
        esac
    done <"$ETC_DIR/paths.conf"
fi

# ---- sshd ---------------------------------------------------------------------
cat >"$TMP/dropin" <<EOF
# Managed by deploy/backup/install.sh (#1707): the copy fetcher can run one command, nothing else.
Match User $USER_NAME
    ForceCommand $LIB_DIR/snapshot-serve.sh $STATE_DIR
    PasswordAuthentication no
    AllowTcpForwarding no
    X11Forwarding no
    PermitTTY no
    AllowAgentForwarding no
    PermitTunnel no
EOF
[ -d /run/sshd ] || install -d -m 0755 /run/sshd
install -d -m 0755 -o root -g root "$(dirname "$DROPIN")"
if ! { [ -f "$DROPIN" ] && cmp -s "$TMP/dropin" "$DROPIN"; }; then
    [ ! -f "$DROPIN" ] || cp -p "$DROPIN" "$TMP/dropin.old"
    install -m 0644 -o root -g root "$TMP/dropin" "$DROPIN"
    if ! "$SSHD" -t; then
        if [ -f "$TMP/dropin.old" ]; then
            cp -p "$TMP/dropin.old" "$DROPIN"
        else
            rm -f "$DROPIN"
        fi
        die "sshd -t rejected the new configuration; the drop-in was rolled back"
    fi
    note "$DROPIN"
fi
# The drop-in counts only if the main sshd_config includes it: ask sshd itself.
effective=$("$SSHD" -T -C "user=$USER_NAME,host=localhost,addr=127.0.0.1")
for want in "forcecommand $LIB_DIR/snapshot-serve.sh $STATE_DIR" "passwordauthentication no" \
    "permittty no" "allowtcpforwarding no" "x11forwarding no" "allowagentforwarding no" "permittunnel no"; do
    printf '%s\n' "$effective" | grep -qx "$want" ||
        die "sshd does not apply '$want' to $USER_NAME: does /etc/ssh/sshd_config have 'Include /etc/ssh/sshd_config.d/*.conf'?"
done

# The checked configuration is loaded on EVERY run, not only when the drop-in
# changed: a run that failed after writing the drop-in must not leave sshd on the
# old configuration. Only after that the key (and with it the way in) appears.
if [ "$SERVICES" = 1 ]; then
    systemctl reload ssh 2>/dev/null || systemctl reload sshd
fi
# The forced command is in the key itself, so it holds even for an sshd that has
# not loaded the drop-in: `restrict` alone would still run any command.
printf 'restrict,command="%s/snapshot-serve.sh %s" %s\n' "$LIB_DIR" "$STATE_DIR" "$SSH_KEY" >"$TMP/authorized_keys"
put_file "$AUTH_KEYS" 0644 "$TMP/authorized_keys"

# ---- timers -------------------------------------------------------------------
unit_service() { # NAME KIND DESCRIPTION EXTRA
    cat >"$TMP/$1.service" <<EOF
[Unit]
Description=$3

[Service]
Type=oneshot
User=root
UMask=0077
PrivateTmp=yes
NoNewPrivileges=yes
TimeoutStartSec=600
$4ExecStart=/usr/bin/python3 $LIB_DIR/hub-snapshot.py $2
EOF
}
unit_timer() { # NAME DESCRIPTION CALENDAR EXTRA
    cat >"$TMP/$1.timer" <<EOF
[Unit]
Description=$2

[Timer]
OnCalendar=$3
${4}Persistent=true

[Install]
WantedBy=timers.target
EOF
}
unit_service haiplane-snapshot-hourly hourly "Haiplane hub: hourly encrypted database snapshot" \
    "TemporaryFileSystem=$STATE_DIR/work:size=512M,mode=0700
"
unit_service haiplane-snapshot-daily daily "Haiplane hub: daily encrypted settings archive" ""
unit_timer haiplane-snapshot-hourly "Haiplane hub: hourly snapshot" "*:17:00" "RandomizedDelaySec=5min
"
unit_timer haiplane-snapshot-daily "Haiplane hub: daily settings archive" "*-*-* 03:47:00 UTC" ""
UNITS_CHANGED=0
for f in haiplane-snapshot-hourly.service haiplane-snapshot-hourly.timer \
    haiplane-snapshot-daily.service haiplane-snapshot-daily.timer; do
    before=$CHANGED
    CHANGED=0
    put_file "$UNIT_DIR/$f" 0644 "$TMP/$f"
    [ "$CHANGED" = 0 ] || UNITS_CHANGED=1
    CHANGED=$((before + CHANGED))
done
[ "$CHANGED" = 0 ] || CHANGED=1

if [ "$SERVICES" = 1 ]; then
    if [ "$UNITS_CHANGED" = 1 ]; then
        systemctl daemon-reload
    fi
    systemctl enable --now haiplane-snapshot-hourly.timer haiplane-snapshot-daily.timer
fi

if [ "$CHANGED" = 0 ]; then
    echo "no changes"
else
    echo "installed"
fi
