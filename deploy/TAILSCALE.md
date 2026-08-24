# Multi-user Hub on a fresh VM via Tailscale

Operations guide for running Haiplane Hub for a small team. The combination is:

1. **Tailscale** for the network — every developer's machine and the VM join the same private tailnet, no public ports, no certificates.
2. **Bearer-token auth** built into the Hub (`HAIPLANE_HUB_TOKENS`) — every request is attributed to a real user.
3. **Streamable-HTTP MCP** mounted at `/mcp` — Cursor connects via `https://hub.tailnet/mcp` (or `http://` over Tailscale) using the same token.

This guide assumes Ubuntu 22.04 / 24.04. Other distros are similar.

---

## 1. Prepare the VM

```bash
sudo apt update && sudo apt install -y python3.11 python3.11-venv git curl
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Create the runtime user (avoid running as `root`):

```bash
sudo useradd -m -s /bin/bash haiplane
sudo -u haiplane -i
```

## 2. Tailscale

On the VM:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh --hostname=haiplane-hub
```

Approve the device in the [Tailscale admin console](https://login.tailscale.com/admin/machines).
Note the assigned MagicDNS name (e.g. `haiplane-hub.tailXXXX.ts.net`) — that is the URL the team will use.

On every developer machine: install Tailscale, sign in to the same tailnet,
verify reachability:

```bash
ping haiplane-hub
```

> No firewall ports need to be opened on the VM — Tailscale uses NAT
> traversal. The Hub binds to `0.0.0.0:8080` but is only reachable from
> the tailnet because no public route exists.

## 3. Install the Hub

```bash
git clone git@github.com:agentdrover/haiplane.git ~/haiplane-hub
cd ~/haiplane-hub
uv sync
```

## 4. Issue tokens

Pick a random token per developer (`openssl rand -hex 24`), build the env:

```bash
cat > ~/.haiplane-hub.env <<'EOF'
HAIPLANE_HUB_TOKENS=denis:dXX...,alice:aYY...,bob:bZZ...
HAIPLANE_HUB_ALLOWED_HOSTS=haiplane-hub:8080,haiplane-hub.tailXXXX.ts.net
HAIPLANE_HUB_DB=/home/haiplane/hub.db
HAIPLANE_HUB_PORT=8080
EOF
chmod 600 ~/.haiplane-hub.env
```

Every `HAIPLANE_*` key above is also accepted under the canonical
`HAIPLANE_*` prefix (e.g. `HAIPLANE_HUB_DB`); the hub reads the new name
first and falls back to the legacy one.

Tokens are environment-driven on purpose for the MVP — to add or revoke a
user, edit the file and restart the service. A future task will move this
into the database.

`HAIPLANE_HUB_ALLOWED_HOSTS` is the Host header allowlist. Include every name
developers will use for the Hub, with ports when they must match exactly.

## 5. systemd service

```ini
# /etc/systemd/system/haiplane-hub.service
[Unit]
Description=Haiplane Hub
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=haiplane
WorkingDirectory=/home/haiplane/haiplane-hub
EnvironmentFile=/home/haiplane/.haiplane-hub.env
ExecStart=/home/haiplane/haiplane-hub/.venv/bin/haiplane-hub
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now haiplane-hub
sudo systemctl status haiplane-hub
journalctl -u haiplane-hub -f
```

Look for `Hub auth ENABLED (N token(s) configured)` in the log — that
confirms the env was picked up and the gate is active.

## 6. Verify

From a developer machine:

```bash
TOKEN=dXX...
HUB=http://haiplane-hub:8080

# Public probe — must work without a token
curl -fsS $HUB/healthz

# Protected — must 401 without a token
curl -i $HUB/api/tasks | head -1   # → HTTP/1.1 401 Unauthorized

# Protected — must 200 with the token
curl -fsS -H "Authorization: Bearer $TOKEN" $HUB/api/tasks | head
```

In the browser, visit `http://haiplane-hub:8080/`. You will be bounced to
`/login`; paste the same token, and the dashboard opens.

## 7. Connect Cursor (MCP over Streamable-HTTP)

In each developer's `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "haiplane-hub": {
      "url": "http://haiplane-hub:8080/mcp",
      "headers": {
        "Authorization": "Bearer dXX..."
      }
    }
  }
}
```

Cursor will negotiate Streamable-HTTP transport against the mounted `/mcp`
endpoint. Multiple developers connect concurrently; each request is attributed
to their token via `request.state.user`.

> The previous stdio-over-SSH transport keeps working for emergency
> single-user access. Both can coexist.

## 8. Day-2 operations

| Task | Command |
|------|---------|
| Add / rotate a token | edit `~/.haiplane-hub.env`, then `sudo systemctl restart haiplane-hub` |
| Tail logs | `journalctl -u haiplane-hub -f` |
| Health check | `curl http://haiplane-hub:8080/healthz` |
| Backup DB | `cp /home/haiplane/hub.db /home/haiplane/hub.db.$(date +%F)` |
| Disable auth temporarily | set `HAIPLANE_HUB_AUTH_DISABLED=1`, restart — never leave on |
| Remove a developer | drop their pair from `HAIPLANE_HUB_TOKENS`, restart, ask them to clear cookies |

## 9. When to graduate to production-grade auth

Move tokens from env to the database (`users` table + CLI + roles + UI)
when one of these is true:

- A new developer needs read-only access and you cannot express that with
  the current "everyone authenticated == full access" model.
- A token has leaked and you want a per-user revocation audit trail.
- The team has > 5 active developers and rotating env vars is friction.
- An external auditor wants centralised user management.

Until then, env-driven tokens are simpler, safer (no DB attack surface
for credentials) and equally functional.
