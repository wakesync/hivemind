#!/usr/bin/env python3
"""Image smoke test (memory-api image, no trainer, no GPU): finetune routes are wired,
inert when FT_ENABLED=0, consent/policy gates work, and nothing is exported.

    docker run --rm -e SWARM_SECRET=x [-e FT_ENABLED=1] <image> python tests/finetune_smoke.py
"""
import hashlib
import hmac
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient  # noqa: E402

from app import main as m  # noqa: E402

c = TestClient(m.app)
FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def hdr(u):
    t = str(int(time.time()))
    return {"X-Swarm-User": u, "X-Swarm-Ts": t,
            "X-Swarm-Sig": hmac.new(m.SWARM_SECRET.encode(), f"{u}.{t}".encode(), hashlib.sha256).hexdigest()}


paths = c.get("/openapi.json").json()["paths"]
for p in ("/v1/orgs/{org}/finetune", "/v1/orgs/{org}/finetune/{job_id}", "/v1/orgs/{org}/generate",
          "/v1/orgs/{org}/adapters", "/v1/orgs/{org}/finetune/consent", "/v1/orgs/{org}/finetune/policy"):
    check(f"route {p}", p in paths)
check("no export route", not [p for p in paths if any(w in p for w in ("export", "download", "weights"))])
check("healthz ok", c.get("/healthz").json()["ok"])
c.post("/orgs", json={"id": "smoke-org", "name": "Smoke"}, headers=hdr("a"))
check("policy set", c.post("/v1/orgs/smoke-org/finetune/policy", json={"org_memories": True},
                           headers=hdr("a")).status_code == 200)
r = c.post("/v1/orgs/smoke-org/finetune", headers=hdr("a"))
if os.environ.get("FT_ENABLED", "0") in ("0", ""):
    check("FT_ENABLED=0 -> 503 (inert)", r.status_code == 503)
else:
    check("FT on, no memories -> 409", r.status_code == 409)
print("ALL PASS" if not FAILS else f"FAILED {len(FAILS)}")
sys.exit(1 if FAILS else 0)
