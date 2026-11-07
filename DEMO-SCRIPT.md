# hivemind: 2-minute demo

**Setup (before recording):**
```bash
cd localhost-swarm-memory && . .local/env.sh
demo/run-local.sh sm-dev 18090                        # fresh, empty memory
export SWARM_URL=http://127.0.0.1:18090
SWARM_URL=$SWARM_URL demo/seed.sh --seed-only         # or run the full thing live with --slow
```
Terminal on the left, `http://127.0.0.1:18090/` on the right. Font size up. If you seeded already, run the steps with `demo/seed.sh --steps-only`. To re-record, reset with `clients/swarm.sh wall mara ledger-fund signal`.

---

**0:00 to 0:15, the problem**
"Real people live in overlapping orgs. Mara is a partner at a fund and sits on a co-op board. Rook cofounded a startup and runs that same co-op. Their agents hold context for all of it. Today you either silo that context or pour it into one shared vector DB that the operator and every tenant can read."

**0:15 to 0:30, the product**
Point at the graph: three colored clusters, one per org, a wall pill next to each name, no people anywhere.
"This is hivemind, a sealed brain for people who live across overlapping orgs. Each org sets its own wall: sealed, signal, or open-derived. One query covers every org you're in, and the walls are enforced inside an enclave."

**0:30 to 0:55, (a) the startup asks**
Run step (a). Teo at Tinplate asks: *who could fund a 250K rooftop pilot, on what terms, and where could it run?*
Read the answer: just what Tinplate already knows (unit specs, 30A circuit). Then point at the red line:
`⚑ signal: 1 org in your network has context on this. request access.`
"Teo doesn't learn which org, or what it knows. Just that someone two hops away has the answer. That's the signal wall."

**0:55 to 1:20, (b) the conduit asks**
Run step (b). Mara, in the fund and the co-op, asks the same question.
Read the answer: 250K SAFE at a 6M cap if there's a signed site, a co-op rooftop with 40 kW solar for an 8% revenue share, and the board signs pilot letters on the first Monday. Point at `member_orgs=2`.
"That answer took two orgs' private notes, synthesized in the enclave. Nobody wrote it down in one place."

**1:20 to 1:40, (c) the fund opens up**
Run step (c). On the graph, the Ledger Fund pill flashes `SIGNAL → OPEN-DERIVED` and its topic words un-redact.
Teo asks again and now gets the fund's terms as a derived answer (`derived_orgs=1`).
"Derived, never raw. A copy guard blocks any six-word run from the fund's notes. And the co-op is still sealed, so Teo still doesn't learn about the rooftop. That's Mara's call to make, as a co-op member."

**1:40 to 1:55, the operator sees nothing**
Point at the graph: the sealed orgs show `█ #A9EFE5`-style hashes, not words. No names. HIDDEN 24 means topics under k=2 contributors never render. Personal memory never shows up at all.
"This is everything we, the operator, can see. The notes, the embeddings and the model prompt only exist in enclave memory. To the host it's ciphertext." Point at the badge top-left: on the Tinfoil deploy it reads `ENCLAVE ATTESTED · CVM … · <sha>` (locally it reads DEV MODE).
"The wall logic is part of the attested code. Your org can verify that sealed means sealed."

**1:55 to 2:00, close**
Flash `tests/leak_test.py` ending in `ALL PASS`: "Canaries in every org, six extraction prompts per wall state, zero leaks. Bottom-right panel: $5 per org per day."

---

**If something breaks live:** answers come from the LLM, so wording varies between runs. The counts and the hint line are deterministic. If the LLM is down, recall falls back to extractive (member-visible notes only) and (c) prints "a derived answer exists but the in-enclave model is unavailable". That's also the correct behavior.
