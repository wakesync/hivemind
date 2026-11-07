# swarm-memory: 2-minute demo

**Setup:** terminal on the left, `graph.html` (deployed instance URL) on the right, empty memory. `export SWARM_URL=... SWARM_SECRET=...`

**0:00 to 0:15, the problem**
"Agents are going multiplayer. My agent, your agent and your coworker's agent all know pieces of the same plan. Today they share memory one of two ways: dump everything into one vector DB that the operator can read, or don't share at all. We built a third option."

**0:15 to 0:30, the claim**
Point at the badge in the top-left: ENCLAVE ATTESTED.
"This is swarm-memory running in a Tinfoil enclave. The memory API and a Qwen 27B model run together on one H200 in confidential mode. We built it, and we can't read it."

**0:30 to 1:00, four agents write**
Run `demo/seed.sh --slow`. As the notes land, nodes heat up on the graph.
"Four agents, each working for a different person. A founder's agent knows the budget and that the venue flooded. An engineer's agent knows he's busy until 2. The ops agent booked a backup loft. The designer's agent knows none of this."
Point at the graph: "This is all the operator gets to see. Topics, how hot they are, no names. HIDDEN 29 means topics only one person touched never show up at all."

**1:00 to 1:30, the payoff**
The designer's agent (dov) asks: *"When and where should the offsite happen, and does it fit the budget including food?"*
Read the answer out loud: Thursday after 2, Greenpoint loft, $2500 + $900 vegan catering = $3400, under the $4000 cap.
"That answer needed three different people's private notes. dov got the answer and `contributors: 3`. It never got the notes, never learned who said what, and nothing left the enclave to produce it. The LLM call stays in the same enclave."

**1:30 to 1:50, plugging in**
Show `clients/` quickly: "Any harness. Two HTTP calls, an OpenAI tool schema, or an MCP server. Claude Code is one `claude mcp add` away."
Optionally, a Claude Code recall over MCP.

**1:50 to 2:00, close**
Show the `tinfoil-config.yml` in the public repo: image digest + model root hash.
"Every byte of what's running is pinned in a public config and checked by attestation. Shared memory for agents, with no one in the middle."

*(Track 01: the bottom-right panel is the revenue ledger. It reads Stripe charges only, aggregates only.)*
