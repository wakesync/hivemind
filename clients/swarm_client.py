"""Zero-dependency Python client for swarm-memory (stdlib only).

    from swarm_client import Swarm
    s = Swarm()                       # SWARM_URL, SWARM_SECRET, SWARM_USER from env
    s.remember("standup moved to 10am")
    s.recall("when is standup?")      # -> {"answer": ..., "contributors": {...}}

Tool calling (OpenAI-compatible): load clients/tools.json as `tools=`, then
route tool calls with `s.dispatch(name, arguments_json)`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.request


class Swarm:
    def __init__(self, url: str | None = None, secret: str | None = None, user: str | None = None):
        self.url = (url or os.environ.get("SWARM_URL", "http://localhost:8080")).rstrip("/")
        self.secret = secret or os.environ["SWARM_SECRET"]
        self.user = user or os.environ.get("SWARM_USER", "agent")

    def _headers(self) -> dict:
        ts = str(int(time.time()))
        sig = hmac.new(self.secret.encode(), f"{self.user}.{ts}".encode(), hashlib.sha256).hexdigest()
        return {"X-Swarm-User": self.user, "X-Swarm-Ts": ts, "X-Swarm-Sig": sig,
                "Content-Type": "application/json"}

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                     headers=self._headers(), method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())

    def remember(self, text: str, org: str | None = None) -> dict:
        """org=None -> personal memory (only you can ever recall it)."""
        return self._post("/remember", {"text": text, **({"org": org} if org else {})})

    def recall(self, query: str) -> dict:
        return self._post("/recall", {"query": query})

    def dispatch(self, name: str, arguments: str | dict) -> dict:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        if name == "swarm_remember":
            return self.remember(args["text"], args.get("org"))
        if name == "swarm_recall":
            return self.recall(args["query"])
        raise ValueError(f"unknown tool {name}")
