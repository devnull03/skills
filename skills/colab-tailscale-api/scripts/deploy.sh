#!/usr/bin/env bash
# Put a server running on a Colab VM behind a Tailscale Service, from the laptop.
# Run from the project's repo root (run-colab's driver pushes repo-relative files).
#
#   deploy.sh up SESSION SERVICE PORT "SERVER CMD" [FILE...]
#       e.g. deploy.sh up myapi svc:my-api 8000 "python -u api/server.py" api/server.py .env
#   deploy.sh ready  SESSION PORT        # block until GET /healthz says ready (<= 20 min)
#   deploy.sh expose SESSION SERVICE PORT
#   deploy.sh check  URL                 # from this machine, over the tailnet
#   deploy.sh down   SESSION
#
# `up` = new VM (or reuse) -> push FILEs + tailscale_up.sh -> start server
# detached -> ready -> expose. .env must hold TS_AUTH_KEY (reusable, ephemeral,
# pre-authorized, tagged $TS_TAG, default tag:colab-temp).
set -euo pipefail
D="$HOME/.claude/skills/run-colab/driver.py"
HERE="$(cd "$(dirname "$0")" && pwd)"
drv() { python3 "$D" "$@"; }
REMOTE="/content/$(basename "$(git rev-parse --show-toplevel)")"
cmd="${1:-}"; shift || true

ready() {  # SESSION PORT
  drv start "$1" ready -- bash -lc "sleep 3; for i in \$(seq 1 120); do s=\$(curl -s localhost:$2/healthz); echo \"\$(date +%T) \$s\"; case \"\$s\" in *'\"ready\"'*) break;; *'\"status\": \"error\"'*) echo BOOTFAIL; break;; esac; sleep 10; done; echo READY DONE"
  drv watch "$1" ready --interval 30 --max-min 21 --stall 60 --done 'READY DONE' | tail -3
}
expose() {  # SESSION SERVICE PORT
  drv start "$1" ts --cwd "$REMOTE" -- bash -lc \
    "set -a; . ./.env; set +a; TS_SERVICE=$2 bash .tailscale_up.sh $3 ${TS_HOSTNAME:-colab-$1}; echo TS DONE"
  drv watch "$1" ts --interval 15 --max-min 6 --done 'TS DONE' |
    grep -E '^(==|!!|http|This machine|backend error|Serve started)' || true
}

case "$cmd" in
up)
  S="$1" SVC="$2" PORT="$3" RUN="$4"; shift 4
  grep -q '^TS_AUTH_KEY=' .env || { echo "!! no TS_AUTH_KEY in .env"; exit 1; }
  drv sessions | grep -q "^$S " && echo "== reusing $S" || drv new "$S"
  cp "$HERE/tailscale_up.sh" .tailscale_up.sh
  drv push "$S" .tailscale_up.sh .env "$@"; rm -f .tailscale_up.sh
  drv start "$S" server --cwd "$REMOTE" -- bash -lc "$RUN; echo SERVER EXITED"
  ready "$S" "$PORT"
  expose "$S" "$SVC" "$PORT"
  ;;
ready)  ready "$1" "$2" ;;
expose) expose "$1" "$2" "$3" ;;
check)  curl -s -m 10 -w ' [%{http_code}]\n' "$1/healthz" || echo "unreachable: $1" ;;
down)   drv stop "$1" ;;
*) sed -n '2,15p' "$0"; exit 2 ;;
esac
