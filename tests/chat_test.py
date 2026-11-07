#!/usr/bin/env python3
"""Unit tests for the OpenAI-compatible POST /v1/chat/completions (no GPU needed).

    python3 tests/chat_test.py
"""
import hashlib
import hmac
import json
import os
import sys
import threading
import time

os.environ.setdefault("SWARM_SECRET", "unit-test-secret")
os.environ.setdefault("DB_PATH", "/tmp/hivemind-chat-test.db")
os.environ.setdefault("LLM_MODEL", "qwen3.8-27b")
os.environ.setdefault("ABL_BASE_URL", "http://vllm-abl:8002/v1")
os.environ.setdefault("ABL_MODEL", "qwen3.8-27b-abliterated")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main as m  # noqa: E402

SECRET = os.environ["SWARM_SECRET"]
FAILS, SENT = [], []
REPLY = {"content": "<think>\n\n</think>\n\nPlain sentences from the enclave.", "finish": "stop"}
GATE = threading.Event()
GATE.set()


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def fake_post(url, json=None, headers=None, timeout=None, **kw):
    SENT.append({"url": url, "json": json})
    GATE.wait(10)
    return httpx.Response(200, request=httpx.Request("POST", url), json={
        "choices": [{"message": {"role": "assistant", "content": REPLY["content"]}, "finish_reason": REPLY["finish"]}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 16}})


m.httpx.post = fake_post
c = TestClient(m.app)


def hdr(user="pool:test", secret=SECRET, ts=None):
    ts = str(int(time.time()) if ts is None else ts)
    sig = hmac.new(secret.encode(), f"{user}.{ts}".encode(), hashlib.sha256).hexdigest()
    return {"X-Swarm-User": user, "X-Swarm-Ts": ts, "X-Swarm-Sig": sig}


ABL = {"model": "qwen3.8-27b-abliterated", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 32}

check("no auth -> 422", c.post("/v1/chat/completions", json=ABL).status_code == 422)
check("bad sig -> 401", c.post("/v1/chat/completions", json=ABL, headers=hdr(secret="x")).status_code == 401)
check("models needs auth", c.get("/v1/models").status_code == 422)
ids = [d["id"] for d in c.get("/v1/models", headers=hdr()).json()["data"]]
check("models lists stock + abliterated", sorted(ids) == ["qwen3.8-27b", "qwen3.8-27b-abliterated"])
check("no upstream call on auth failure", not SENT)

r = c.post("/v1/chat/completions", json={**ABL, "user": "x", "logprobs": True, "base_url": "http://evil"}, headers=hdr())
j = r.json()
check("abl 200", r.status_code == 200)
check("openai shape", j["object"] == "chat.completion" and j["choices"][0]["message"]["role"] == "assistant")
check("think stripped", j["choices"][0]["message"]["content"] == "Plain sentences from the enclave.")
check("usage surfaced", j["usage"]["total_tokens"] == 16)
up = SENT[-1]
check("abl upstream fixed", up["url"] == "http://vllm-abl:8002/v1/chat/completions")
check("unknown fields not forwarded", not ({"user", "logprobs", "base_url"} & set(up["json"])))
check("qwen non-thinking applied", up["json"]["chat_template_kwargs"] == {"enable_thinking": False})

c.post("/v1/chat/completions", json={**ABL, "model": "qwen3.8-27b", "temperature": 0.1}, headers=hdr())
check("stock upstream fixed", SENT[-1]["url"] == "http://vllm:8001/v1/chat/completions" and SENT[-1]["json"]["model"] == "qwen3.8-27b")
check("caller temperature honored", SENT[-1]["json"]["temperature"] == 0.1)

n = len(SENT)
check("unknown model -> 404", c.post("/v1/chat/completions", json={**ABL, "model": "gpt-4o"}, headers=hdr()).status_code == 404)
check("tools rejected", c.post("/v1/chat/completions", json={**ABL, "tools": [{"type": "function"}]}, headers=hdr()).status_code == 400)
check("image part rejected", c.post("/v1/chat/completions", json={**ABL, "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]}, headers=hdr()).status_code == 422)
check("tool role rejected", c.post("/v1/chat/completions", json={**ABL, "messages": [{"role": "tool", "content": "x"}]}, headers=hdr()).status_code == 422)
check("max_tokens over cap -> 413", c.post("/v1/chat/completions", json={**ABL, "max_tokens": 4096}, headers=hdr()).status_code == 413)
check("prompt over cap -> 413", c.post("/v1/chat/completions", json={**ABL, "messages": [{"role": "user", "content": "a" * 9000}] * 2}, headers=hdr()).status_code == 413)
check("no upstream on rejects", len(SENT) == n)

r = c.post("/v1/chat/completions", json={**ABL, "messages": [{"role": "developer", "content": [{"type": "text", "text": "be brief"}]}, {"role": "user", "content": "hi"}]}, headers=hdr())
check("text parts + developer role ok", r.status_code == 200 and SENT[-1]["json"]["messages"][0] == {"role": "system", "content": "be brief"})

r = c.post("/v1/chat/completions", json={**ABL, "stream": True}, headers=hdr())
lines = [l for l in r.text.split("\n") if l.startswith("data: ")]
check("stream SSE ends with DONE", r.status_code == 200 and lines[-1] == "data: [DONE]")
check("stream carries text", json.loads(lines[0][6:])["choices"][0]["delta"]["content"] == "Plain sentences from the enclave.")

GATE.clear()
res = {}
t = threading.Thread(target=lambda: res.setdefault("a", c.post("/v1/chat/completions", json=ABL, headers=hdr()).status_code))
t.start()
time.sleep(0.5)
busy = c.post("/v1/chat/completions", json=ABL, headers=hdr()).status_code
other = {}
t2 = threading.Thread(target=lambda: other.setdefault("s", c.post("/v1/chat/completions", json={**ABL, "model": "qwen3.8-27b"}, headers=hdr()).status_code))
t2.start()
time.sleep(0.5)
GATE.set()
t.join(); t2.join()
check("same model concurrent -> 429", busy == 429 and res.get("a") == 200)
check("other engine not blocked", other.get("s") == 200)

with m._lock:
    check("chat persists nothing", m.conn().execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0)


def boom(*a, **k):
    raise httpx.ConnectError("down")


m.httpx.post = boom
check("upstream down -> 502", c.post("/v1/chat/completions", json=ABL, headers=hdr()).status_code == 502)
check("healthz lists chat models", c.get("/healthz").json()["chat_models"] == ["qwen3.8-27b", "qwen3.8-27b-abliterated"])

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAIL'}")
sys.exit(1 if FAILS else 0)
