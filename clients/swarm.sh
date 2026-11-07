#!/usr/bin/env bash
# Minimal hivemind client. Set SWARM_URL + SWARM_SECRET.
#   swarm.sh remember <user> "<text>" [org]     # no org = personal (author-only)
#   swarm.sh recall   <user> "<question>"
#   swarm.sh org-create <user> <org-id> "<name>" [sealed|signal|open-derived]
#   swarm.sh org-add    <admin> <org-id> <user> [member|admin]
#   swarm.sh wall       <admin> <org-id> sealed|signal|open-derived
#   swarm.sh orgs       <user>
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

# json k1 v1 [k2 v2 ...]  (empty values dropped)
json() { python3 -c 'import json,sys; a=sys.argv[1:]; print(json.dumps({a[i]:a[i+1] for i in range(0,len(a),2) if a[i+1]}))' "$@"; }

post() {  # user path body
  local user=$1 path=$2 body=$3; mapfile -t H < <(sign "$user")
  curl -sS -X POST "$SWARM_URL$path" "${H[@]}" -H 'Content-Type: application/json' -d "$body"
}

case "$cmd" in
  remember)   post "$1" /remember "$(json text "$2" org "${3:-}")" ;;
  recall)     post "$1" /recall "$(json query "$2")" ;;
  org-create) post "$1" /orgs "$(json id "$2" name "$3" wall "${4:-sealed}")" ;;
  org-add)    post "$1" "/orgs/$2/members" "$(json user "$3" role "${4:-member}")" ;;
  wall)       post "$1" "/orgs/$2/wall" "$(json wall "$3")" ;;
  orgs)       mapfile -t H < <(sign "$1"); curl -sS "$SWARM_URL/orgs" "${H[@]}" ;;
  graph)      curl -sS "$SWARM_URL/graph" ;;
  *) echo "usage: swarm.sh remember|recall|org-create|org-add|wall|orgs|graph ..." >&2; exit 2 ;;
esac
echo
