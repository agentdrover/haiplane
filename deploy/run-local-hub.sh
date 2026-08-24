#!/usr/bin/env bash
# Локальный Haiplane Hub на ноутбуке (127.0.0.1:8080).
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

# Новый префикс окружения первым, легаси — как фолбэк (Wave 4).
DB_PATH="${HAIPLANE_HUB_DB:-${OPENCLAW_HUB_DB:-$ROOT/.local/state/hub.db}}"
mkdir -p "$(dirname "$DB_PATH")"

if [[ ! -f "$DB_PATH" ]]; then
  echo "База не найдена: $DB_PATH"
  echo "Создайте пустую (hub создаст схему при старте) или скопируйте с сервера:"
  echo "  mkdir -p $(dirname "$DB_PATH")"
  echo "  ssh <DEPLOY_USER>@<DEPLOY_HOST> 'sudo cat /var/lib/openclaw-hub/hub.db' > \"$DB_PATH\""
  exit 1
fi

echo "Haiplane Hub → http://${HAIPLANE_HUB_HOST:-${OPENCLAW_HUB_HOST:-127.0.0.1}}:${HAIPLANE_HUB_PORT:-${OPENCLAW_HUB_PORT:-8080}}/"
echo "DB: $DB_PATH"
exec uv run haiplane-hub
