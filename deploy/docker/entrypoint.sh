#!/bin/sh
# Container entrypoint (#944): opt-in demo seed, then the hub itself.
# HAIPLANE_DEMO_SEED=1 fills an EMPTY database with a sample project;
# the seed is idempotent and never touches an already-seeded database.
set -e

if [ "${HAIPLANE_DEMO_SEED:-0}" = "1" ]; then
    python /app/scripts/demo_seed.py
fi

exec haiplane-hub
