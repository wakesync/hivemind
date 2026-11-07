"""Membrane quickstart: attest the enclave, then sign and call it.

pip install tinfoil
export SWARM_SECRET=...   # shared swarm HMAC secret, never commit it
export SWARM_USER=my-agent
"""
import hashlib, hmac, json, os, time

from tinfoil import SecureClient

ENCLAVE = "membrane.shadow-labs.containers.tinfoil.dev"
REPO = "wakesync/hivemind"  # public config repo; the SDK checks the enclave against its latest Sigstore-logged release
SECRET = os.environ["SWARM_SECRET"].encode()
USER = os.environ.get("SWARM_USER", "quickstart-agent")

# 1. Attest before sending a byte. Raises if the hardware report, the Sigstore
#    measurement or the TLS key don't match.
client = SecureClient(enclave=ENCLAVE, repo=REPO, transport=os.environ.get("TINFOIL_TRANSPORT", "ehbp"))
http = client.make_secure_http_client()  # httpx.Client bound to the verified enclave
doc = client.get_verification_document()
print("attested:", doc.security_verified if doc else None, "config", doc.config_repo if doc else REPO)


# 2. Sign every request: hex(HMAC_SHA256(secret, "<user>.<unix ts>")), 300 s skew window.
def headers(user: str = USER) -> dict:
    ts = str(int(time.time()))
    sig = hmac.new(SECRET, f"{user}.{ts}".encode(), hashlib.sha256).hexdigest()
    return {"X-Swarm-User": user, "X-Swarm-Ts": ts, "X-Swarm-Sig": sig, "Content-Type": "application/json"}


def post(path: str, body: dict) -> dict:
    r = http.post(f"https://{ENCLAVE}{path}", headers=headers(), content=json.dumps(body), timeout=180)
    r.raise_for_status()
    return r.json()


# 3. Stateless generation on the in-enclave Qwen3.8-27B. Nothing persisted.
g = post("/v1/generate", {"messages": [{"role": "user", "content": "One sentence: why should agent memory live in an enclave?"}],
                          "max_tokens": 96})
print("generate:", g["finish_reason"], g["text"])

# 4. Remember into your personal scope (no "org" = only you can ever recall it), then recall.
print("remember:", post("/remember", {"text": "quickstart: my standup moved to 10am on Tuesdays"}))
r = post("/recall", {"query": "when is my standup?"})
print("recall:", r["mode"], r["answer"])
print("access:", r["access"])
