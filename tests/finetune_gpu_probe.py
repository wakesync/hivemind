#!/usr/bin/env python3
"""Live GPU acceptance for private fine-tuning, against a held candidate enclave.

Every request goes through `tinfoil http` (attestation verified per request). Uses a fresh
synthetic org with canary facts; nothing real. Prints status codes, timings, hashes, and the
adapter vs base answers to the canary question only. Never prints the secret.

    SWARM_SECRET=... python3 tests/finetune_gpu_probe.py <host[:port]> <repo[@tag]>
e.g.  ... membrane.shadow-labs.containers.tinfoil.dev:4443 wakesync/hivemind@v0.4.0-ft
"""
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time

HOST, REPO = sys.argv[1], sys.argv[2]
SECRET = os.environ["SWARM_SECRET"]
ORG = f"ft-probe-{int(time.time()) % 10**8}"
ADMIN = f"ft-probe-admin-{ORG[-6:]}"
FACT = "The probe garden keeps its spare keys inside the blue heron birdhouse by the east gate."
Q = "Where does the probe garden keep its spare keys?"
FAILS = []


def tf(method, path, user=None, body=None, timeout=300):
    cmd = ["tinfoil", "http", method, f"https://{HOST}{path}", "--enclave", HOST, "--repo", REPO]
    if user:
        ts = str(int(time.time()))
        sig = hmac.new(SECRET.encode(), f"{user}.{ts}".encode(), hashlib.sha256).hexdigest()
        cmd += ["-H", f"X-Swarm-User: {user}", "-H", f"X-Swarm-Ts: {ts}", "-H", f"X-Swarm-Sig: {sig}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "--body", json.dumps(body)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    out = ((p.stdout or "") + (p.stderr or "")).replace(SECRET, "<redacted>")
    i = out.find("{")
    try:
        return p.returncode, json.loads(out[i:]) if i >= 0 else {"raw": out[-400:]}
    except Exception:
        return p.returncode, {"raw": out[-400:]}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""), flush=True)
    if not cond:
        FAILS.append(name)


rc, h = tf("get", "/healthz")
check("health", rc == 0 and h.get("ok"), json.dumps(h)[:200])
rc, j = tf("post", "/orgs", ADMIN, {"id": ORG, "name": "Probe Garden", "wall": "sealed"})
check("org created", rc == 0 and j.get("org") == ORG)
for t in (FACT, "Probe garden volunteers water the tomatoes every Thursday at dusk.",
          "The probe garden compost bins are turned on the first Sunday of each month."):
    tf("post", "/remember", ADMIN, {"text": t, "org": ORG})
tf("post", f"/v1/orgs/{ORG}/finetune/policy", ADMIN, {"org_memories": True})
rc, base = tf("post", "/v1/generate", ADMIN, {"messages": [{"role": "user", "content": Q}], "max_tokens": 64})
print("  base answer:", repr((base.get("text") or "")[:160]))
t0 = time.time()
rc, job = tf("post", f"/v1/orgs/{ORG}/finetune", ADMIN, {"r": 16, "epochs": 8, "max_steps": 40, "lr": 2e-4,
                                                          "grad_accum": 1, "max_len": 512})
check("job accepted", rc == 0 and job.get("state") == "queued", json.dumps(job)[:300])
jid = job.get("job_id")
st = {}
while jid and time.time() - t0 < 3600:
    rc, st = tf("get", f"/v1/orgs/{ORG}/finetune/{jid}", ADMIN)
    print(f"  t+{time.time() - t0:.0f}s state={st.get('state')} step={st.get('step')}/{st.get('total')} "
          f"loss={st.get('loss')}", flush=True)
    if st.get("state") in ("done", "failed", "lost"):
        break
    time.sleep(30)
check("job done", st.get("state") == "done", json.dumps(st.get("result"))[:400])
rcpt = (st.get("receipt") or {})
check("receipt signed by attested key", bool(rcpt.get("sig_ed25519")), rcpt.get("canonical_sha256", ""))
rc, g = tf("post", f"/v1/orgs/{ORG}/generate", ADMIN, {"messages": [{"role": "user", "content": Q}],
                                                       "max_tokens": 64})
print("  adapter answer:", repr((g.get("text") or "")[:160]), "serve=", g.get("serve"))
check("org generate ok", rc == 0 and g.get("adapter_sha256") == st.get("adapter_sha256"))
check("served by vLLM LoRA (not fallback)", g.get("serve") == "vllm")
check("adapter learned the fact (heron)", "heron" in (g.get("text") or "").lower())
check("quality not degenerate", not (g.get("quality") or {}).get("degenerate", True))
rc, o = tf("post", f"/v1/orgs/{ORG}/generate", "ft-probe-outsider", {"messages": [{"role": "user", "content": Q}]})
check("non-member blocked", "no such org" in json.dumps(o))
rc, s = tf("post", "/v1/generate", ADMIN, {"messages": [{"role": "user", "content": "Say hello in one sentence."}],
                                            "max_tokens": 32})
check("base /v1/generate still healthy", not (s.get("quality") or {}).get("degenerate", True), repr(s.get("text"))[:80])
# `tinfoil http` has no DELETE; turning the org policy off deletes adapters built from org memories
rc, d = tf("post", f"/v1/orgs/{ORG}/finetune/policy", ADMIN, {"org_memories": False})
check("adapter deleted on consent off", d.get("adapters_deleted") == 1)
rc, g2 = tf("post", f"/v1/orgs/{ORG}/generate", ADMIN, {"messages": [{"role": "user", "content": Q}]})
check("generate after delete -> no adapter", "no adapter" in json.dumps(g2))
print(f"total {time.time() - t0:.0f}s;", "ALL PASS" if not FAILS else f"FAILED {FAILS}")
sys.exit(1 if FAILS else 0)
