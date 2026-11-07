#!/usr/bin/env python3
"""Unit tests for sampling params + POST /v1/generate (no GPU needed).

Mocks the in-enclave vLLM with httpx.MockTransport-style monkeypatch and checks
the exact upstream request shape, auth gating, bounds, and one-in-flight.

    SWARM_SECRET=test DB_PATH=/tmp/gen-test.db python3 tests/generate_test.py
"""
import hashlib
import hmac
import os
import sys
import threading
import time

os.environ.setdefault("SWARM_SECRET", "unit-test-secret")
os.environ.setdefault("DB_PATH", "/tmp/hivemind-generate-test.db")
os.environ.setdefault("LLM_MODEL", "qwen3.8-27b")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main as m  # noqa: E402

SECRET = os.environ["SWARM_SECRET"]
FAILS = []
SENT = []
REPLY = {"content": "The enclave answered in plain English sentences.", "finish": "stop"}
GATE = threading.Event()
GATE.set()


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def fake_post(url, json=None, headers=None, timeout=None, **kw):
    SENT.append({"url": url, "json": json})
    GATE.wait(10)
    req = httpx.Request("POST", url)
    return httpx.Response(200, request=req, json={
        "choices": [{"message": {"role": "assistant", "content": REPLY["content"]},
                     "finish_reason": REPLY["finish"]}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 9, "total_tokens": 19}})


m.httpx.post = fake_post
c = TestClient(m.app)


def hdr(user="juno", secret=SECRET, ts=None):
    ts = str(int(time.time()) if ts is None else ts)
    sig = hmac.new(secret.encode(), f"{user}.{ts}".encode(), hashlib.sha256).hexdigest()
    return {"X-Swarm-User": user, "X-Swarm-Ts": ts, "X-Swarm-Sig": sig}


MSG = {"messages": [{"role": "user", "content": "Say hello in one sentence."}], "max_tokens": 64}

# --- sampling shape
p = m.sampling_params()
check("recommended temperature .7", p["temperature"] == 0.7)
check("recommended top_p .8", p["top_p"] == 0.8)
check("recommended top_k 20 int", p["top_k"] == 20 and isinstance(p["top_k"], int))
check("recommended min_p 0", p["min_p"] == 0.0)
check("recommended presence_penalty 1.5", p["presence_penalty"] == 1.5)
check("recommended repetition_penalty 1.0", p["repetition_penalty"] == 1.0)
check("qwen thinking disabled", p["chat_template_kwargs"] == {"enable_thinking": False})
lp = m.sampling_params("legacy")
check("legacy preset = temp .2 only", lp["temperature"] == 0.2 and "presence_penalty" not in lp)

# --- auth
check("no auth -> 422", c.post("/v1/generate", json=MSG).status_code == 422)
check("bad sig -> 401", c.post("/v1/generate", json=MSG, headers=hdr(secret="wrong")).status_code == 401)
check("stale ts -> 401", c.post("/v1/generate", json=MSG, headers=hdr(ts=time.time() - 3600)).status_code == 401)
check("no upstream call on auth failure", len(SENT) == 0)

# --- happy path + upstream shape
r = c.post("/v1/generate", json=MSG, headers=hdr())
j = r.json()
check("ok 200", r.status_code == 200)
check("text returned", j.get("text") == REPLY["content"])
check("finish_reason surfaced", j.get("finish_reason") == "stop")
check("quality non-degenerate", j["quality"]["degenerate"] is False and j["quality"]["letter_ratio"] > 0.7)
up = SENT[-1]
check("upstream is closed in-enclave vllm", up["url"] == "http://vllm:8001/v1/chat/completions")
check("upstream model fixed", up["json"]["model"] == "qwen3.8-27b")
check("upstream sampling recommended", up["json"]["presence_penalty"] == 1.5 and up["json"]["top_k"] == 20)
check("upstream max_tokens bounded", up["json"]["max_tokens"] == 64)
r = c.post("/v1/generate", json={**MSG, "sampling": "legacy"}, headers=hdr())
check("legacy A/B preset accepted", r.status_code == 200 and SENT[-1]["json"]["temperature"] == 0.2)

# --- bounds / no destination override
check("extra field (model) rejected", c.post("/v1/generate", json={**MSG, "model": "x"}, headers=hdr()).status_code == 422)
check("extra field (base_url) rejected", c.post("/v1/generate", json={**MSG, "base_url": "http://evil"}, headers=hdr()).status_code == 422)
check("tool role rejected", c.post("/v1/generate", json={"messages": [{"role": "tool", "content": "x"}]}, headers=hdr()).status_code == 422)
check("max_tokens over cap -> 413", c.post("/v1/generate", json={**MSG, "max_tokens": 2048}, headers=hdr()).status_code == 413)
big = {"messages": [{"role": "user", "content": "a" * 9000}, {"role": "user", "content": "b" * 9000}]}
check("prompt over char cap -> 413", c.post("/v1/generate", json=big, headers=hdr()).status_code == 413)
many = {"messages": [{"role": "user", "content": "hi"}] * 17}
check("too many messages -> 413", c.post("/v1/generate", json=many, headers=hdr()).status_code == 413)

# --- one in flight
GATE.clear()
res = {}
t = threading.Thread(target=lambda: res.setdefault("a", c.post("/v1/generate", json=MSG, headers=hdr()).status_code))
t.start()
time.sleep(0.5)
busy = c.post("/v1/generate", json=MSG, headers=hdr()).status_code
GATE.set()
t.join()
check("second concurrent request -> 429", busy == 429)
check("first concurrent request -> 200", res.get("a") == 200)

# --- degenerate detection surfaces honestly
REPLY["content"] = "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" * 10
REPLY["finish"] = "length"
j = c.post("/v1/generate", json=MSG, headers=hdr()).json()
check("degenerate flagged on generate", j["quality"]["degenerate"] is True and j["finish_reason"] == "length")
check("recall-path llm_answer rejects degenerate", m.llm_answer("q", [{"text": "note"}]) is None)
REPLY["content"] = "<think>\n\n</think>\n\nHello there, friend."
check("empty think block stripped", m.strip_think(REPLY["content"]) == "Hello there, friend.")

# --- nothing persisted
with m._lock:
    n = m.conn().execute("SELECT COUNT(*) FROM memories").fetchone()[0]
check("generate persists nothing", n == 0)

# --- upstream failure
def boom(*a, **k):
    raise httpx.ConnectError("down")
m.httpx.post = boom
check("upstream down -> 502", c.post("/v1/generate", json=MSG, headers=hdr()).status_code == 502)

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAIL'}")
sys.exit(1 if FAILS else 0)
