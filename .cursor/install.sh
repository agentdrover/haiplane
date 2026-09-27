#!/usr/bin/env bash
# Среда облачного исполнителя Cursor (#1441, F3): uv и зависимости до начала
# работы агента. Cursor запускает это из environment.json («install») при
# сборке среды. В образе uv нет (наблюдено в F0 #1273: Python 3.12.3, uv
# ставился скриптом, uv sync — 87 пакетов). Секретов здесь нет и быть не должно.
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

uv --version
uv sync --frozen
