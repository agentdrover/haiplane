#!/usr/bin/env bash
set -euo pipefail

# Deploy Haiplane Hub to the Pi.
# Run from the hub/ directory on the Pi after copying/syncing.

INSTALL_DIR="$HOME/services/haiplane-hub"
VENV="$INSTALL_DIR/.venv"

echo "==> Installing to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp -r hub pyproject.toml "$INSTALL_DIR/"
# The unit installs as haiplane-hub.service. The repository still ships the
# unit file under its pre-rename name, so fall back to it and rename on copy.
if [ -f haiplane-hub.service ]; then
    cp haiplane-hub.service "$INSTALL_DIR/"
elif [ -f openclaw-hub.service ]; then
    cp openclaw-hub.service "$INSTALL_DIR/haiplane-hub.service"
fi

echo "==> Setting up venv"
if [ ! -d "$VENV" ]; then
    uv venv "$VENV"
fi

echo "==> Installing dependencies"
# Order matters: uninstall the old distribution BEFORE installing the new one.
# The reverse can delete shared console-script names from the old package
# RECORD, removing the aliases the new distribution just installed.
uv pip uninstall openclaw-hub --python "$VENV/bin/python" 2>/dev/null || true
uv pip install -e "$INSTALL_DIR" --python "$VENV/bin/python"

echo "==> Installing CLI links"
mkdir -p "$HOME/.local/bin"
ln -sf "$VENV/bin/hp-hub" "$HOME/.local/bin/hp-hub"
ln -sf "$VENV/bin/haiplane-hub" "$HOME/.local/bin/haiplane-hub"
ln -sf "$VENV/bin/haiplane-hub-mcp" "$HOME/.local/bin/haiplane-hub-mcp"
# Legacy aliases stay until Wave 5 drops them from pyproject.
ln -sf "$VENV/bin/oc-hub" "$HOME/.local/bin/oc-hub"
ln -sf "$VENV/bin/openclaw-hub" "$HOME/.local/bin/openclaw-hub"
ln -sf "$VENV/bin/openclaw-hub-mcp" "$HOME/.local/bin/openclaw-hub-mcp"

echo "==> Installing systemd service"
sudo cp "$INSTALL_DIR/haiplane-hub.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable haiplane-hub.service
sudo systemctl restart haiplane-hub.service

echo "==> Waiting for startup..."
sleep 3
if systemctl is-active --quiet haiplane-hub.service; then
    echo "==> haiplane-hub is running on port 8080"
    echo "    Dashboard: http://$(hostname -I | awk '{print $1}'):8080"
else
    echo "==> ERROR: service failed to start"
    journalctl -u haiplane-hub.service --no-pager -n 20
    exit 1
fi
