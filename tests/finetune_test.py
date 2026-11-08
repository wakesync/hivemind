#!/usr/bin/env python3
"""End-to-end private fine-tuning test on CPU (no GPU, no network beyond first model fetch).

memory-api (app.main + app.finetune) and the trainer sidecar (trainer.service) run in
process; HTTP between them is routed through TestClient, so the real dataset builder,
the real PEFT trainer, the real adapter store and both serving paths are exercised.

    FT_TEST_MODEL=tiny-random/qwen3.5 python3 tests/finetune_test.py      # ~1 min, pipeline only
    FT_TEST_MODEL=Qwen/Qwen3.5-0.8B   python3 tests/finetune_test.py      # same arch as prod, slower

Checks consent walls (sealed / signal / open-derived / personal), auth, job lifecycle,
receipts, non-exportability, vLLM LoRA request shape (mocked engine), revoke = delete.
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="hivemind-ft-"))
os.environ.update({
    "SWARM_SECRET": "unit-test-secret", "DB_PATH": str(TMP / "swarm.db"), "LLM_ENABLED": "0",
    "FT_ENABLED": "1", "FT_SERVE": "trainer", "FT_TRAINER_URL": "http://trainer:8090",
    "FT_ADAPTER_DIR": str(TMP / "adapters"), "FT_ADAPTER_ROOT": "/adapters",
    "FT_BASE_MODEL": os.environ.get("FT_TEST_MODEL", "tiny-random/qwen3.5"), "FT_KEEP_BASE": "1",
    "RECEIPT_KEY_DIR": str(TMP / "keys"), "EMBED_CACHE": os.environ.get("EMBED_CACHE", str(TMP / "emb")),
})
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main as m  # noqa: E402  (main wires app.finetune in)
from app import finetune as ft  # noqa: E402
from trainer import service as ts  # noqa: E402

FAILS = []
SECRET = os.environ["SWARM_SECRET"]
c = TestClient(m.app)
tc = TestClient(ts.app)
TRAINER_SENT, VLLM_SENT = [], []

# attested receipt key, as the enclave would grant it at /run/tinfoil/keys/<id>/
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

_k = Ed25519PrivateKey.generate()
(TMP / "keys").mkdir()
(TMP / "keys" / "private_key.pem").write_bytes(_k.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
(TMP / "keys" / "public_key.pem").write_bytes(_k.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))


class Router:
    """memory-api's outbound HTTP: trainer -> in-process trainer app, vllm -> fake engine."""

    def request(self, method, url, json=None, timeout=None, headers=None):
        if url.startswith("http://trainer:8090"):
            path = url[len("http://trainer:8090"):]
            TRAINER_SENT.append({"method": method, "path": path, "json": json})
            return tc.request(method, path, json=json, headers=headers or {})
        VLLM_SENT.append({"method": method, "url": url, "json": json})
        req = httpx.Request(method, url)
        if url.endswith("/v1/load_lora_adapter"):
            return httpx.Response(200, request=req, text="Success")
        if url.endswith("/v1/unload_lora_adapter"):
            return httpx.Response(200, request=req, text="Success")
        if url.endswith("/chat/completions"):
            return httpx.Response(200, request=req, json={"choices": [{"message": {
                "content": "The lantern house keeps its seed library on the north shelf."},
                "finish_reason": "stop"}]})
        return httpx.Response(404, request=req)


ft.http = Router()


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond:
        FAILS.append(name)


def hdr(user):
    ts_ = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), f"{user}.{ts_}".encode(), hashlib.sha256).hexdigest()
    return {"X-Swarm-User": user, "X-Swarm-Ts": ts_, "X-Swarm-Sig": sig}


def post(user, path, body=None):
    return c.post(path, json=body, headers=hdr(user))


ORG_CANARY = "LANTERN-ORG-5521"
ALICE_CANARY = "ALICE-OPTIN-8830"
BOB_CANARY = "BOB-NOCONSENT-1177"
SEALED_CANARY = "LEDGER-SEALED-7731"
SIGNAL_CANARY = "SIGNAL-ORG-3092"
OPEN_CANARY = "COMMONS-OPEN-6604"

# --- world: lantern (alice admin, bob member); ledger sealed; beacon signal; commons open-derived.
#     bob is also in ledger + beacon, so they're in lantern's network.
for user, org, wall in (("alice", "lantern-house", "sealed"), ("carol", "ledger-fund", "sealed"),
                        ("dave", "beacon-coop", "signal"), ("erin", "commons", "open-derived")):
    check(f"create {org}", post(user, "/orgs", {"id": org, "name": org.replace("-", " ").title(),
                                                 "wall": wall}).status_code == 200)
check("add bob to lantern", post("alice", "/orgs/lantern-house/members", {"user": "bob"}).status_code == 200)
check("add bob to ledger", post("carol", "/orgs/ledger-fund/members", {"user": "bob"}).status_code == 200)
check("add bob to beacon", post("dave", "/orgs/beacon-coop/members", {"user": "bob"}).status_code == 200)
notes = [
    ("alice", "lantern-house", f"The seed library lives on the north shelf of the lantern house, code {ORG_CANARY}."),
    ("bob", "lantern-house", "Tuesday potlucks at the lantern house start at seven and everyone brings bread."),
    ("alice", None, f"I want the lantern house to start a winter tool share, my code {ALICE_CANARY}."),
    ("bob", None, f"I privately think the potluck should move to Sunday, my code {BOB_CANARY}."),
    ("carol", "ledger-fund", f"Ledger fund diligence on the solar pilot, internal code {SEALED_CANARY}."),
    ("bob", "ledger-fund", "Ledger fund will review the rooftop solar pilot next quarter."),
    ("dave", "beacon-coop", f"Beacon coop runs the bike repair night, code {SIGNAL_CANARY}."),
    ("erin", "commons", f"The commons garden waters on Mondays, code {OPEN_CANARY}."),
]
for user, org, text in notes:
    r = post(user, "/remember", {"text": text, **({"org": org} if org else {})})
    check(f"remember {user}/{org or 'personal'}", r.status_code == 200)

# --- consent gates before anything is turned on
r = post("alice", "/v1/orgs/lantern-house/finetune")
check("no consent yet -> 409", r.status_code == 409)
check("trainer never called without consent", not TRAINER_SENT)
check("member (non-admin) cannot start -> 403", post("bob", "/v1/orgs/lantern-house/finetune").status_code == 403)
check("non-member cannot start -> 403/404", post("carol", "/v1/orgs/lantern-house/finetune").status_code in (403, 404))
check("non-admin cannot set policy -> 403",
      post("bob", "/v1/orgs/lantern-house/finetune/policy", {"org_memories": True}).status_code == 403)
check("non-member cannot consent -> 404",
      post("carol", "/v1/orgs/lantern-house/finetune/consent", {"personal": True}).status_code == 404)
check("no auth -> 422", c.post("/v1/orgs/lantern-house/finetune").status_code == 422)

check("admin turns on org memories",
      post("alice", "/v1/orgs/lantern-house/finetune/policy", {"org_memories": True}).status_code == 200)
check("alice opts in personal",
      post("alice", "/v1/orgs/lantern-house/finetune/consent", {"personal": True}).status_code == 200)

ds = ft.build_dataset("lantern-house")
blob = json.dumps(ds["examples"])
check("dataset has lantern org memory", ORG_CANARY in blob)
check("dataset has alice opted-in personal memory", ALICE_CANARY in blob)
check("dataset excludes bob personal (no consent)", BOB_CANARY not in blob)
check("dataset excludes sealed org even though bob is a member there", SEALED_CANARY not in blob)
check("dataset excludes signal org in the network", SIGNAL_CANARY not in blob)
check("dataset excludes open-derived org", OPEN_CANARY not in blob)
check("dataset excludes ledger's member-visible note too", "rooftop solar" not in blob)
check("dataset counts", ds["memories"] == 3 and ds["contributors"] == ["alice", "bob"])
check("dataset is chat examples ending in assistant",
      all(e["messages"][-1]["role"] == "assistant" for e in ds["examples"]))

# --- start + poll
hp = {"r": 8, "epochs": 4, "lr": 2e-3, "max_steps": 12, "grad_accum": 1, "max_len": 256}
if os.environ.get("FT_TEST_STRICT") == "1":  # enough steps for a real model to actually learn the facts
    hp.update(epochs=10, max_steps=60)
check("rank over serving max -> 422", post("alice", "/v1/orgs/lantern-house/finetune", {"r": 32}).status_code == 422)
r = post("alice", "/v1/orgs/lantern-house/finetune", hp)
check("start -> 202", r.status_code == 202)
job = r.json()
jid = job["job_id"]
sent = [s for s in TRAINER_SENT if s["path"] == "/jobs"][-1]["json"]
sblob = json.dumps(sent)
check("payload to trainer == consented dataset only",
      ORG_CANARY in sblob and ALICE_CANARY in sblob and
      not any(x in sblob for x in (BOB_CANARY, SEALED_CANARY, SIGNAL_CANARY, OPEN_CANARY)))
check("payload carries no user ids", '"alice"' not in sblob and '"bob"' not in sblob)
check("second start while running -> 409", post("alice", "/v1/orgs/lantern-house/finetune", hp).status_code == 409)
check("trainer rejects bad token", tc.get(f"/jobs/{jid}", headers={"Authorization": "Bearer nope"}).status_code == 401)
check("non-member status -> 404", c.get(f"/v1/orgs/lantern-house/finetune/{jid}", headers=hdr("carol")).status_code == 404)
check("job id under wrong org -> 404", c.get(f"/v1/orgs/ledger-fund/finetune/{jid}", headers=hdr("bob")).status_code == 404)

t0 = time.time()
st = {}
while time.time() - t0 < 1800:
    st = c.get(f"/v1/orgs/lantern-house/finetune/{jid}", headers=hdr("bob")).json()
    if st["state"] in ("done", "failed", "lost"):
        break
    time.sleep(2)
print("job:", {k: st.get(k) for k in ("state", "step", "total")}, (st.get("result") or {}).get("seconds"), "s")
check("job done", st.get("state") == "done")
res = st.get("result") or {}
sha = st.get("adapter_sha256")
check("loss went down", res.get("loss_last", 9e9) < res.get("loss_first", 0))
adir = Path(os.environ["FT_ADAPTER_DIR"]) / "lantern-house" / str(sha)
check("adapter stored in enclave adapter dir by sha", (adir / "adapter_model.safetensors").exists())
check("no temp dirs left", not [p for p in adir.parent.iterdir() if p.name.startswith(".tmp")])
from trainer.train_lora import PEFT_SERVE_PREFIX, adapter_digest  # noqa: E402

check("adapter sha recomputes from files", adapter_digest(adir)["adapter_sha256"] == sha)
from safetensors import safe_open  # noqa: E402

with safe_open(str(adir / "adapter_model.safetensors"), "pt") as f:
    keys = list(f.keys())
check("adapter keys in serving (ConditionalGeneration) layout", keys and all(k.startswith(PEFT_SERVE_PREFIX) for k in keys))
check("only lora_A/lora_B tensors", all(".lora_A." in k or ".lora_B." in k for k in keys))
# every LoRA target must name a real weight in the base CHECKPOINT (the layout vLLM maps from)
from huggingface_hub import snapshot_download  # noqa: E402

snap = Path(os.environ["FT_BASE_MODEL"]) if Path(os.environ["FT_BASE_MODEL"]).is_dir() else \
    Path(snapshot_download(os.environ["FT_BASE_MODEL"], allow_patterns=["*.json", "*.safetensors"]))
base_keys = set()
for sf in snap.glob("*.safetensors"):
    with safe_open(str(sf), "pt") as f:
        base_keys |= set(f.keys())
targets = {k.replace("base_model.model.", "", 1).rsplit(".lora_", 1)[0] + ".weight" for k in keys}
check(f"all {len(targets)} LoRA targets exist as checkpoint weights", targets and targets <= base_keys)
acfg = json.loads((adir / "adapter_config.json").read_text())
check("adapter config vLLM-compatible", not acfg.get("modules_to_save") and not acfg.get("use_dora")
      and acfg["bias"] == "none" and acfg["r"] == 8)
check("adapter config has no host path", acfg["base_model_name_or_path"] == "hivemind-base" and str(TMP) not in json.dumps(acfg))

rc = st.get("receipt") or {}
check("receipt binds org + adapter + dataset", rc.get("receipt", {}).get("org") == "lantern-house"
      and rc["receipt"]["adapter_sha256"] == sha and rc["receipt"]["dataset_sha256"] == job["dataset"]["sha256"])
payload = json.dumps(rc.get("receipt"), sort_keys=True, separators=(",", ":")).encode()
import base64  # noqa: E402

try:
    _k.public_key().verify(base64.b64decode(rc["sig_ed25519"]), payload)
    ok = True
except Exception:
    ok = False
check("receipt ed25519 signature verifies", ok)
check("receipt says not exportable", rc["receipt"]["exportable"] is False)
check("receipt has contributor count, not ids", rc["receipt"]["contributors"] == 2 and "alice" not in json.dumps(rc))

la = c.get("/v1/orgs/lantern-house/adapters", headers=hdr("bob")).json()
check("member lists adapters (hash + receipt)", la["adapters"] and la["adapters"][0]["adapter_sha256"] == sha)
check("adapter listing has no contributor ids", "alice" not in json.dumps(la) and la["adapters"][0]["contributors"] == 2)
check("non-member cannot list adapters", c.get("/v1/orgs/lantern-house/adapters", headers=hdr("carol")).status_code == 404)
paths = c.get("/openapi.json").json()["paths"]
tpaths = tc.get("/openapi.json").json()["paths"]
check("no export/download route anywhere",
      not [p for p in list(paths) + list(tpaths) if re.search(r"export|download|weights|/files", p)]
      if (re := __import__("re")) else False)

# --- generate with the org adapter (trainer HF path)
GQ = {"messages": [{"role": "user", "content": "Where is the seed library?"}], "max_tokens": 16}
r = post("bob", "/v1/orgs/lantern-house/generate", GQ)
check("member generate with org adapter -> 200", r.status_code == 200)
g = r.json()
check("generate returns text + adapter hash", isinstance(g.get("text"), str) and g.get("adapter_sha256") == sha)
print("  adapter answer:", repr(g.get("text", "")[:120]))
if os.environ.get("FT_TEST_STRICT") == "1":  # real model: the adapter must have learned the org fact
    check("adapter recalls org fact (north shelf)", "north" in g.get("text", "").lower())
    tok_, base_ = ts.get_base()
    with base_.disable_adapter():
        import torch  # noqa: E402

        p = tok_.apply_chat_template([{"role": "system", "content": ft.org_system("lantern-house")}, *GQ["messages"]],
                                     tokenize=False, add_generation_prompt=True, enable_thinking=False)
        ids = tok_(p, return_tensors="pt", add_special_tokens=False)
        o = base_.generate(**ids, max_new_tokens=16, do_sample=False)
        base_txt = tok_.decode(o[0][ids["input_ids"].shape[1]:], skip_special_tokens=True)
    print("  base answer:   ", repr(base_txt[:120]))
    check("base model alone does not know it", "north shelf" not in base_txt.lower())
check("non-member generate -> 404", post("carol", "/v1/orgs/lantern-house/generate", GQ).status_code == 404)
check("org without adapter -> 404", post("carol", "/v1/orgs/ledger-fund/generate", GQ).status_code == 404)
check("trainer refuses another org's dir name",
      tc.post("/generate", json={"org": "ledger-fund", "adapter_sha256": sha, "messages": GQ["messages"]},
              headers={"Authorization": f"Bearer {ts.internal_token()}"}).status_code == 404)
check("extra fields (model/adapter path) rejected",
      post("bob", "/v1/orgs/lantern-house/generate", {**GQ, "model": "x"}).status_code == 422)

# --- vLLM LoRA serving path (mocked engine): request shape
ft.FT_SERVE = "vllm"
m.LLM_BASE_URL = "http://vllm:8001/v1"
r = post("bob", "/v1/orgs/lantern-house/generate", GQ)
check("vllm-serve generate -> 200", r.status_code == 200)
load = [s for s in VLLM_SENT if s["url"].endswith("/v1/load_lora_adapter")]
chat = [s for s in VLLM_SENT if s["url"].endswith("/chat/completions")]
check("vllm load_lora_adapter called once with org-scoped name + enclave path",
      len(load) == 1 and load[0]["url"] == "http://vllm:8001/v1/load_lora_adapter"
      and load[0]["json"] == {"lora_name": f"org-lantern-house-{sha[:16]}", "lora_path": f"/adapters/lantern-house/{sha}"})
check("chat request names the org LoRA as model", chat and chat[-1]["json"]["model"] == f"org-lantern-house-{sha[:16]}")
check("org system prompt always first", chat[-1]["json"]["messages"][0]["role"] == "system"
      and "Lantern House" in chat[-1]["json"]["messages"][0]["content"])
post("bob", "/v1/orgs/lantern-house/generate", GQ)
check("adapter loaded once, reused", len([s for s in VLLM_SENT if s["url"].endswith("/v1/load_lora_adapter")]) == 1)

# auto mode: engine refuses the LoRA (e.g. unsupported in the pinned image) -> trainer serves
_orig = Router.request


def refusing(self, method, url, json=None, timeout=None, headers=None):
    if url.endswith("/v1/load_lora_adapter"):
        VLLM_SENT.append({"method": method, "url": url, "json": json})
        return httpx.Response(400, request=httpx.Request(method, url), text="LoRA not supported")
    return _orig(self, method, url, json, timeout, headers)


Router.request = refusing
ft._vllm_loaded.clear()
ft.FT_SERVE = "auto"
r = post("bob", "/v1/orgs/lantern-house/generate", GQ)
check("auto: vllm refuses LoRA -> trainer fallback 200", r.status_code == 200 and r.json()["serve"] == "trainer")
ft.FT_SERVE = "vllm"
check("strict vllm: refusal -> 502", post("bob", "/v1/orgs/lantern-house/generate", GQ).status_code == 502)
Router.request = _orig
r = post("bob", "/v1/orgs/lantern-house/generate", GQ)
check("vllm path recovers once engine accepts", r.status_code == 200 and r.json()["serve"] == "vllm")

# --- revoke: alice withdraws personal consent -> adapter that included her is destroyed
r = post("alice", "/v1/orgs/lantern-house/finetune/consent", {"personal": False})
check("withdraw consent -> 200, adapter deleted", r.status_code == 200 and r.json()["adapters_deleted"] == 1)
check("adapter files gone from enclave store", not adir.exists())
check("vllm unload_lora_adapter called", any(s["url"].endswith("/v1/unload_lora_adapter") for s in VLLM_SENT))
check("generate after revoke -> 404", post("bob", "/v1/orgs/lantern-house/generate", GQ).status_code == 404)
check("job marked deleted", c.get(f"/v1/orgs/lantern-house/finetune/{jid}", headers=hdr("alice")).json()["state"] == "deleted")
ds2 = ft.build_dataset("lantern-house")
check("next dataset excludes alice personal", ALICE_CANARY not in json.dumps(ds2["examples"]) and ds2["memories"] == 2)

ft.FT_ENABLED = False
check("FT disabled -> 503", post("alice", "/v1/orgs/lantern-house/finetune").status_code == 503)

print(f"\n{'ALL PASS' if not FAILS else 'FAILED: ' + str(len(FAILS))} ({len(FAILS)} fails)")
sys.exit(1 if FAILS else 0)
