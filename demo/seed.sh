#!/usr/bin/env bash
# 4 agents, each working for a different person, write private notes into the
# shared memory. Then one agent asks a question whose answer needs 3 of them.
# Nobody (including the operator) can read another agent's raw notes.
#   SWARM_URL=http://localhost:8080 SWARM_SECRET=... demo/seed.sh [--slow]
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
c="$here/../clients/swarm.sh"
slow=${1:-}
pause() { [ "$slow" = "--slow" ] && sleep 1.5 || true; }

say() { printf '\n\033[38;5;214m▸ %s\033[0m\n' "$*"; }

say "agent:ada (works for a founder) remembers"
"$c" remember ada "The offsite for the agents team is moving to Thursday because the venue at the Navy Yard flooded. Budget is capped at 4000 dollars." ; pause
"$c" remember ada "I prefer vegetarian catering for the team offsite, two people on the team are vegan." ; pause

say "agent:bix (works for an engineer) remembers"
"$c" remember bix "Thursday I can only do the offsite after 2pm, I have the GPU cluster migration in the morning." ; pause
"$c" remember bix "The inference cluster migration to H200s should finish Thursday noon. Rollback plan is in the runbook." ; pause

say "agent:cyd (works for ops) remembers"
"$c" remember cyd "Booked backup venue for the offsite: Greenpoint loft, holds 20 people, costs 2500 dollars, available Thursday from 1pm." ; pause
"$c" remember cyd "Vendor for catering quoted 900 dollars for a vegan menu for 15 people." ; pause

say "agent:dov (works for a designer) remembers"
"$c" remember dov "Working on the new brand palette, ironbow thermal gradients for the hackathon site." ; pause
"$c" remember dov "Need GPU budget approval for the image model fine tune, about 600 dollars." ; pause

say "agent:dov asks the swarm (it never saw ada/bix/cyd's notes)"
"$c" recall dov "When and where should the team offsite happen, and does it fit the budget including food?"

say "graph (topics only, no names, topics with <k contributors hidden)"
"$c" graph | python3 -c 'import json,sys; g=json.load(sys.stdin); print("nodes:", [n["id"] for n in g["nodes"]][:20]); print("links:", len(g["links"]), "hidden_topics:", g["hidden_topics"], "members:", g["members"])'
