#!/usr/bin/env bash
# Starts an already prepared agent scenario after its clean VM snapshot is restored.
set -euo pipefail
cd /workspace
systemctl start docker
docker compose -f vm/agent-compose.yml start
bash /workspace/vm/start-api-proxy.sh
for attempt in $(seq 1 60); do
  if docker compose -f vm/agent-compose.yml exec -T postgres pg_isready -U lab -d lab >/dev/null 2>&1 &&
     curl -fsS --max-time 2 http://127.0.0.1:8080/healthz >/dev/null &&
     docker compose -f vm/agent-compose.yml exec -T sandbox sh -lc 'id' >/dev/null 2>&1; then
    printf '[lab] prepared agent scenario ready: %ss\n' "$SECONDS"
    exit 0
  fi
  sleep 1
done
docker compose -f vm/agent-compose.yml ps >&2
exit 1
