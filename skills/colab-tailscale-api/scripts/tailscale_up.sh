#!/usr/bin/env bash
# Join a Colab VM to the tailnet and publish a local port to it.
#
# Colab has no TUN device and no CAP_NET_ADMIN (measured 2026-09-17, kernel
# 6.6.122+: /dev/net/tun absent, `modprobe tun` fails, CapEff lacks bit 12), so
# userspace networking is mandatory - this is not a fallback path.
#
# Runs ON the VM:
#   TS_AUTHKEY=tskey-auth-... [TS_SERVICE=svc:my-api] api/tailscale_up.sh [PORT] [HOSTNAME]
#
# The key must be reusable + ephemeral + pre-authorized and tagged
# tag:colab-temp. Ephemeral means the node is reaped after it disconnects, so
# the next VM can reclaim the same MagicDNS hostname.
set -uo pipefail

PORT="${1:-8000}"
HOSTNAME_WANTED="${2:-colab-gpu}"
# accept either spelling; .env in this repo uses TS_AUTH_KEY
TS_AUTHKEY="${TS_AUTHKEY:-${TS_AUTH_KEY:-}}"
: "${TS_AUTHKEY:?set TS_AUTHKEY or TS_AUTH_KEY (reusable+ephemeral+pre-authorized, tag:colab-temp)}"
STATE_DIR=/content/tailscale
mkdir -p "$STATE_DIR"

if ! command -v tailscale >/dev/null 2>&1; then
  echo "== installing tailscale"
  curl -fsSL https://tailscale.com/install.sh | sh >/dev/null 2>&1 || {
    echo "!! install.sh failed, falling back to the static tarball"
    ver=$(curl -fsSL https://pkgs.tailscale.com/stable/?mode=json | python3 -c 'import json,sys;print(json.load(sys.stdin)["TarballsVersion"])')
    curl -fsSL "https://pkgs.tailscale.com/stable/tailscale_${ver}_amd64.tgz" -o /tmp/ts.tgz
    tar -xzf /tmp/ts.tgz -C /tmp
    install -m755 "/tmp/tailscale_${ver}_amd64/tailscale" "/tmp/tailscale_${ver}_amd64/tailscaled" /usr/local/bin/
  }
fi
tailscale version | head -2

# Stale node with the same hostname? Delete it first, or this VM becomes
# colab-gpu-1. Needs a Tailscale API key (TS_API_KEY) + tailnet; optional.
if [ -n "${TS_API_KEY:-}" ] && [ -n "${TS_TAILNET:-}" ]; then
  id=$(curl -fsSL -u "${TS_API_KEY}:" \
        "https://api.tailscale.com/api/v2/tailnet/${TS_TAILNET}/devices" |
       python3 -c "
import json,sys
want='${HOSTNAME_WANTED}'
for d in json.load(sys.stdin).get('devices', []):
    if d.get('hostname') == want or d.get('name','').split('.')[0] == want:
        print(d['nodeId']); break
" 2>/dev/null)
  if [ -n "$id" ]; then
    echo "== deleting stale node $id ($HOSTNAME_WANTED)"
    curl -fsS -X DELETE -u "${TS_API_KEY}:" "https://api.tailscale.com/api/v2/device/${id}" >/dev/null || true
    sleep 3
  fi
fi

# setsid, not just nohup: when this was a plain background child of the job's
# shell it was torn down with the job (tailscaled logged "Client.Shutdown" the
# moment the launching job printed DONE). Its own session survives.
if ! tailscale status >/dev/null 2>&1; then
echo "== starting tailscaled (userspace networking, own session)"
setsid nohup tailscaled \
  --tun=userspace-networking \
  --statedir="$STATE_DIR" \
  --socks5-server=localhost:1055 \
  --outbound-http-proxy-listen=localhost:1055 \
  > "$STATE_DIR/tailscaled.log" 2>&1 < /dev/null &
  disown 2>/dev/null || true
else
  echo "== tailscaled already running"
fi

for _ in $(seq 1 30); do
  tailscale status >/dev/null 2>&1 && break
  sleep 1
done

echo "== joining tailnet as $HOSTNAME_WANTED"
tailscale up \
  --auth-key="${TS_AUTHKEY}" \
  --hostname="$HOSTNAME_WANTED" \
  --advertise-tags="${TS_TAG:-tag:colab-temp}" \
  --accept-dns=false \
  --timeout=90s || { echo "!! tailscale up failed"; tail -20 "$STATE_DIR/tailscaled.log"; exit 1; }

# TS_SERVICE=svc:NAME: host a Tailscale Service instead of a node port. The
# service's name and address belong to the tailnet, not to this node, so they
# stay the same when the next VM takes over. Define it in the admin console
# (Services) with port tcp:80, and auto-approve tag:colab-temp for it in the
# policy file, or each new VM waits for a manual approval.
if [ -n "${TS_SERVICE:-}" ]; then
  echo "== hosting $TS_SERVICE -> 127.0.0.1:$PORT"
  tailscale serve --bg --service="$TS_SERVICE" --http=80 "127.0.0.1:$PORT" ||
    { echo "!! tailscale serve --service failed"; tail -20 "$STATE_DIR/tailscaled.log"; }
else
  echo "== publishing localhost:$PORT to the tailnet"
  tailscale serve --bg "$PORT" || { echo "!! tailscale serve failed (this is the userspace-mode inbound question)"; tail -20 "$STATE_DIR/tailscaled.log"; }
fi

echo "== status"
tailscale status || true
tailscale ip -4 || true
echo "== serve config"
tailscale serve status || true
echo
echo "Reachable from the tailnet at: http://${HOSTNAME_WANTED}:${PORT}/ (MagicDNS)"
echo "or via the node's 100.x address above. ACL must allow the caller -> tag:colab-temp:${PORT}."
