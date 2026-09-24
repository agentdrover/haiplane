#!/usr/bin/env bash
# Cold-start smoke of docker-compose.yml (#1358): the one-command start that
# #944/#1336 promise, checked by a machine instead of by hand or by a cloud
# reviewer that has no Docker daemon.
#
#   deploy/docker/compose_smoke.sh
#
# Two starts, each on a FRESH named volume (never ./data — running this in a
# checkout must not touch the reader's own hub database):
#   1. compose as shipped (demo seed on): /healthz answers, project "demo"
#      exists. The positive half is what makes the negative one mean anything:
#      a query that cannot see "demo" would pass run 2 for free.
#   2. HAIPLANE_DEMO_SEED=0: /healthz answers, project "demo" does not exist.
# Everything is torn down with `down -v` on any exit; on failure the container
# logs are printed first.
set -euo pipefail

cd "$(dirname "$0")/../.."

PROJECT="haiplane-smoke"
URL="http://localhost:8080/healthz"
WAIT_SECONDS="${SMOKE_WAIT_SECONDS:-120}"

override_dir="$(mktemp -d)"
# Same target path, so this entry REPLACES the ./data bind mount.
cat >"$override_dir/volume.yml" <<'YAML'
services:
  hub:
    volumes:
      - smoke-data:/data
volumes:
  smoke-data: {}
YAML
cat >"$override_dir/no-seed.yml" <<'YAML'
services:
  hub:
    environment:
      HAIPLANE_DEMO_SEED: "0"
YAML

compose() {
    docker compose -p "$PROJECT" -f docker-compose.yml "$@"
}

phase="start"
cleanup() {
    rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "::error::compose smoke failed during: $phase"
        compose -f "$override_dir/volume.yml" logs --no-color || true
    fi
    compose -f "$override_dir/volume.yml" down -v --remove-orphans || true
    rm -rf "$override_dir"
    exit "$rc"
}
trap cleanup EXIT

wait_healthz() {
    local deadline=$((SECONDS + WAIT_SECONDS))
    until curl -fsS "$URL" >/dev/null 2>&1; do
        if [ "$SECONDS" -ge "$deadline" ]; then
            echo "healthz did not answer within ${WAIT_SECONDS}s"
            return 1
        fi
        sleep 2
    done
    echo "healthz: $(curl -fsS "$URL")"
}

demo_projects() {
    compose "$@" exec -T hub python -c \
        "import sqlite3; print(sqlite3.connect('/data/hub.db').execute(\"SELECT count(*) FROM projects WHERE slug='demo'\").fetchone()[0])"
}

phase="run 1: compose up --build (demo seed on)"
echo "== $phase"
compose -f "$override_dir/volume.yml" up -d --build
wait_healthz
count="$(demo_projects -f "$override_dir/volume.yml")"
if [ "$count" != "1" ]; then
    echo "expected the demo project with the seed on, found $count"
    exit 1
fi
echo "demo project present: $count"

phase="teardown between runs"
compose -f "$override_dir/volume.yml" down -v

phase="run 2: empty database, HAIPLANE_DEMO_SEED=0"
echo "== $phase"
compose -f "$override_dir/volume.yml" -f "$override_dir/no-seed.yml" up -d
wait_healthz
count="$(demo_projects -f "$override_dir/volume.yml" -f "$override_dir/no-seed.yml")"
if [ "$count" != "0" ]; then
    echo "expected no demo project with the seed off, found $count"
    exit 1
fi
echo "demo project absent: $count"

phase="done"
echo "compose smoke: ok"
