#!/usr/bin/env bash
# Minimal swarm-memory client. Any harness: set SWARM_URL + SWARM_SECRET, call 2 endpoints.
#   swarm.sh remember <user> "<text>"
#   swarm.sh recall   <user> "<question>"
#   swarm.sh graph
set -euo pipefail
: "${SWARM_URL:=http://localhost:8080}"
: "${SWARM_SECRET:?set SWARM_SECRET}"
cmd=${1:?cmd}; shift

sign() {  # $1=user -> prints curl header args
  local user=$1 ts sig
  ts=$(date +%s)
  sig=$(printf '%s.%s' "$user" "$ts" | openssl dgst -sha256 -hmac "$SWARM_SECRET" -r | cut -d' ' -f1)
  printf '%s\n' -H "X-Swarm-User: $user" -H "X-Swarm-Ts: $ts" -H "X-Swarm-Sig: $sig"
}

json() { python3 -c 'import json,sys; print(json.dumps({sys.argv[1]: sys.argv[2]}))' "$1" "$2"; }

case "$cmd" in
  remember)
    user=$1; text=$2; mapfile -t H < <(sign "$user")
    curl -sS -X POST "$SWARM_URL/remember" "${H[@]}" -H 'Content-Type: application/json' -d "$(json text "$text")" ;;
  recall)
    user=$1; q=$2; mapfile -t H < <(sign "$user")
    curl -sS -X POST "$SWARM_URL/recall" "${H[@]}" -H 'Content-Type: application/json' -d "$(json query "$q")" ;;
  graph) curl -sS "$SWARM_URL/graph" ;;
  *) echo "usage: swarm.sh remember|recall <user> <text> | graph" >&2; exit 2 ;;
esac
echo
