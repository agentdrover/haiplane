#!/usr/bin/env bash
# Manual deploy through the canonical server script (#1620).
#
#   scripts/manual_deploy.sh [--build-set FILE] [TREE]
#
# TREE is the checkout to ship (default: the repo this script lives in). The
# script exports the hashed runtime and build sets from TREE's uv.lock exactly as
# the CI deploy job does, assembles a staging tree, rsyncs it and runs
# deploy/remote-deploy.sh of THIS repo on the server (drain, backup, install by
# hash, recheck, restart). It runs under `set -e`: a failed `uv lock --check`, a
# failed export or a missing input stops everything BEFORE the rsync and before
# the server part, so nothing is shipped from a stale lock.
#
# Rollback to a commit from BEFORE #1620: its lock has no `build` group and its
# own remote-deploy.sh would resolve dependencies. Check that commit out into a
# SEPARATE directory (TREE) and pass --build-set with the build export of the
# CURRENT checkout (uv export --frozen --only-group build --no-emit-project
# --format requirements-txt --no-header -o FILE). The runtime set then comes from
# the old lock; hatchling and editables come, with their hashes, from the file;
# the canonical remote-deploy.sh, review-drain.sh and predeploy-backup.py of this
# checkout replace the old tree's copies in staging, so drain, backup and the
# no-resolution install stay in force.
#
# Environment: DEPLOY_USER, DEPLOY_HOST (required); STAGING_DIR (default
# haiplane-hub-src-staging); SSH_OPTS (e.g. "-i ~/.ssh/key -o BatchMode=yes").
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_SET=""
if [ "${1:-}" = "--build-set" ]; then
  BUILD_SET="${2:?--build-set needs a file}"
  shift 2
fi
TREE="$(cd "${1:-$SELF}" && pwd)"
: "${DEPLOY_USER:?DEPLOY_USER is required}"
: "${DEPLOY_HOST:?DEPLOY_HOST is required}"
STAGING_DIR="${STAGING_DIR:-haiplane-hub-src-staging}"
# shellcheck disable=SC2206
SSH_OPTS_ARR=(${SSH_OPTS:-})

for f in "$TREE/pyproject.toml" "$TREE/uv.lock" "$SELF/deploy/remote-deploy.sh" \
  "$SELF/deploy/review-drain.sh" "$SELF/deploy/predeploy-backup.py"; do
  [ -r "$f" ] || { echo "manual_deploy: $f is missing; nothing was shipped" >&2; exit 1; }
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
OUT="$WORK/tree"
mkdir "$OUT"

cd "$TREE"
uv lock --check
uv export --frozen --no-dev --no-emit-project --format requirements-txt --no-header -o "$WORK/requirements.runtime.txt"
if grep -q '^build = \[' pyproject.toml; then
  [ -z "$BUILD_SET" ] || { echo "manual_deploy: TREE has a build group; --build-set is for older commits only" >&2; exit 1; }
  uv export --frozen --only-group build --no-emit-project --format requirements-txt --no-header -o "$WORK/requirements.build.txt"
else
  [ -n "$BUILD_SET" ] || { echo "manual_deploy: TREE has no build group in uv.lock (a commit before #1620); pass --build-set FILE, see the header of this script; nothing was shipped" >&2; exit 1; }
  [ -s "$BUILD_SET" ] || { echo "manual_deploy: $BUILD_SET is missing or empty; nothing was shipped" >&2; exit 1; }
  cp "$BUILD_SET" "$WORK/requirements.build.txt"
fi

rsync -a --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude '*.pyc' --exclude '.git' ./ "$OUT/"
mkdir -p "$OUT/deploy"
cp "$SELF/deploy/remote-deploy.sh" "$SELF/deploy/review-drain.sh" "$SELF/deploy/predeploy-backup.py" "$OUT/deploy/"
cp "$WORK/requirements.runtime.txt" "$WORK/requirements.build.txt" "$OUT/"
git -C "$TREE" rev-parse HEAD >"$OUT/.deploy-sha" 2>/dev/null || echo manual >"$OUT/.deploy-sha"

rsync -az --delete -e "ssh ${SSH_OPTS:-}" "$OUT/" "$DEPLOY_USER@$DEPLOY_HOST:~/$STAGING_DIR/"
exec ssh ${SSH_OPTS_ARR[@]+"${SSH_OPTS_ARR[@]}"} "$DEPLOY_USER@$DEPLOY_HOST" 'bash -s' <"$SELF/deploy/remote-deploy.sh"
