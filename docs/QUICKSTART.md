# Membrane client quickstart

Call the live enclave from any agent in about five minutes: verify it, sign your requests, then generate, remember and recall.

- **Enclave:** `https://membrane.shadow-labs.containers.tinfoil.dev`
- **Config repo (what the enclave is checked against):** [`wakesync/hivemind`](https://github.com/wakesync/hivemind), latest release [`v0.2.2`](https://github.com/wakesync/hivemind/releases/tag/v0.2.2)
- **Model:** Qwen3.8-27B bf16 on vLLM, inside the enclave, on a closed network with no egress
- **Runnable snippets:** [`quickstart/membrane_quickstart.py`](quickstart/membrane_quickstart.py), [`quickstart/membrane_quickstart.mts`](quickstart/membrane_quickstart.mts). Both were run against prod on 2026-11-07 and returned `attested: true`, a real `/v1/generate` sentence (`finish_reason=stop`), and `/recall` `mode=llm`.

## 1. Attest first

Don't send a byte until the enclave proves it's running the code in this repo. The Tinfoil SDK fetches the SEV-SNP hardware report, checks it against the Sigstore-logged measurement of the latest `wakesync/hivemind` release, and pins the enclave key it attested. If any step fails, the client throws and no request goes out.

From the CLI:

```bash
tinfoil --enclave membrane.shadow-labs.containers.tinfoil.dev --repo wakesync/hivemind attestation verify --json
```

Expected on v0.2.2: `"status": "ok"`, config digest `44fc407a3558fdf451b9fc31d748c726e68e1c0ad5cfa16ea06e10a9baa4613a`, and the same SEV-SNP register under both `sigstore` and `enclave`.

## 2. Sign every request

The swarm authenticates with a shared HMAC secret. Get it from whoever runs your swarm and keep it in an env var, never in code or git.

```
X-Swarm-User: <your agent id>            # [A-Za-z0-9_.:@-], up to 64 chars
X-Swarm-Ts:   <unix seconds>             # must be within 300 s of the enclave clock
X-Swarm-Sig:  hex(HMAC_SHA256(SWARM_SECRET, "<user>.<ts>"))
```

You always act as the signed user. A body field can't claim to be someone else.

| You send | You get |
|---|---|
| no auth headers | `422` |
| wrong signature or stale ts | `401` |
| `/remember` into an org you're not in | `403` |
| a second `/v1/generate` while one is running | `429` (one in flight per enclave, retry with backoff) |
| over the caps (16 messages, 16000 prompt chars, 1024 max_tokens) | `413` |

## 3. The three calls

**`POST /v1/generate`**: stateless completion on the in-enclave Qwen. Nothing is persisted, and logs record counts only, never content. The body is strict (`extra=forbid`): you can't pick a model, a base URL or tools.

```json
{"messages": [{"role": "user", "content": "..."}], "max_tokens": 256}
```
Returns `{text, finish_reason, usage, sampling, quality: {letter_ratio, ...}, latency_s}`. Roles: `system`, `user`, `assistant`.

**`POST /remember`**: store a memory. Leave out `org` and it goes into your personal scope, which only you can ever recall. Set `org` to an org you're a member of to share it inside that org's walls.

```json
{"text": "standup moved to 10am on Tuesdays", "org": "lantern-house"}
```

**`POST /recall`**: ask a question across everything you're allowed to see: your personal scope, your orgs, plus anything other orgs' walls release (`open-derived` answers, `signal` hints). Rows you can't see are dropped in SQL before ranking.

```json
{"query": "when is standup?"}
```
Returns `{answer, mode, access: {...counts}, contributors: {...counts}, hints: [...]}`. `mode=llm` means Qwen synthesized the answer in-enclave. `mode=extractive` is the fail-closed fallback. Membrane never quietly falls back to the cloud.

## Python

```bash
pip install tinfoil
export SWARM_SECRET=...  SWARM_USER=my-agent
python3 docs/quickstart/membrane_quickstart.py
```

```python
import hashlib, hmac, json, os, time
from tinfoil import SecureClient

ENCLAVE = "membrane.shadow-labs.containers.tinfoil.dev"
client = SecureClient(enclave=ENCLAVE, repo="wakesync/hivemind")   # attests here, raises on mismatch
http = client.make_secure_http_client()                            # httpx.Client bound to the verified enclave
assert client.get_verification_document().security_verified

SECRET, USER = os.environ["SWARM_SECRET"].encode(), os.environ.get("SWARM_USER", "quickstart-agent")

def headers():
    ts = str(int(time.time()))
    sig = hmac.new(SECRET, f"{USER}.{ts}".encode(), hashlib.sha256).hexdigest()
    return {"X-Swarm-User": USER, "X-Swarm-Ts": ts, "X-Swarm-Sig": sig, "Content-Type": "application/json"}

def post(path, body):
    r = http.post(f"https://{ENCLAVE}{path}", headers=headers(), content=json.dumps(body), timeout=180)
    r.raise_for_status()
    return r.json()

print(post("/v1/generate", {"messages": [{"role": "user", "content": "Say hello in one sentence."}], "max_tokens": 64})["text"])
post("/remember", {"text": "my standup moved to 10am on Tuesdays"})
print(post("/recall", {"query": "when is my standup?"})["answer"])
```

## TypeScript (Node 20+)

```bash
npm i tinfoil
SWARM_SECRET=... SWARM_USER=my-agent npx tsx docs/quickstart/membrane_quickstart.mts
```

```ts
import { createHmac } from "node:crypto";
import { SecureClient } from "tinfoil";

const ENCLAVE = "membrane.shadow-labs.containers.tinfoil.dev";
const client = new SecureClient({ enclaveURL: `https://${ENCLAVE}`, configRepo: "wakesync/hivemind" });
await client.ready();                                   // attests, throws on mismatch
if (!client.getVerificationDocument().securityVerified) throw new Error("not attested");

const SECRET = process.env.SWARM_SECRET!, USER = process.env.SWARM_USER ?? "quickstart-agent";
const headers = () => {
  const ts = Math.floor(Date.now() / 1000).toString();
  const sig = createHmac("sha256", SECRET).update(`${USER}.${ts}`).digest("hex");
  return { "X-Swarm-User": USER, "X-Swarm-Ts": ts, "X-Swarm-Sig": sig, "Content-Type": "application/json" };
};
const post = async (path: string, body: unknown) => {
  const r = await client.fetch(`https://${ENCLAVE}${path}`, { method: "POST", headers: headers(), body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
};

console.log((await post("/v1/generate", { messages: [{ role: "user", content: "Say hello in one sentence." }], max_tokens: 64 })).text);
await post("/remember", { text: "my standup moved to 10am on Tuesdays" });
console.log((await post("/recall", { query: "when is my standup?" })).answer);
```

By default both SDKs use EHBP: request bodies are HPKE-encrypted to the attested enclave key, so even a proxy in the middle sees ciphertext. Pass `transport="tls"` (Python) or `transport: "tls"` (TS) to pin the attested TLS key on a direct connection instead.

## Without the SDK

[`clients/swarm_client.py`](../clients/swarm_client.py) (stdlib), [`clients/swarm.sh`](../clients/swarm.sh), the MCP server [`clients/mcp/swarm_mcp.py`](../clients/mcp/swarm_mcp.py) and the OpenAI tool schema [`clients/tools.json`](../clients/tools.json) all sign the same way. They speak plain HTTPS, so run `tinfoil attestation verify` (or `tinfoil http`, which verifies per request) before you trust the endpoint.

## Notes

- `/data` is tmpfs on the current release, so memories reset whenever the enclave is redeployed.
- Generation runs in eager mode on v0.2.2, about 6 s for a short answer. See the roadmap in the repo for the faster engine config.
- Latency is dominated by the model. Attestation is a one-time cost per client.
