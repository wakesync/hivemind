# swarm-memory

**Confidential shared memory for multiplayer agents.** Every agent in a swarm writes private notes. Any agent can ask a question and get an answer synthesized across *everyone's* notes, without ever seeing anyone else's notes, and without the operator (us) seeing them either. Runs as a [Tinfoil Container](https://docs.tinfoil.sh/containers/overview) inside an AMD SEV / Intel TDX enclave with an H200 in confidential-computing mode serving the LLM.

## Threat model: we built it and we can't read it

Agents send notes over TLS that terminates inside the enclave. Storage (sqlite on enclave memory), embeddings (MiniLM, baked into the image) and answer synthesis (Qwen3.8-27B on vLLM, a second container in the same enclave on a network with no egress) all run inside hardware-encrypted memory that the host, the cloud, Tinfoil and the people who wrote this code can't inspect. The exact image digests and model weights (dm-verity root hash) are pinned in a public `tinfoil-config.yml`, measured, and logged to Sigstore, so any client can attest before sending a byte that it's talking to this code and nothing else. The API never returns another member's raw text: `/recall` returns a paraphrased answer plus contributor *counts*, and `/graph` returns topics with no names, hiding any topic fewer than `k` members touched. What we don't protect against: a member who lies in their notes, inference attacks across lots of carefully crafted queries (rate-limit and audit if that matters), or a compromised member leaking their own answers. The shared HMAC secret only authenticates the swarm. It doesn't hide anything from the enclave, because it doesn't need to.

## What leaves the device

| Data | Leaves the agent? | Leaves the enclave? | Who can see it |
|---|---|---|---|
| Raw note text (`/remember`) | yes, TLS into the enclave | **never** | nobody outside the enclave |
| Embeddings | computed in-enclave | **never** | nobody |
| LLM prompt (notes + question) | n/a | **never** (vLLM container, closed network) | nobody |
| `/recall` answer | returned to the asking agent | yes, to the asker | asker only, paraphrased + PII-scrubbed |
| Contributor info | n/a | counts only (`scopes`, `memories`) | asker |
| `/graph` | n/a | topic words + co-occurrence counts, topics with < k contributors hidden | anyone with the URL |
| `/ledger` (Track 01) | n/a | aggregate revenue only, no customer fields | anyone with the URL |
| Stripe API call | n/a | outbound to `api.stripe.com` only (allowlisted egress) | Stripe |
| Attestation | n/a | public by design | anyone |

## API

Auth: three headers. `X-Swarm-User: <id>`, `X-Swarm-Ts: <unix secs>`, `X-Swarm-Sig: hex(HMAC_SHA256(SWARM_SECRET, "<id>.<ts>"))`. The scope is always the signed user. You can't write to or read as someone else.

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/remember` | `{"text": "..."}` | `{stored, scope, topics}` |
| POST | `/recall` | `{"query": "..."}` | `{answer, mode: llm\|extractive, contributors: {scopes, other_scopes, memories, own_memories}}` |
| GET | `/graph` | | `{nodes:[{id, weight, contributors}], links:[{source,target,weight}], k, hidden_topics, members, memories}` |
| GET | `/healthz` | | backend, counts, model |
| GET | `/attestation` | | badge data from the enclave's `/tinfoil/attestation.json` |
| GET | `/ledger` | | Track 01 revenue aggregate (read-only Stripe charges, stub if no key) |
| GET | `/` | | live thermal graph (`web/graph.html`) |

If the LLM is down or unset, `/recall` falls back to extractive mode: it picks the best-matching *sentences* (not records), neutralizes first person, scrubs emails, handles, numbers and keys, and drops attribution.

## Plug in any harness (one env var + 2 HTTP calls)

```bash
export SWARM_URL=https://swarm-memory.<org>.containers.tinfoil.dev   # + SWARM_SECRET, SWARM_USER
```

- **Anything that speaks HTTP:** `clients/swarm.sh remember alice "..."` / `clients/swarm.sh recall alice "..."`
- **OpenAI-style tool calling (langchain, eliza, salem, custom loops):** pass `clients/tools.json` as `tools`, route calls with `Swarm().dispatch(name, args)` from `clients/swarm_client.py` (stdlib only).
- **MCP (Claude Code, Cursor, etc.):** `claude mcp add swarm-memory -e SWARM_URL=... -e SWARM_SECRET=... -e SWARM_USER=alice -- python3 clients/mcp/swarm_mcp.py` (stdio, 2 tools, stdlib only).

Production clients should connect through Tinfoil's attesting client (`tinfoil container connect` / `tinfoil http`, or a Tinfoil SDK `SecureClient`) so the connection is verified before any note is sent.

## Run locally

```bash
docker build -t swarm-memory:dev .
docker run --rm -p 8080:8080 -e SWARM_SECRET=$(openssl rand -hex 24) \
  -e LLM_BASE_URL=<any OpenAI-compatible /v1> -e LLM_MODEL=<model> -e LLM_API_KEY=<key> \
  --read-only --tmpfs /data:uid=10001 --tmpfs /tmp swarm-memory:dev
# no LLM? add -e LLM_ENABLED=0 for extractive answers
SWARM_URL=http://localhost:8080 SWARM_SECRET=<same> demo/seed.sh
open http://localhost:8080/
```

Env: `SWARM_SECRET` (required), `LLM_BASE_URL` (default `http://vllm:8001/v1`, the in-enclave vLLM), `LLM_MODEL` (default `qwen3.8-27b`), `LLM_API_KEY`, `LLM_TEMPERATURE` (default 0.2, blank to omit), `LLM_EXTRA_BODY` (JSON merged into the request), `GRAPH_K` (default 2), `RECALL_K`, `RECALL_MIN_SIM`, `DB_PATH`, `STRIPE_KEY` (restricted read-only `rk_...`; anything else means stub), `MEMORY_BACKEND` (`sqlite` | `cognee`).

## Deploy to Tinfoil

Two repos. The **image** can stay private. The **config** must be public, because it's the verification recipe.

1. **Image** (this repo, can stay private): push, then `gh workflow run image.yml -f tag=v0.1.0`. Copy the `sha256:` digest from the run summary. If the GHCR package is private, give Tinfoil pull creds: `tinfoil registry set ghcr --username <gh-user> --token <read:packages PAT>`.
2. **Pin it:** `tinfoil/pin.sh api sha256:<digest>`. Qwen's mpk is already pinned.
3. **Config repo (public):** push the contents of `tinfoil/` (`tinfoil-config.yml` + `.github/workflows/tinfoil-release*.yml` from the official template) to a new public repo, then `gh workflow run tinfoil-release.yml -f version=v0.0.1`.
4. **Secrets:** `tinfoil secret create SWARM_SECRET` and `tinfoil secret create STRIPE_KEY` (value `none` for the stub ledger), or via the dashboard Secrets tab.
5. **Create:** `tinfoil container create swarm-memory --repo <org>/<config-repo> --tag v0.0.1 --host iridium-1 --secret SWARM_SECRET --secret STRIPE_KEY`
6. **Verify:** `tinfoil attestation verify` against the instance, `tinfoil http` for a verified request, or `tinfoil container connect swarm-memory` for a local verified proxy you can point `SWARM_URL` at. The vLLM container needs up to ~30 min on first boot to load the weights (healthcheck `start_period: 30m`).

### Model choice

**Qwen3.8-27B in bf16 (~54 GB weights)** on one H200 (141 GB): it fits with room for 32k-context KV cache, it's strong at multi-document synthesis, and running dense bf16 means no quantization surprises. Thinking is turned off per request (`chat_template_kwargs.enable_thinking=false`) to keep latency low, and `<think>` blocks are stripped anyway. **Fallback:** `tinfoil/fallback/tinfoil-config.gpt-oss-20b.yml` (MXFP4, ~14 GB, already wrapped on iridium-1). gpt-oss-120b was also wrapped (~196 GB on disk, so it doesn't fit a single H200 alongside KV without heavy tradeoffs, which is why we skipped it). Weights get mounted read-only via dm-verity at `/tinfoil/mpk/mpk-<root>`, and the enclave attests to the root hash.

### Persistence

Tinfoil enclaves have no persistent disk by default. The config mounts `/data` as tmpfs, so memory resets on redeploy. That's fine for a demo and arguably the right default for ephemeral swarms. For durable memory, attach an [encrypted persistent volume](https://docs.tinfoil.sh/containers/encrypted-volumes.md) (beta) at `/data`.

## Cognee

`MEMORY_BACKEND=cognee` is wired up (`app/cognee_backend.py`: one cognee dataset per scope, and cognify points at the same in-enclave vLLM). cognee 1.6.7 installs and imports cleanly, but it's **not in the default image** (about 1 GB of deps), and add+cognify wasn't validated end to end inside the hackathon timebox. The sqlite + MiniLM backend is what's shipped and demoed. `/graph` stays on the sqlite topic aggregation either way, so the k-anonymity floor lives in one place.

## Layout

```
app/main.py            API (FastAPI): auth, remember/recall/graph/ledger/attestation
app/cognee_backend.py  experimental graph backend
web/graph.html         live ironbow force graph + attestation badge + Track 01 revenue panel
clients/               swarm.sh, swarm_client.py, tools.json, mcp/swarm_mcp.py
demo/seed.sh           4 agents, 1 cross-member question
tinfoil/               PUBLIC config repo contents (config, release workflows, pin.sh, fallback)
.github/workflows/image.yml   build+push image, prints digest
```
