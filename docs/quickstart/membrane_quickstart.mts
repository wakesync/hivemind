// Membrane quickstart (Node 20+): attest the enclave, then sign and call it.
//   npm i tinfoil && npx tsx membrane_quickstart.mts
//   env: SWARM_SECRET (never commit), SWARM_USER
import { createHmac } from "node:crypto";
import { SecureClient } from "tinfoil";

const ENCLAVE = "membrane.shadow-labs.containers.tinfoil.dev";
const REPO = "wakesync/hivemind"; // public config repo, verified against its latest Sigstore-logged release
const SECRET = process.env.SWARM_SECRET!;
const USER = process.env.SWARM_USER ?? "quickstart-agent";

// 1. Attest before sending a byte. ready() throws if anything doesn't match.
const client = new SecureClient({ enclaveURL: `https://${ENCLAVE}`, configRepo: REPO });
await client.ready();
const doc = client.getVerificationDocument();
console.log("attested:", doc.securityVerified, "config", doc.configRepo);

// 2. Sign every request: hex(HMAC_SHA256(secret, "<user>.<unix ts>")), 300 s skew window.
function headers(user = USER): Record<string, string> {
  const ts = Math.floor(Date.now() / 1000).toString();
  const sig = createHmac("sha256", SECRET).update(`${user}.${ts}`).digest("hex");
  return { "X-Swarm-User": user, "X-Swarm-Ts": ts, "X-Swarm-Sig": sig, "Content-Type": "application/json" };
}

async function post(path: string, body: unknown): Promise<any> {
  const r = await client.fetch(`https://${ENCLAVE}${path}`, { method: "POST", headers: headers(), body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`${path} -> ${r.status} ${await r.text()}`);
  return r.json();
}

// 3. Stateless generation on the in-enclave Qwen3.8-27B. Nothing persisted.
const g = await post("/v1/generate", {
  messages: [{ role: "user", content: "One sentence: why should agent memory live in an enclave?" }],
  max_tokens: 96,
});
console.log("generate:", g.finish_reason, g.text);

// 4. Remember into your personal scope (no "org" = only you), then recall.
console.log("remember:", await post("/remember", { text: "quickstart: my standup moved to 10am on Tuesdays" }));
const r = await post("/recall", { query: "when is my standup?" });
console.log("recall:", r.mode, r.answer, r.access);
