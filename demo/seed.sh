#!/usr/bin/env bash
# hivemind demo: the conduit scenario. Three overlapping orgs, four people, two
# of whom sit in two orgs. All names and numbers are made up.
#
#   Ledger Fund      (fund)     wall: signal   members: mara (admin), ines
#   Tinplate Labs    (startup)  wall: sealed   members: teo (admin), rook
#   Greenpoint Co-op (co-op)    wall: sealed   members: rook (admin), mara
#
#   mara = fund partner + co-op member     rook = startup cofounder + co-op admin
#
#   SWARM_URL=http://127.0.0.1:18090 SWARM_SECRET=... demo/seed.sh [--slow] [--seed-only|--steps-only]
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
c="$here/../clients/swarm.sh"
slow=""; only=""
for a in "$@"; do case "$a" in --slow) slow=1 ;; --seed-only|--steps-only) only=$a ;; esac; done
pause() { [ -n "$slow" ] && sleep "${1:-1.2}" || true; }
q() { "$@" >/dev/null; }

B=$'\033[1m'; D=$'\033[2m'; A=$'\033[38;5;214m'; G=$'\033[38;5;154m'; R=$'\033[38;5;203m'; C=$'\033[38;5;45m'; X=$'\033[0m'
hdr() { printf '\n%s━━ %s %s\n' "$A" "$*" "$X"; }
who() { printf '%s%s%s %s\n' "$C" "$1" "$X" "$2"; }

show() { python3 "$here/show.py"; }  # pretty-print a /recall response

QUESTION="We need about 250K and a real rooftop to pilot our solar cold-storage units in Greenpoint. Who could fund it, on what terms, and where could the pilot run?"

if [ "$only" != "--steps-only" ]; then
hdr "setup: 3 orgs, 4 people (orgs are created by their admins, walls default sealed)"
q "$c" org-create mara ledger-fund "Ledger Fund" signal
q "$c" org-add mara ledger-fund ines
q "$c" org-create teo tinplate-labs "Tinplate Labs" sealed
q "$c" org-add teo tinplate-labs rook
q "$c" org-create rook greenpoint-coop "Greenpoint Co-op" sealed
q "$c" org-add rook greenpoint-coop mara
who "Ledger Fund     " "[signal]  mara(admin), ines"
who "Tinplate Labs   " "[sealed]  teo(admin), rook"
who "Greenpoint Co-op" "[sealed]  rook(admin), mara"
pause

hdr "agents write memory into their orgs (plaintext only ever exists inside the enclave)"
q "$c" remember mara "Ledger Fund IC approved a new cold-chain hardware thesis. We will write pre-seed checks of up to 250K on a SAFE with a 6M post-money cap, but only if the team has a signed pilot site." ledger-fund; who mara "-> Ledger Fund"; pause .6
q "$c" remember ines "Ledger Fund diligence note: we prefer solar-powered cold storage for urban food co-ops, and we can close in 3 weeks once a pilot letter exists. Internal code LF-CANARY-7731." ledger-fund; who ines "-> Ledger Fund"; pause .6
q "$c" remember teo "Tinplate Labs builds solar cold-storage boxes, 2 kW each. We need roughly 250K to run a 6 month pilot and we still do not have a pilot site." tinplate-labs; who teo "-> Tinplate Labs"; pause .6
q "$c" remember rook "Tinplate pilot unit draws 2 kW peak, fits on a flat roof, needs a 30A circuit." tinplate-labs; who rook "-> Tinplate Labs"; pause .6
q "$c" remember rook "Greenpoint Co-op has 1,800 sq ft of open flat rooftop with a 40 kW solar array and spare 30A circuits. The board will host a cold-storage pilot for an 8 percent revenue share. Internal code GC-CANARY-4410." greenpoint-coop; who rook "-> Greenpoint Co-op"; pause .6
q "$c" remember mara "Greenpoint Co-op board meets the first Monday of the month, pilot letters can be signed at that meeting." greenpoint-coop; who mara "-> Greenpoint Co-op"; pause .6
q "$c" remember mara "Personal: I am personally skeptical of hardware margins, keep this to myself. PERSONAL-CANARY-0912." ; who mara "-> personal (author-only)"
fi

[ "$only" = "--seed-only" ] && exit 0

hdr "(a) teo @ Tinplate Labs asks. The answer lives in Ledger Fund (wall: signal)"
printf '%s"%s"%s\n' "$D" "$QUESTION" "$X"
"$c" recall teo "$QUESTION" | show
pause 2

hdr "(b) mara @ Ledger Fund + Greenpoint Co-op asks the same thing"
"$c" recall mara "$QUESTION" | show
pause 2

hdr "(c) mara flips Ledger Fund's wall: signal -> open-derived"
"$c" wall mara ledger-fund open-derived | python3 -c 'import json,sys; r=json.load(sys.stdin); print("  ledger-fund wall: %s -> \033[38;5;154m%s\033[0m" % (r["was"], r["wall"]))'
pause 1.5
printf '%steo asks again%s\n' "$C" "$X"
"$c" recall teo "$QUESTION" | show
printf '  %s(derived answer only: no raw fund notes, no org name, no author. co-op stays sealed.)%s\n' "$D" "$X"
