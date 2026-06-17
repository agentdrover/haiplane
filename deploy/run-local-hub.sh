#!/usr/bin/env bash
# Локальный OpenClaw Hub на ноутбуке (127.0.0.1:8080).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/.env.local" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env.local"
  set +a
elif [[ -f "$ROOT/deploy/local-hub.env.example" ]]; then
  echo "Подсказка: скопируйте deploy/local-hub.env.example → .env.local"
fi

DB_PATH="${OPENCLAW_HUB_DB:-$ROOT/.local/state/hub.db}"
mkdir -p "$(dirname "$DB_PATH")"

if [[ ! -f "$DB_PATH" ]]; then
  echo "База не найдена: $DB_PATH"
  echo "Создайте пустую (hub создаст схему при старте) или скопируйте с agenthai.ru:"
  echo "  mkdir -p $(dirname "$DB_PATH")"
  echo "  ssh user1@194.113.34.33 'sudo cat /var/lib/openclaw-hub/hub.db' > \"$DB_PATH\""
  exit 1
fi

echo "OpenClaw Hub → http://${OPENCLAW_HUB_HOST:-127.0.0.1}:${OPENCLAW_HUB_PORT:-8080}/"
echo "DB: $DB_PATH"
exec uv run openclaw-hub
