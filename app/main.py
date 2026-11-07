"""swarm-memory: confidential shared memory for multiplayer agents.

Runs inside a Tinfoil enclave. Plaintext memories, embeddings and answer
synthesis all stay inside the enclave boundary:
  - embeddings: local ONNX MiniLM baked into the image
  - storage: sqlite on in-enclave memory (or encrypted volume)
  - synthesis: in-enclave vLLM container (LLM_BASE_URL), extractive fallback

Callers never get another scope's raw records back. /recall returns a
synthesized answer + contributor COUNTS. /graph returns topics + co-occurrence
with no per-person labels, and hides topics with fewer than GRAPH_K contributors.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from itertools import combinations
from pathlib import Path

import httpx
import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------- config
SWARM_SECRET = os.environ.get("SWARM_SECRET", "")
DB_PATH = os.environ.get("DB_PATH", "/data/swarm.db")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
EMBED_CACHE = os.environ.get("EMBED_CACHE", "/models")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://vllm:8001/v1").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3.8-27b")
LLM_TEMPERATURE = os.environ.get("LLM_TEMPERATURE", "0.2")
LLM_EXTRA_BODY = os.environ.get("LLM_EXTRA_BODY", "")  # JSON merged into chat body
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "60"))
LLM_ENABLED = os.environ.get("LLM_ENABLED", "1") not in ("0", "false", "")
MEMORY_BACKEND = os.environ.get("MEMORY_BACKEND", "sqlite")
GRAPH_K = int(os.environ.get("GRAPH_K", "2"))  # k-anonymity floor for topics
RECALL_K = int(os.environ.get("RECALL_K", "8"))
RECALL_MIN_SIM = float(os.environ.get("RECALL_MIN_SIM", "0.15"))
SIG_MAX_SKEW = int(os.environ.get("SIG_MAX_SKEW", "300"))
STRIPE_KEY = os.environ.get("STRIPE_KEY", "")
if not STRIPE_KEY.startswith(("sk_", "rk_")):
    STRIPE_KEY = ""  # "none"/placeholder -> stub ledger
WEB_DIR = Path(os.environ.get("WEB_DIR", Path(__file__).resolve().parent.parent / "web"))
TINFOIL_DIR = Path("/tinfoil")

app = FastAPI(title="swarm-memory", version="0.1.0")
_lock = threading.Lock()

# ---------------------------------------------------------------- embeddings
_embedder = None


def embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding

        try:
            _embedder = TextEmbedding(EMBED_MODEL, cache_dir=EMBED_CACHE, local_files_only=True)
        except Exception:
            # dev convenience only; inside the enclave the model is baked in
            _embedder = TextEmbedding(EMBED_MODEL, cache_dir=EMBED_CACHE)
    return _embedder


def embed(texts: list[str]) -> np.ndarray:
    vecs = np.array(list(embedder().embed(texts)), dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
    return vecs


# ---------------------------------------------------------------- storage
def db() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.execute(
        """CREATE TABLE IF NOT EXISTS memories(
             id INTEGER PRIMARY KEY, scope TEXT NOT NULL, text TEXT NOT NULL,
             topics TEXT NOT NULL, emb BLOB NOT NULL, created REAL NOT NULL)"""
    )
    return c


_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = db()
    return _conn


# ---------------------------------------------------------------- auth
def scope_from_headers(
    x_swarm_user: str = Header(..., description="agent / user id"),
    x_swarm_ts: str = Header(..., description="unix seconds"),
    x_swarm_sig: str = Header(..., description="hex HMAC-SHA256(SWARM_SECRET, user + '.' + ts)"),
) -> str:
    if not SWARM_SECRET:
        raise HTTPException(503, "SWARM_SECRET not configured")
    try:
        ts = int(x_swarm_ts)
    except ValueError:
        raise HTTPException(401, "bad ts")
    if abs(time.time() - ts) > SIG_MAX_SKEW:
        raise HTTPException(401, "stale signature")
    want = hmac.new(SWARM_SECRET.encode(), f"{x_swarm_user}.{ts}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, x_swarm_sig.lower()):
        raise HTTPException(401, "bad signature")
    if not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,64}", x_swarm_user):
        raise HTTPException(400, "bad user id")
    return x_swarm_user


# ---------------------------------------------------------------- topics
STOP = set(
    """a about above after again against all also am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had has have
having he her here hers herself him himself his how i if in into is it its itself just let me more most my
myself no nor not now of off on once only or other our ours ourselves out over own same she should so some
such than that the their theirs them themselves then there these they this those through to too under until
up very was we were what when where which while who whom why will with would you your yours yourself
yourselves via per get got going gonna want wants need needs like likes think thinks know knows said says
really maybe probably still yeah ok okay thing things stuff lot lots one two new next last week today tomorrow
tonight yesterday morning evening night day days time times use used using make makes made around
people someone everyone something anything nothing every much many well good great""".split()
)
WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#\-]{2,}")


def extract_topics(text: str, n: int = 6) -> list[str]:
    toks = [t.lower().strip("-") for t in WORD.findall(text)]
    toks = [t for t in toks if t not in STOP and len(t) > 2]
    # light plural folding so "agents"/"agent" share a node
    toks = [t[:-1] if t.endswith("s") and not t.endswith("ss") and len(t) > 4 else t for t in toks]
    return [w for w, _ in Counter(toks).most_common(n)]


# ---------------------------------------------------------------- privacy scrub
PII = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[email]"),
    (re.compile(r"https?://\S+"), "[link]"),
    (re.compile(r"@\w{2,}"), "[handle]"),
    (re.compile(r"\+?\d[\d\s().-]{7,}\d"), "[number]"),
    (re.compile(r"\b(sk|rk|pk)_(live|test)_\w+"), "[key]"),
]


def scrub(s: str) -> str:
    for rx, rep in PII:
        s = rx.sub(rep, s)
    return s


# ---------------------------------------------------------------- models
class RememberIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=8000)
    scope: str | None = Field(None, description="optional; must equal the signed X-Swarm-User")


class RecallIn(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)


# ---------------------------------------------------------------- backends
class SqliteBackend:
    name = "sqlite"

    def remember(self, scope: str, text: str) -> dict:
        topics = extract_topics(text)
        v = embed([text])[0]
        with _lock:
            cur = conn().execute(
                "INSERT INTO memories(scope,text,topics,emb,created) VALUES(?,?,?,?,?)",
                (scope, text, json.dumps(topics), v.tobytes(), time.time()),
            )
            conn().commit()
        return {"id": cur.lastrowid, "topics": topics}

    def search(self, query: str, k: int) -> list[dict]:
        with _lock:
            rows = conn().execute("SELECT id, scope, text, emb FROM memories").fetchall()
        if not rows:
            return []
        q = embed([query])[0]
        mat = np.stack([np.frombuffer(r[3], dtype=np.float32) for r in rows])
        sims = mat @ q
        order = np.argsort(-sims)[:k]
        return [
            {"id": rows[i][0], "scope": rows[i][1], "text": rows[i][2], "sim": float(sims[i])}
            for i in order
            if sims[i] >= RECALL_MIN_SIM
        ]

    def all_topics(self) -> list[tuple[str, list[str]]]:
        with _lock:
            rows = conn().execute("SELECT scope, topics FROM memories").fetchall()
        return [(r[0], json.loads(r[1])) for r in rows]

    def stats(self) -> dict:
        with _lock:
            n, s = conn().execute("SELECT COUNT(*), COUNT(DISTINCT scope) FROM memories").fetchone()
        return {"memories": n, "scopes": s}


def make_backend():
    if MEMORY_BACKEND == "cognee":
        try:
            from app.cognee_backend import CogneeBackend  # noqa: WPS433

            return CogneeBackend()
        except Exception as e:  # pragma: no cover
            print(f"[swarm-memory] cognee backend unavailable ({e!r}); falling back to sqlite")
    return SqliteBackend()


backend = make_backend()

# ---------------------------------------------------------------- synthesis
SYSTEM = (
    "You are the shared memory of a swarm of agents. You are given private notes contributed by "
    "several different members. Answer the question by COMBINING facts across notes. Rules: "
    "never quote notes verbatim, never say who said what, never reveal names, emails, handles, "
    "phone numbers, keys or links. Paraphrase into one short, direct answer (max 5 sentences). "
    "If the notes don't answer it, say so."
)


def llm_answer(query: str, hits: list[dict]) -> str | None:
    if not LLM_ENABLED or not LLM_BASE_URL:
        return None
    notes = "\n".join(f"- {h['text']}" for h in hits)
    headers = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}
    body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"NOTES:\n{notes}\n\nQUESTION: {query}"},
        ],
        "max_tokens": 1200,
    }
    if LLM_TEMPERATURE:
        body["temperature"] = float(LLM_TEMPERATURE)
    if "gpt-oss" in LLM_MODEL:
        body["reasoning_effort"] = "low"
    elif "qwen" in LLM_MODEL.lower():
        body["chat_template_kwargs"] = {"enable_thinking": False}  # vLLM: skip <think>
    if LLM_EXTRA_BODY:
        body.update(json.loads(LLM_EXTRA_BODY))
    try:
        r = httpx.post(f"{LLM_BASE_URL}/chat/completions", json=body, headers=headers, timeout=LLM_TIMEOUT)
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        out = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
        return scrub(out) if out else None
    except Exception as e:
        print(f"[swarm-memory] llm unavailable: {type(e).__name__}")
        return None


SENT = re.compile(r"(?<=[.!?])\s+")


def extractive_answer(query: str, hits: list[dict]) -> str:
    """Fallback when no in-enclave LLM: best-matching SENTENCES (not records),
    de-attributed, PII-scrubbed, first person neutralised."""
    sents = []
    for h in hits:
        for s in SENT.split(h["text"]):
            s = s.strip()
            if len(s) > 12:
                sents.append(s)
    if not sents:
        return "nothing in shared memory matches that yet."
    v = embed(sents)
    q = embed([query])[0]
    sims = v @ q
    best = [sents[i] for i in np.argsort(-sims)[:5] if sims[i] > 0.1] or [sents[int(np.argmax(sims))]]
    pron = {"I": "they", "I'm": "they're", "I've": "they've", "I'll": "they'll", "I'd": "they'd",
            "my": "their", "My": "Their", "me": "them", "mine": "theirs"}
    neutral = []
    for s in best:
        s = re.sub(r"\b(I'm|I've|I'll|I'd|I|My|my|me|mine)\b", lambda m: pron[m.group(0)], s)
        s = s[0].upper() + s[1:]
        neutral.append(scrub(s if s.endswith((".", "!", "?")) else s + "."))
    return " ".join(neutral)


# ---------------------------------------------------------------- routes
@app.get("/healthz")
def healthz():
    return {"ok": True, "backend": backend.name, **backend.stats(), "llm": LLM_MODEL if LLM_ENABLED else None}


@app.post("/remember")
def remember(body: RememberIn, scope: str = Depends(scope_from_headers)):
    if body.scope and body.scope != scope:
        raise HTTPException(403, "scope must match signed X-Swarm-User")
    out = backend.remember(scope, body.text)
    return {"stored": True, "scope": scope, "topics": out["topics"]}


@app.post("/recall")
def recall(body: RecallIn, scope: str = Depends(scope_from_headers)):
    hits = backend.search(body.query, RECALL_K)
    scopes = {h["scope"] for h in hits}
    own = sum(1 for h in hits if h["scope"] == scope)
    answer, mode = None, "empty"
    if hits:
        answer = llm_answer(body.query, hits)
        mode = "llm" if answer else "extractive"
        if not answer:
            answer = extractive_answer(body.query, hits)
    else:
        answer = "nothing in shared memory matches that yet."
    return {
        "answer": answer,
        "mode": mode,
        "contributors": {
            "scopes": len(scopes),  # how many distinct members' memories informed this
            "other_scopes": len(scopes - {scope}),
            "memories": len(hits),
            "own_memories": own,
        },
    }


@app.get("/graph")
def graph():
    node_mem: Counter = Counter()
    node_scopes: dict[str, set] = {}
    edges: Counter = Counter()
    for sc, topics in backend.all_topics():
        for t in topics:
            node_mem[t] += 1
            node_scopes.setdefault(t, set()).add(sc)
        for a, b in combinations(sorted(set(topics)), 2):
            edges[(a, b)] += 1
    visible = {t for t, s in node_scopes.items() if len(s) >= GRAPH_K}
    nodes = [
        {"id": t, "weight": node_mem[t], "contributors": len(node_scopes[t])}
        for t in sorted(visible, key=lambda t: -node_mem[t])[:120]
    ]
    keep = {n["id"] for n in nodes}
    links = [
        {"source": a, "target": b, "weight": w}
        for (a, b), w in edges.most_common(400)
        if a in keep and b in keep
    ]
    st = backend.stats()
    return {
        "nodes": nodes,
        "links": links,
        "k": GRAPH_K,
        "hidden_topics": len(node_scopes) - len(visible),
        "memories": st["memories"],
        "members": st["scopes"],
    }


@app.get("/attestation")
def attestation():
    """Badge data. Inside a Tinfoil enclave /tinfoil is a read-only attested mount."""
    att = TINFOIL_DIR / "attestation.json"
    cfg = TINFOIL_DIR / "config.yml"
    try:
        raw = att.read_bytes() if att.exists() else None
    except OSError:
        raw = None
    if raw is not None:
        digest = hashlib.sha256(raw).hexdigest()
        cvm = None
        if cfg.exists() and os.access(cfg, os.R_OK):
            m = re.search(r"cvm-version:\s*\"?([\w.\-]+)", cfg.read_text())
            cvm = m.group(1) if m else None
        return {"enclave": True, "attestation_sha256": digest, "cvm_version": cvm,
                "verify": "tinfoil attest <your-instance-domain>  (or any Tinfoil SecureClient)"}
    return {"enclave": False, "note": "not running in a Tinfoil enclave (local/dev)"}


# --- Track 01: revenue ledger. Read-only Stripe charges, aggregates only (no customer PII).
_ledger_cache: dict = {"t": 0, "v": None}

STUB_CHARGES = [
    {"amount": 900, "currency": "usd", "status": "succeeded", "created": 1762531200},
    {"amount": 2900, "currency": "usd", "status": "succeeded", "created": 1762534800},
    {"amount": 900, "currency": "usd", "status": "succeeded", "created": 1762538400},
    {"amount": 4900, "currency": "usd", "status": "succeeded", "created": 1762542000},
]


@app.get("/ledger")
def ledger():
    now = time.time()
    if _ledger_cache["v"] and now - _ledger_cache["t"] < 30:
        return _ledger_cache["v"]
    source, charges, err = "stub", STUB_CHARGES, None
    if STRIPE_KEY:
        try:
            r = httpx.get("https://api.stripe.com/v1/charges", params={"limit": 100},
                          auth=(STRIPE_KEY, ""), timeout=10)
            r.raise_for_status()
            charges = [{k: c.get(k) for k in ("amount", "currency", "status", "created")}
                       for c in r.json().get("data", [])]
            source = "stripe"
        except Exception as e:
            err = type(e).__name__
    ok = [c for c in charges if c["status"] == "succeeded"]
    by_cur: Counter = Counter()
    for c in ok:
        by_cur[c["currency"]] += c["amount"]
    out = {
        "source": source,
        "error": err,
        "charges": len(ok),
        "gross": {cur: amt / 100 for cur, amt in by_cur.items()},
        "recent": sorted(ok, key=lambda c: -c["created"])[:10],
    }
    _ledger_cache.update(t=now, v=out)
    return out


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "graph.html")


@app.get("/graph.html")
def graph_html():
    return FileResponse(WEB_DIR / "graph.html")


@app.exception_handler(sqlite3.Error)
def _sqlerr(_, exc):
    return JSONResponse({"error": "storage"}, status_code=500)
