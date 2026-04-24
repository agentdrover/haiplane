#!/usr/bin/env bash
set -euo pipefail

# Deploy OpenClaw Hub to the Pi.
# Run from the hub/ directory on the Pi after copying/syncing.

INSTALL_DIR="$HOME/services/openclaw-hub"
VENV="$INSTALL_DIR/.venv"

echo "==> Installing to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp -r hub pyproject.toml "$INSTALL_DIR/"
if [ -f openclaw-hub.service ]; then
    cp openclaw-hub.service "$INSTALL_DIR/"
fi

echo "==> Setting up venv"
if [ ! -d "$VENV" ]; then
    uv venv "$VENV"
fi

echo "==> Installing dependencies"
uv pip install -e "$INSTALL_DIR" --python "$VENV/bin/python"

echo "==> Installing CLI links"
mkdir -p "$HOME/.local/bin"
ln -sf "$VENV/bin/oc-hub" "$HOME/.local/bin/oc-hub"
ln -sf "$VENV/bin/openclaw-hub" "$HOME/.local/bin/openclaw-hub"
ln -sf "$VENV/bin/openclaw-hub-mcp" "$HOME/.local/bin/openclaw-hub-mcp"

echo "==> Installing systemd service"
sudo cp "$INSTALL_DIR/openclaw-hub.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable openclaw-hub.service
sudo systemctl restart openclaw-hub.service

echo "==> Waiting for startup..."
sleep 3
if systemctl is-active --quiet openclaw-hub.service; then
    echo "==> openclaw-hub is running on port 8080"
    echo "    Dashboard: http://$(hostname -I | awk '{print $1}'):8080"
else
    echo "==> ERROR: service failed to start"
    journalctl -u openclaw-hub.service --no-pager -n 20
    exit 1
fi
