#!/usr/bin/env bash
# Local dev container (host network, loopback only, in-memory /data).
#   demo/run-local.sh <name> <port>      env: SWARM_SECRET, LLM_BASE_URL, LLM_MODEL, LLM_API_KEY
set -euo pipefail
name=${1:-sm-dev}; port=${2:-18090}
docker rm -f "$name" >/dev/null 2>&1 || true
docker run -d --name "$name" --network host --read-only --tmpfs /data:uid=10001 --tmpfs /tmp \
  -e SWARM_SECRET="${SWARM_SECRET:?}" -e LLM_BASE_URL="${LLM_BASE_URL:-}" -e LLM_MODEL="${LLM_MODEL:-qwen3.8-27b}" \
  -e LLM_API_KEY="${LLM_API_KEY:-}" -e LLM_TEMPERATURE="${LLM_TEMPERATURE-}" \
  hivemind:dev uvicorn app.main:app --host 127.0.0.1 --port "$port" >/dev/null
for _ in $(seq 60); do curl -sf "http://127.0.0.1:$port/healthz" >/dev/null && break; sleep 1; done
curl -s "http://127.0.0.1:$port/healthz"; echo
