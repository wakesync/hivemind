# Membrane

**Rent the brain, own the soul. Sovereign memory for agents.**

Every agent you run is learning your life: who you talk to, what you agreed to, what you're building. Today that memory sits in plaintext on someone else's server and gets pasted into every prompt you send. Membrane moves it into a hardware enclave on an NVIDIA H200 that nobody can read, not the cloud, not Tinfoil, not us. Then it lets any model be the brain for a task, and that model only ever sees the slice you choose.

Sol, Muse, Grok, Hermes and OCPlatform plug into one swarm memory. Each keeps its own private context. They share what the walls allow, cooperate across orgs without handing over raw notes, and every answer carries a receipt of who contributed what.

![Membrane: rent the brain, own the soul](demo/motion-v2/hero.gif)

**Live:** <https://membrane-agent.pages.dev> · **Demo:** <https://membrane-agent.pages.dev/demo> · **Enclave:** `membrane.shadow-labs.containers.tinfoil.dev` · **Call it:** [docs/QUICKSTART.md](docs/QUICKSTART.md) · LOCALHOST NYC, 2026-11-07

## The idea in one line

Your **soul** (memory, permissions, walls) lives in an attested enclave you can verify. Your **brain** (Qwen in the enclave, a Darkbloom Mac, or a frontier API) is rented per task and sees only what the soul releases.

## How it works

```
  Sol     Muse     Grok     Hermes     OpenClaw        any harness: HTTP · MCP · OpenAI tools
   │        │        │         │            │
   └────────┴────────┴────┬────┴────────────┘
                          │ signed requests, TLS terminates inside the enclave
                          ▼
 ┌──────────────── Tinfoil enclave · NVIDIA H200 · SEV-SNP + confidential GPU ────────┐
 │                                                                                    │
 │  SOUL  memory-api                                                                  │
 │   ├─ sealed orgs: org = cognee tenant, memories = dataset, member = role grant     │
 │   ├─ graded walls per org: sealed │ signal │ open-derived                          │
 │   ├─ /recall drops every row you can't see in SQL, before ranking                  │
 │   ├─ personal scope: visible to its author and nobody else                         │
 │   └─ contribution ledger: who added what, counts only                              │
 │                                                                                    │
 │  ROUTER  private │ frontier │ signer                                               │
 │   └─ decides per request which brain is allowed to see which slice                 │
 │                                                                                    │
 │  PRIVATE BRAIN  vLLM · Qwen3.8-27B + per-agent LoRA "soul adapters" · no egress    │
 │                                                                                    │
 └──────────────┬────────────────────────────────────────────┬────────────────────────┘
                │ de-identified slice only                   │ de-identified slice only
                ▼                                            ▼
     Darkbloom (private inference on Apple Silicon)     frontier API
```

1. **Write.** An agent remembers something into its personal scope or into an org it belongs to. Writing into an org you're not in is a 403.
2. **Recall.** Another agent asks a question. The enclave resolves its memberships and throws away everything it isn't allowed to see before a single row is scored. What you can't see is never loaded, never ranked, never shown to a model.
3. **Route.** The router picks the brain. Anything touching sealed context stays on the in-enclave Qwen. Generic reasoning can go out to Darkbloom or a frontier model, but only as a fixed, de-identified slice. Actions that need a key go to the signer, and the key never appears in any outbound payload.
4. **Answer with receipts.** Every answer comes back with access counts and contributor counts. Non-members of a `sealed` org get nothing, not even a count.
5. **Verify.** Image digests and model weights are pinned in the public [`tinfoil-config.yml`](tinfoil-config.yml), measured and logged to Sigstore. Your client attests the enclave before it sends a byte. The wall policy is part of the measured code, so the rules are provable, not promised.

## Private swarm memory (2.5)

A confidential memory layer for multiplayer agent apps, open source and pluggable into any harness.

- **Any harness, three ways in:** `clients/swarm.sh` (CLI), [`clients/mcp/swarm_mcp.py`](clients/mcp/swarm_mcp.py) (MCP stdio, drop it into Claude, Cursor or any MCP client), and [`clients/tools.json`](clients/tools.json) + [`clients/swarm_client.py`](clients/swarm_client.py) for OpenAI-style tool calling. Stdlib only, no SDK lock-in.
- **Multiplayer by design:** agents belong to many orgs at once, with `member` and `admin` roles. One `/recall` searches across every org you're in, in one query.
- **Graded walls, not binary grants:** `sealed` (members only, outsiders see nothing), `signal` (outsiders who share a person in the network learn "one org has context on this, request access", count only, no content, no name), `open-derived` (outsiders get an answer synthesized in-enclave, guarded so raw text can't be copied out).
- **Personal scope that never leaks:** not to org-mates, not to admins, not to the graph.
- **Proven, not claimed:** a leak suite ([tests/leak_test.py](tests/leak_test.py)) plants canary strings in fund, sealed and personal notes and fires extraction prompts at every wall state. 113/113 assertions pass. Membership gating proof with 403 negative controls: [docs/membership-gating-proof.json](docs/membership-gating-proof.json).

## Private inference routing (2.3)

Confidential compute deciding, per request, which brain is trusted with which bytes.

- **Three lanes:** `private` (in-enclave Qwen, sealed context allowed), `frontier` (Darkbloom or a frontier API, de-identified slice only), `signer` (key-bearing actions, key never leaves).
- **Default private, fail closed.** Unknown tasks stay in the enclave. If the private brain is down, you get a local extractive answer, never a quiet fallback to the cloud.
- **Frontier gets a projection, not a scrub.** Instead of regex-scrubbing arbitrary prose and hoping, the router maps a request onto a fixed, de-identified instruction. Raw user text and raw note text never cross the boundary, even when nothing looks secret.
- **Every decision is auditable:** each route returns the brain, the reason, the exact outbound payload bytes, and a live audit that tracks planted canaries across every frontier call. The demo's audit shows zero canaries leaked to frontier.
- **A nuanced threat model, written down.** What's protected, what isn't (lying members, timing metadata, many-query inference against `signal` hints), and what leaves the device, field by field. See [Threat model](#threat-model).

## Private cooperation (2.4)

Several people's agents, each with its own private context, climbing one leaderboard together, with no one handing over raw context.

- **The setup:** each agent keeps its private notes (training tricks, failed runs, hyperparameters that worked) in its own scope or a sealed team org. A shared `open-derived` org is the commons.
- **Cooperate without disclosing:** an agent asks the swarm "what's working on this speedrun?" and gets a derived answer synthesized inside the enclave from everyone's allowed context. It learns the lesson, never the notes.
- **The score shows who added what:** every recall returns contributor counts per scope, and the `/graph` shows each org's topic footprint without person labels. That's the attribution ledger for the leaderboard: how much of the winning context came from whom, without revealing what it was.
- **Coopetition dial:** teams flip their own wall from `sealed` to `signal` to `open-derived` as the race goes on. The co-op stays sealed while the fund shares. Same swarm, different trust per org.

## Own the model (2.2)

The brain you own, not the one you rent.

- **Open weights in your own enclave:** Qwen3.8-27B bf16 runs on vLLM inside the attested container on the H200, on a closed network with no egress. Weights mounted read-only via dm-verity, root hash attested.
- **Soul adapters:** each agent's sealed memory distills into a per-agent LoRA trained inside the enclave. The adapter learns that agent's voice, its domain and its people, and the training data never leaves the hardware. Teacher outputs come only from license-clean open models (gpt-oss-120b is Apache 2.0), via Unsloth or TRL.
- **Memory becomes skill:** recall gives the agent facts, the adapter gives it instinct. Swap the base model and the soul comes with you.

## Tinfoil Containers

The whole stack is a Tinfoil Container, not a demo beside one.

- **Live on one H200:** `membrane.shadow-labs.containers.tinfoil.dev`, two containers (memory-api + vLLM) on a closed network, single egress allowlist.
- **Users check the code it runs:** `tinfoil attestation verify` passes against the release built from this repo's public `tinfoil-config.yml`, logged to Sigstore. Measurement, config digest and vLLM image digest are all pinned.
- **Operator-blind:** memory is encrypted in hardware. Tinfoil can't read it, the cloud can't read it, we can't read it.

```bash
tinfoil --enclave membrane.shadow-labs.containers.tinfoil.dev --repo wakesync/hivemind attestation verify --json
```

### Live status: v0.2.2, generating, attested

- **Release:** [`v0.2.2`](https://github.com/wakesync/hivemind/releases/tag/v0.2.2) is promoted and serving. Qwen3.8-27B writes real text in the enclave: `/v1/generate` returns `finish_reason=stop`, and `/recall` answers in `mode=llm` with a correct synthesis of the caller's allowed notes.
- **Attestation:** `status: ok`. Config digest `44fc407a3558fdf451b9fc31d748c726e68e1c0ad5cfa16ea06e10a9baa4613a`. SEV-SNP measurement `d2030b6f7ff42bacca0b7b46965f7f752ed64747e03567f7752a385c9f6a1e15ebc6effc26277ef51784168d05e6d921`, identical in Sigstore and in the enclave's hardware report. Attested TLS key fingerprint `98c1515374acca082f0666d2e955b63c5d2be450053fbb5423ae08346775fbe6`.
- **What broke and what fixed it:** on this H200, the general-purpose `vllm/vllm-openai:v0.33.0` engine emitted token 0 at every step for Qwen3.8-27B, with any sampling settings and with or without eager mode, bf16, or language-model-only. The fault was the engine image, not the model, the weights or the sampling. v0.2.2 switches to the model-specific vLLM build from the Qwen3.8 recipe (`vllm/vllm-openai:qwen38@sha256:4a2f33a8…bc31`, pinned in [`tinfoil-config.yml`](tinfoil-config.yml)), and generation came back immediately. v0.2.0 (model-card sampling) and v0.2.1 (eager + bf16 + LM-only) were released, tested on hold and canceled, never promoted. Every step is in the public release history.
- **Auth holds:** no headers gets 422, a bad signature gets 401. No new egress, no frontier fallback.

## Darkbloom

The rented brain doesn't have to be a hyperscaler.

- **Darkbloom is a first-class brain** in the router: an OpenAI-compatible endpoint on the decentralized Apple Silicon network, so frontier-lane reasoning runs on private inference instead of a big lab's servers.
- **Same discipline:** Darkbloom only ever receives the de-identified slice the soul releases, same as any other rented brain.
- **The room is the cloud:** any Mac in the room can join Darkbloom as a provider and serve the swarm. Sovereign memory in the enclave, sovereign inference on the laptops around it.

## Cognee

Agents that share what they learn, with permissions on who reads and writes what.

- **Built on cognee's data model:** org = tenant, org memory = dataset, membership = role grant. Personal scopes and orgs each map to their own cognee dataset (`org_<id>`, `personal_<user>`), with cognify pointed at the in-enclave vLLM. See [`app/cognee_backend.py`](app/cognee_backend.py).
- **Beyond binary grants:** cognee gives tenants and ACLs; Membrane adds graded walls (`sealed` / `signal` / `open-derived`), cross-org recall in one query, and an operator-blind enclave around all of it.
- **Gets smarter every run:** what one agent learns becomes recall for the next, inside the walls. Outcomes and fixes go back into the graph, so each run starts from what the swarm already knows.

## Quickstart

```bash
docker build -t hivemind:dev .
export SWARM_SECRET=$(openssl rand -hex 24); demo/run-local.sh sm-dev 18090   # 127.0.0.1 only, read-only fs, tmpfs /data
SWARM_URL=http://127.0.0.1:18090 demo/seed.sh --slow
```

Open <http://127.0.0.1:18090/> for the live graph. Point `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY` at any OpenAI-compatible `/v1` (the in-enclave vLLM, Darkbloom, or a frontier API) to pick the brain.

Run the leak suite against a fresh instance:

```bash
demo/run-local.sh hm-test 18091 && SWARM_URL=http://127.0.0.1:18091 python3 tests/leak_test.py
```

## Built with

- **Tinfoil** confidential containers: attested enclave, Sigstore-logged measurement, dm-verity model weights.
- **NVIDIA H200** (141 GB) confidential GPU.
- **Qwen3.8-27B** bf16 on **vLLM** (the model-specific `qwen38` build, digest-pinned), inside the enclave on a closed network.
- **Darkbloom** private inference on Apple Silicon for the rented brain.
- **Cognee** data model and backend: tenants, datasets, role grants.
- **MCP** + OpenAI tool calling for any harness.

---


## Reference

> Repo history: started as `swarm-memory` (confidential shared memory for multiplayer agents), then `hivemind` (org layer), now Membrane. API, auth and enclave setup carried through every step.

### Model

- **Org** (cognee: *tenant*): `id`, `name`, `wall` policy. Its memories form one *dataset*.
- **Membership** (cognee: *role grant*): a user can be in many orgs, as `member` or `admin`.
- **Memory**: `(author, org | personal)`. Personal memories are only ever visible to their author. Not to org-mates, not to admins, not to the graph.

### Wall policy (per org, admin-toggled)

| Wall | Members | Non-members |
|---|---|---|
| `sealed` | full answers | nothing, not even a count |
| `signal` | full answers | if they share a person (transitively) and the query hits: "1 org in your network has context on this. request access." Count only, no content, no org name |
| `open-derived` | full answers | a derived answer synthesized in-enclave, never raw text (6-gram copy guard; extractive fallback can't touch non-member notes) |

### Threat model

Protected: raw notes, personal notes, embeddings, the membership graph and LLM prompts never leave the enclave. `/graph` shows topics per org with no person labels, hides topics fewer than `k` members touched, excludes personal memories and redacts topic words for `sealed` and `signal` orgs.

Not protected:
- a member who lies in notes, or leaks answers they legitimately received;
- `open-derived` facts being learned by non-members (that's the setting);
- inference across many crafted queries (e.g. probing `signal` hints). Rate limits + audit needed for prod;
- metadata like timing and request volume;
- the shared HMAC secret only authenticates the swarm, not individual agents.

### What leaves the device

| Data | Leaves the agent? | Leaves the enclave? | Who can see it |
|---|---|---|---|
| Raw note text (`/remember`) | yes, TLS into the enclave | never | nobody outside the enclave |
| Personal notes | yes, TLS into the enclave | never, used only for the author's own recall | author |
| Embeddings | computed in-enclave | never | nobody |
| Org membership graph | admins add members | never as a graph; you see your own orgs (`GET /orgs`) | you |
| LLM prompt (allowed notes + question) | n/a | never (vLLM on closed network) | nobody |
| `/recall` answer | returned to the asker | yes, to the asker | asker only, built only from scopes the walls allow |
| Access counts | n/a | counts per class | asker |
| Signal hint | n/a | "N orgs in your network have context", count only | asker |
| `/graph` | n/a | org names + wall state, topic counts, topic words only for `open-derived`, < k hidden | anyone with the URL |
| Stripe API call | n/a | outbound to `api.stripe.com` only (single egress allowlist) | Stripe |
| Attestation | n/a | public by design | anyone |

### API

Auth: `X-Swarm-User: <id>`, `X-Swarm-Ts: <unix secs>`, `X-Swarm-Sig: hex(HMAC_SHA256(SWARM_SECRET, "<id>.<ts>"))`. You always act as the signed user.

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/orgs` | `{"id","name","wall"?}` | creates a tenant, caller becomes `admin` |
| POST | `/orgs/{id}/members` | `{"user","role"?}` | admin only |
| POST | `/orgs/{id}/wall` | `{"wall": "sealed\|signal\|open-derived"}` | admin only, `{wall, was}` |
| GET | `/orgs` | | caller's orgs, wall, role, counts, personal memory count |
| POST | `/remember` | `{"text", "org"?}` | no `org` = personal. Org you're not in = 403 |
| POST | `/recall` | `{"query"}` | `{answer, mode, access:{personal_memories, member_orgs, member_memories, derived_orgs, derived_memories, signal_orgs}, contributors:{...}, hints:[...]}` |
| POST | `/v1/generate` | `{"messages", "max_tokens"?}` | Stateless, fixed in-enclave model, no tools, capped, one request in flight (429 otherwise). Persists nothing |
| GET | `/graph` | | `{orgs, nodes, links, k, hidden_topics}` |
| GET | `/healthz`, `/attestation`, `/` | | health, enclave badge data, live graph |

### Env

`SWARM_SECRET` (required), `LLM_BASE_URL` (default in-enclave vLLM), `LLM_MODEL` (default `qwen3.8-27b`), `LLM_API_KEY`, `LLM_TEMPERATURE` / `LLM_TOP_P` / `LLM_TOP_K` / `LLM_MIN_P` / `LLM_PRESENCE_PENALTY` / `LLM_REPETITION_PENALTY` (model-card defaults), `LLM_EXTRA_BODY`, `GRAPH_K` (2), `RECALL_K` (8), `RECALL_MIN_SIM` (0.15), `SIGNAL_MIN_SIM` (0.35), `ORG_DAY_USD` (5), `DB_PATH`, `STRIPE_KEY` (`rk_...` or stub), `MEMORY_BACKEND` (`sqlite` | `cognee`).

### Conduit demo (seed.sh)

| Org | Wall | Members |
|---|---|---|
| Ledger Fund | `signal` | mara (admin), ines |
| Tinplate Labs | `sealed` | teo (admin), rook |
| Greenpoint Co-op | `sealed` | rook (admin), mara |

- **(a)** teo asks who could fund a 250K rooftop pilot. Gets Tinplate's context plus a signal hint, no fund content or name.
- **(b)** mara (fund + co-op) gets the full plan (`member_orgs=2`).
- **(c)** mara flips Ledger Fund to `open-derived`. teo now gets a derived answer (`derived_orgs=1`); the co-op stays sealed.

`demo/seed.sh --seed-only` loads data only. `--steps-only` re-runs a-c (reset with `clients/swarm.sh wall mara ledger-fund signal`).

### Deploy to Tinfoil

Two containers (memory-api + vLLM) on a closed `swarm` network, single egress allowlist (`api.stripe.com`).

1. **Image:** `gh workflow run image.yml -f tag=<tag>`. Copy the `sha256:` digest. Private GHCR: `tinfoil registry set ghcr --username <gh-user> --token <read:packages PAT>`.
2. **Pin:** `tinfoil/pin.sh api sha256:<digest>`. Qwen's mpk is already pinned.
3. **Release:** commit the root `tinfoil-config.yml`, then `gh workflow run tinfoil-release.yml -f version=<version>`.
4. **Secrets:** `tinfoil secret create SWARM_SECRET`, `tinfoil secret create STRIPE_KEY` (`none` for stub).
5. **Create:** `tinfoil container create hivemind --repo <org>/<config-repo> --tag <version> --host <gpu-host> --secret SWARM_SECRET --secret STRIPE_KEY`. Updates: `tinfoil container update <id> --tag <version> --hold`, check the candidate, then `tinfoil container promote <id>`.
6. **Verify:** `tinfoil attestation verify`, `tinfoil http`, or `tinfoil container connect hivemind`. vLLM can take ~30 min to load weights on first boot.

**Model:** Qwen3.8-27B bf16 (~54 GB) on one H200, thinking off per request, on `vllm/vllm-openai:qwen38` (the generic `v0.33.0` image emits token 0 on H200 for this model, so don't swap it back). Fallback configs: [`tinfoil/fallback/`](tinfoil/fallback/) (gpt-oss-20b, and a Qwen eager-mode engine config). Weights mounted read-only via dm-verity, root hash attested.

**Persistence:** `/data` is tmpfs, memory resets on redeploy. For durable memory, attach an [encrypted persistent volume](https://docs.tinfoil.sh/containers/encrypted-volumes.md) at `/data`.

### Cognee backend

`MEMORY_BACKEND=cognee` ([`app/cognee_backend.py`](app/cognee_backend.py)) maps one cognee dataset per org (`org_<id>`) and per personal scope (`personal_<user>`), with cognify pointed at the in-enclave vLLM. Wall enforcement stays in `/recall` above the backend. Ships as a separate image profile (cognee 1.6.7, ~1 GB of deps) so the default enclave image stays lean.

### Related work

- **Deeplake Hivemind** ([activeloopai/hivemind](https://github.com/activeloopai/hivemind)): org-wide shared agent memory, cloud-hosted, operator holds plaintext. Unrelated project with the same name.
- **Cognee multi-user mode**: tenants, roles, per-dataset ACLs. One active tenant at a time, binary grants. Closest data model; we use its vocabulary.
- **Collaborative Memory** ([arXiv:2505.18279](https://arxiv.org/abs/2505.18279)), **MemTrust** ([arXiv:2601.07004](https://arxiv.org/abs/2601.07004)), **SAMEP** ([arXiv:2507.10562](https://arxiv.org/abs/2507.10562)).

What Membrane adds: operator-blind storage and policy in an attested enclave, cross-org recall in one query, graded walls instead of binary grants, and personal scope that never leaves its author.

### Layout

```
app/main.py            API: auth, orgs/members/walls, remember/recall, generate, graph, ledger, attestation
app/cognee_backend.py  cognee backend (dataset per org)
web/graph.html         live graph
clients/               swarm.sh, swarm_client.py, tools.json, mcp/swarm_mcp.py
demo/seed.sh           conduit scenario (a, b, c)
demo/run-local.sh      local container on 127.0.0.1:<port>
demo/motion-v2/        hero film still (hero.gif)
docs/                  QUICKSTART.md (attest + sign + call, python/ts), membership-gating proof
tests/                 leak_test.py (canaries across all wall states), generate_test.py
tinfoil/               pin.sh, fallback configs
tinfoil-config.yml     public, measured enclave config
```

Pitch and claim limits: [PITCH.md](PITCH.md), [DEMO-SCRIPT.md](DEMO-SCRIPT.md).
