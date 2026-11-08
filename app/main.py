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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------- config
SWARM_SECRET = os.environ.get("SWARM_SECRET", "")
DB_PATH = os.environ.get("DB_PATH", "/data/swarm.db")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
EMBED_CACHE = os.environ.get("EMBED_CACHE", "/models")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://vllm:8001/v1").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3.8-27b")
# Sampling: Qwen3.8-27B model card, NON-thinking recommendation
# (temperature .7, top_p .8, top_k 20, min_p 0, presence_penalty 1.5, repetition_penalty 1.0).
# v0.1.0 forced temperature .2 with no penalties; /recall returned degenerate output.
LLM_TEMPERATURE = os.environ.get("LLM_TEMPERATURE", "0.7")
LLM_TOP_P = os.environ.get("LLM_TOP_P", "0.8")
LLM_TOP_K = os.environ.get("LLM_TOP_K", "20")
LLM_MIN_P = os.environ.get("LLM_MIN_P", "0")
LLM_PRESENCE_PENALTY = os.environ.get("LLM_PRESENCE_PENALTY", "1.5")
LLM_REPETITION_PENALTY = os.environ.get("LLM_REPETITION_PENALTY", "1.0")
# Stateless in-enclave generation proxy (POST /v1/generate). HMAC-gated, bounded, one in flight.
GENERATE_ENABLED = os.environ.get("GENERATE_ENABLED", "1") not in ("0", "false", "")
GEN_MAX_MESSAGES = int(os.environ.get("GEN_MAX_MESSAGES", "16"))
GEN_MAX_PROMPT_CHARS = int(os.environ.get("GEN_MAX_PROMPT_CHARS", "16000"))
GEN_MAX_TOKENS = int(os.environ.get("GEN_MAX_TOKENS", "1024"))
# OpenAI-compatible POST /v1/chat/completions (v0.3.0). HMAC-gated, stateless, fixed upstreams.
# Model id -> closed in-enclave vLLM. The caller picks a model id from this allowlist only;
# it can never supply a URL. ABL_* = second vLLM serving the abliterated weights.
ABL_BASE_URL = os.environ.get("ABL_BASE_URL", "").rstrip("/")
ABL_MODEL = os.environ.get("ABL_MODEL", "qwen3.8-27b-abliterated")
CHAT_ENABLED = os.environ.get("CHAT_ENABLED", "1") not in ("0", "false", "")
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

app = FastAPI(title="hivemind", version="0.3.0")
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
    cols = {r[1] for r in c.execute("PRAGMA table_info(memories)")}
    if "org" not in cols:  # NULL org = personal memory, visible to author only
        c.execute("ALTER TABLE memories ADD COLUMN org TEXT")
    # hivemind org layer (cognee vocabulary: org = tenant, org memory = dataset)
    c.execute(
        """CREATE TABLE IF NOT EXISTS orgs(
             id TEXT PRIMARY KEY, name TEXT NOT NULL, wall TEXT NOT NULL,
             created REAL NOT NULL, wall_changed REAL NOT NULL)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS members(
             org TEXT NOT NULL, user TEXT NOT NULL, role TEXT NOT NULL,
             PRIMARY KEY(org, user))"""
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
    org: str | None = Field(None, description="org id you're a member of; omit = personal (author-only)")


class RecallIn(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)


WALLS = ("sealed", "signal", "open-derived")
ORG_ID = r"^[a-z0-9][a-z0-9-]{1,40}$"


class OrgIn(BaseModel):
    id: str = Field(..., pattern=ORG_ID)
    name: str = Field(..., min_length=1, max_length=80)
    wall: str = Field("sealed", pattern="^(sealed|signal|open-derived)$")


class MemberIn(BaseModel):
    user: str = Field(..., pattern=r"^[A-Za-z0-9_.:@-]{1,64}$")
    role: str = Field("member", pattern="^(member|admin)$")


class WallIn(BaseModel):
    wall: str = Field(..., pattern="^(sealed|signal|open-derived)$")


# ---------------------------------------------------------------- backends
class SqliteBackend:
    name = "sqlite"

    def remember(self, scope: str, text: str, org: str | None = None) -> dict:
        topics = extract_topics(text)
        v = embed([text])[0]
        with _lock:
            cur = conn().execute(
                "INSERT INTO memories(scope,org,text,topics,emb,created) VALUES(?,?,?,?,?,?)",
                (scope, org, text, json.dumps(topics), v.tobytes(), time.time()),
            )
            conn().commit()
        return {"id": cur.lastrowid, "topics": topics}

    def search(self, query: str, k: int, where: str = "1=1", args: tuple = (),
               min_sim: float | None = None) -> list[dict]:
        """Scope filter is applied in SQL BEFORE ranking: disallowed rows are
        never loaded, scored, or handed to the LLM."""
        with _lock:
            rows = conn().execute(
                f"SELECT id, scope, org, text, emb FROM memories WHERE {where}", args
            ).fetchall()
        if not rows:
            return []
        q = embed([query])[0]
        mat = np.stack([np.frombuffer(r[4], dtype=np.float32) for r in rows])
        sims = mat @ q
        order = np.argsort(-sims)[:k]
        floor = RECALL_MIN_SIM if min_sim is None else min_sim
        return [
            {"id": rows[i][0], "scope": rows[i][1], "org": rows[i][2], "text": rows[i][3],
             "sim": float(sims[i])}
            for i in order
            if sims[i] >= floor
        ]

    def all_topics(self) -> list[tuple[str, str | None, list[str]]]:
        with _lock:
            rows = conn().execute("SELECT scope, org, topics FROM memories").fetchall()
        return [(r[0], r[1], json.loads(r[2])) for r in rows]

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
    "You are hivemind, a sealed shared memory running in a confidential enclave. You are given "
    "private notes from the asker and from orgs they may draw on. Answer the question by COMBINING facts across notes. Rules: "
    "never quote notes verbatim, never say who said what or which org a fact came from, never reveal names, emails, handles, "
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
    body.update(sampling_params())
    try:
        r = httpx.post(f"{LLM_BASE_URL}/chat/completions", json=body, headers=headers, timeout=LLM_TIMEOUT)
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        out = strip_think(msg.get("content") or "")
        if out and quality(out)["degenerate"]:
            # never hand punctuation soup to a caller as an "answer"; extractive fallback instead
            print("[swarm-memory] llm output degenerate, falling back")
            return None
        return scrub(out) if out else None
    except Exception as e:
        print(f"[swarm-memory] llm unavailable: {type(e).__name__}")
        return None


LEGACY_SAMPLING = {"temperature": 0.2}  # v0.1.0 behaviour, kept only for A/B diagnosis


def sampling_params(preset: str = "recommended") -> dict:
    """Request-body sampling + chat-template fields for the in-enclave model."""
    if preset == "legacy":
        p = dict(LEGACY_SAMPLING)
    else:
        p = {}
        for k, v, cast in (("temperature", LLM_TEMPERATURE, float), ("top_p", LLM_TOP_P, float),
                           ("top_k", LLM_TOP_K, int), ("min_p", LLM_MIN_P, float),
                           ("presence_penalty", LLM_PRESENCE_PENALTY, float),
                           ("repetition_penalty", LLM_REPETITION_PENALTY, float)):
            if v not in (None, ""):
                p[k] = cast(v)
    if "gpt-oss" in LLM_MODEL:
        p["reasoning_effort"] = "low"
    elif "qwen" in LLM_MODEL.lower():
        p["chat_template_kwargs"] = {"enable_thinking": False}  # vLLM: skip <think>
    if LLM_EXTRA_BODY:
        p.update(json.loads(LLM_EXTRA_BODY))
    return p


def strip_think(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if "</think>" in text:  # opening tag eaten by the template
        text = text.split("</think>", 1)[1]
    return text.strip()


def quality(text: str) -> dict:
    """Content-free quality counts (safe to log/return). No text retained."""
    n = len(text)
    letters = sum(1 for ch in text if ch.isalpha())
    words = re.findall(r"[^\W\d_]{2,}", text)
    ratio = letters / n if n else 0.0
    top = Counter(text).most_common(1)[0][1] / n if n else 0.0
    degenerate = n > 0 and (ratio < 0.3 or len(words) < 2 or (n >= 40 and top > 0.5))
    return {"chars": n, "letters": letters, "words": len(words), "letter_ratio": round(ratio, 3),
            "top_char_share": round(top, 3), "degenerate": degenerate}


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


# ---------------------------------------------------------------- org layer
# cognee vocabulary: org = tenant, org memory = dataset, membership = role grant.
def org_row(org_id: str):
    with _lock:
        return conn().execute("SELECT id, name, wall, created, wall_changed FROM orgs WHERE id=?",
                              (org_id,)).fetchone()


def role_of(user: str, org_id: str) -> str | None:
    with _lock:
        r = conn().execute("SELECT role FROM members WHERE org=? AND user=?", (org_id, user)).fetchone()
    return r[0] if r else None


def my_orgs(user: str) -> set[str]:
    with _lock:
        return {r[0] for r in conn().execute("SELECT org FROM members WHERE user=?", (user,))}


def network_orgs(mine: set[str]) -> set[str]:
    """Your network = orgs reachable through shared members (connected component
    of the org<->person graph, computed in-enclave, never returned)."""
    with _lock:
        rows = conn().execute("SELECT org, user FROM members").fetchall()
    by_user: dict[str, set] = {}
    for o, u in rows:
        by_user.setdefault(u, set()).add(o)
    seen, frontier = set(mine), set(mine)
    while frontier:
        nxt = set()
        for orgs in by_user.values():
            if orgs & frontier:
                nxt |= orgs - seen
        seen |= nxt
        frontier = nxt
    return seen - mine


def require_admin(user: str, org_id: str):
    if not org_row(org_id):
        raise HTTPException(404, "no such org")
    if role_of(user, org_id) != "admin":
        raise HTTPException(403, "org admin only")


SIGNAL_MIN_SIM = float(os.environ.get("SIGNAL_MIN_SIM", "0.35"))
ORG_DAY_USD = float(os.environ.get("ORG_DAY_USD", "5"))


def shingles(s: str, n: int = 6) -> set[str]:
    w = re.findall(r"[a-z0-9]+", s.lower())
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


def leaks_raw(answer: str, texts: list[str]) -> bool:
    """open-derived guard: any 6-word run copied from a non-member note = raw leak."""
    a = shingles(answer)
    return any(a & shingles(t) for t in texts)


def _inq(xs) -> str:
    return ",".join("?" * len(xs))


# ---------------------------------------------------------------- stateless generation
class GenMessage(BaseModel):
    model_config = {"extra": "forbid"}
    role: str = Field(..., pattern="^(system|user|assistant)$")
    content: str = Field(..., min_length=1, max_length=16000)


class GenerateIn(BaseModel):
    model_config = {"extra": "forbid"}  # no model / url / base / tools fields accepted
    messages: list[GenMessage] = Field(..., min_length=1, max_length=64)
    max_tokens: int = Field(512, ge=1, le=4096)
    sampling: str = Field("recommended", pattern="^(recommended|legacy)$")


_gen_slot = threading.Semaphore(1)


# ---------------------------------------------------------------- routes
@app.get("/healthz")
def healthz():
    with _lock:
        n_orgs = conn().execute("SELECT COUNT(*) FROM orgs").fetchone()[0]
    return {"ok": True, "product": "hivemind", "backend": backend.name, **backend.stats(),
            "orgs": n_orgs, "llm": LLM_MODEL if LLM_ENABLED else None,
            "chat_models": sorted(chat_upstreams()) if CHAT_ENABLED else []}


@app.post("/orgs")
def create_org(body: OrgIn, user: str = Depends(scope_from_headers)):
    now = time.time()
    with _lock:
        if conn().execute("SELECT 1 FROM orgs WHERE id=?", (body.id,)).fetchone():
            raise HTTPException(409, "org exists")
        conn().execute("INSERT INTO orgs VALUES(?,?,?,?,?)", (body.id, body.name, body.wall, now, now))
        conn().execute("INSERT INTO members VALUES(?,?,?)", (body.id, user, "admin"))
        conn().commit()
    return {"org": body.id, "name": body.name, "wall": body.wall, "role": "admin"}


@app.post("/orgs/{org_id}/members")
def add_member(org_id: str, body: MemberIn, user: str = Depends(scope_from_headers)):
    require_admin(user, org_id)
    with _lock:
        conn().execute("INSERT OR REPLACE INTO members VALUES(?,?,?)", (org_id, body.user, body.role))
        conn().commit()
    return {"org": org_id, "user": body.user, "role": body.role}


@app.post("/orgs/{org_id}/wall")
def set_wall(org_id: str, body: WallIn, user: str = Depends(scope_from_headers)):
    require_admin(user, org_id)
    before = org_row(org_id)[2]
    with _lock:
        conn().execute("UPDATE orgs SET wall=?, wall_changed=? WHERE id=?", (body.wall, time.time(), org_id))
        conn().commit()
    return {"org": org_id, "wall": body.wall, "was": before}


@app.get("/orgs")
def list_orgs(user: str = Depends(scope_from_headers)):
    """Caller's view: only orgs you're in. Other orgs are not enumerable."""
    with _lock:
        rows = conn().execute(
            """SELECT o.id, o.name, o.wall, m.role,
                      (SELECT COUNT(*) FROM members x WHERE x.org=o.id),
                      (SELECT COUNT(*) FROM memories y WHERE y.org=o.id)
               FROM orgs o JOIN members m ON m.org=o.id WHERE m.user=? ORDER BY o.id""", (user,)).fetchall()
        personal = conn().execute("SELECT COUNT(*) FROM memories WHERE scope=? AND org IS NULL",
                                  (user,)).fetchone()[0]
    return {"user": user, "personal_memories": personal,
            "orgs": [{"id": r[0], "name": r[1], "wall": r[2], "role": r[3], "members": r[4], "memories": r[5]}
                     for r in rows]}


@app.post("/remember")
def remember(body: RememberIn, scope: str = Depends(scope_from_headers)):
    if body.scope and body.scope != scope:
        raise HTTPException(403, "scope must match signed X-Swarm-User")
    if body.org is not None:
        if not org_row(body.org):
            raise HTTPException(404, "no such org")
        if not role_of(scope, body.org):
            raise HTTPException(403, "not a member of that org")
    out = backend.remember(scope, body.text, body.org)
    return {"stored": True, "scope": scope, "org": body.org or "personal", "topics": out["topics"]}


@app.post("/recall")
def recall(body: RecallIn, scope: str = Depends(scope_from_headers)):
    """Walls are enforced here, inside the enclave, BEFORE retrieval:
       your personal notes + orgs you're in -> full context
       open-derived orgs you're not in      -> context, but only a derived answer leaves
       signal orgs in your network          -> existence hint (org COUNT only), zero content
       sealed orgs you're not in            -> nothing, not even counted"""
    mine = my_orgs(scope)
    with _lock:
        walls = dict(conn().execute("SELECT id, wall FROM orgs").fetchall())
    derived_orgs = {o for o, w in walls.items() if w == "open-derived" and o not in mine}
    signal_orgs = {o for o, w in walls.items() if w == "signal" and o not in mine} & network_orgs(mine)

    where = "(org IS NULL AND scope=?)" + (f" OR org IN ({_inq(mine)})" if mine else "")
    member_hits = backend.search(body.query, RECALL_K, where, (scope, *sorted(mine)))
    derived_hits = (backend.search(body.query, RECALL_K, f"org IN ({_inq(derived_orgs)})",
                                   tuple(sorted(derived_orgs))) if derived_orgs else [])
    signal_hit = set()
    if signal_orgs:
        sh = backend.search(body.query, RECALL_K, f"org IN ({_inq(signal_orgs)})",
                            tuple(sorted(signal_orgs)), min_sim=SIGNAL_MIN_SIM)
        signal_hit = {h["org"] for h in sh}
        del sh  # signal content is never passed to the model or the response

    hits = member_hits + derived_hits
    mode = "empty"
    if hits:
        answer = llm_answer(body.query, hits)
        mode = "llm"
        if answer and derived_hits and leaks_raw(answer, [h["text"] for h in derived_hits]):
            answer = llm_answer(body.query + " (paraphrase heavily, copy no phrases)", hits)
            if answer and leaks_raw(answer, [h["text"] for h in derived_hits]):
                answer = None
        if not answer:
            # extractive returns raw sentences, so it may only ever use member-visible notes
            mode = "extractive"
            answer = (extractive_answer(body.query, member_hits) if member_hits else
                      "a derived answer exists but the in-enclave model is unavailable; nothing raw is returned.")
    else:
        answer = "nothing in memory you can access matches that yet."
    hints = []
    if signal_hit:
        n = len(signal_hit)
        hints.append({"type": "signal", "orgs": n,
                      "message": f"{n} org{'s' if n > 1 else ''} in your network "
                                 f"{'have' if n > 1 else 'has'} context on this. request access."})
    return {
        "answer": answer,
        "mode": mode,
        "access": {
            "personal_memories": sum(1 for h in member_hits if h["org"] is None),
            "member_orgs": len({h["org"] for h in member_hits if h["org"]}),
            "member_memories": sum(1 for h in member_hits if h["org"]),
            "derived_orgs": len({h["org"] for h in derived_hits}),
            "derived_memories": len(derived_hits),
            "signal_orgs": len(signal_hit),
        },
        "contributors": {
            "scopes": len({h["scope"] for h in hits}),
            "other_scopes": len({h["scope"] for h in hits} - {scope}),
            "memories": len(hits),
            "own_memories": sum(1 for h in hits if h["scope"] == scope),
        },
        "hints": hints,
    }


PALETTE = ["#c8ff00", "#ff3b2f", "#2fd6ff", "#ffb000", "#c77dff", "#3bff9a"]


@app.post("/v1/generate")
def generate(body: GenerateIn, user: str = Depends(scope_from_headers)):
    """Stateless in-enclave completion. Same closed vLLM destination as /recall, fixed model,
    no caller-selected endpoint, nothing persisted or logged (content-free counts only).
    Bounded prompt/output; one request in flight per enclave (busy -> 429)."""
    if not GENERATE_ENABLED or not LLM_ENABLED or not LLM_BASE_URL:
        raise HTTPException(503, "generation disabled")
    if len(body.messages) > GEN_MAX_MESSAGES:
        raise HTTPException(413, f"at most {GEN_MAX_MESSAGES} messages")
    if sum(len(m.content) for m in body.messages) > GEN_MAX_PROMPT_CHARS:
        raise HTTPException(413, f"prompt over {GEN_MAX_PROMPT_CHARS} chars")
    if body.max_tokens > GEN_MAX_TOKENS:
        raise HTTPException(413, f"max_tokens over {GEN_MAX_TOKENS}")
    if not _gen_slot.acquire(blocking=False):
        raise HTTPException(429, "generation busy, one request in flight")
    try:
        req = {"model": LLM_MODEL, "max_tokens": body.max_tokens,
               "messages": [{"role": m.role, "content": m.content} for m in body.messages],
               **sampling_params(body.sampling)}
        headers = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}
        t0 = time.time()
        try:
            r = httpx.post(f"{LLM_BASE_URL}/chat/completions", json=req, headers=headers, timeout=LLM_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            choice = data["choices"][0]
        except Exception as e:
            print(f"[swarm-memory] generate upstream error: {type(e).__name__}")
            raise HTTPException(502, "in-enclave model unavailable")
        text = strip_think((choice.get("message") or {}).get("content") or "")
        q = quality(text)
        latency = round(time.time() - t0, 2)
        print(f"[swarm-memory] generate chars={q['chars']} ratio={q['letter_ratio']} "
              f"degenerate={q['degenerate']} finish={choice.get('finish_reason')} s={latency}")
        return {"model": LLM_MODEL, "text": text, "finish_reason": choice.get("finish_reason"),
                "usage": data.get("usage"), "sampling": body.sampling, "quality": q,
                "latency_s": latency}
    finally:
        _gen_slot.release()


# ---------------------------------------------------------------- OpenAI-compatible chat
def chat_upstreams() -> dict:
    """model id -> fixed in-enclave base url. Nothing else is reachable."""
    ups = {}
    if LLM_ENABLED and LLM_BASE_URL:
        ups[LLM_MODEL] = LLM_BASE_URL
    if ABL_BASE_URL:
        ups[ABL_MODEL] = ABL_BASE_URL
    return ups


_chat_slots: dict = {}
_chat_slots_lock = threading.Lock()


def chat_slot(model: str) -> threading.Semaphore:
    with _chat_slots_lock:
        if model not in _chat_slots:
            # the stock model shares its slot with /v1/generate (one engine, one in flight)
            _chat_slots[model] = _gen_slot if model == LLM_MODEL else threading.Semaphore(1)
        return _chat_slots[model]


class ChatPart(BaseModel):
    model_config = {"extra": "ignore"}
    type: str = Field(..., pattern="^text$")  # text only: no images/audio/files
    text: str = Field(..., max_length=16000)


class ChatMessage(BaseModel):
    model_config = {"extra": "ignore"}  # name etc. dropped, never forwarded
    role: str = Field(..., pattern="^(system|developer|user|assistant)$")
    content: str | list[ChatPart]

    def text(self) -> str:
        return self.content if isinstance(self.content, str) else "\n".join(p.text for p in self.content)


class ChatIn(BaseModel):
    # OpenAI SDKs send many optional fields; unknown ones are ignored and NEVER forwarded.
    model_config = {"extra": "ignore"}
    model: str = Field(..., max_length=64)
    messages: list[ChatMessage] = Field(..., min_length=1, max_length=64)
    max_tokens: int | None = Field(None, ge=1, le=8192)
    max_completion_tokens: int | None = Field(None, ge=1, le=8192)
    temperature: float | None = Field(None, ge=0, le=2)
    top_p: float | None = Field(None, gt=0, le=1)
    presence_penalty: float | None = Field(None, ge=-2, le=2)
    frequency_penalty: float | None = Field(None, ge=-2, le=2)
    stop: str | list[str] | None = None
    seed: int | None = None
    stream: bool = False
    tools: list | None = None
    n: int | None = Field(None, ge=1, le=1)


def _chat_upstream_body(body: ChatIn, model: str) -> dict:
    mt = body.max_completion_tokens or body.max_tokens or 512
    if mt > GEN_MAX_TOKENS:
        raise HTTPException(413, f"max_tokens over {GEN_MAX_TOKENS}")
    msgs = [{"role": "system" if m.role == "developer" else m.role, "content": m.text()} for m in body.messages]
    if len(msgs) > GEN_MAX_MESSAGES:
        raise HTTPException(413, f"at most {GEN_MAX_MESSAGES} messages")
    if sum(len(m["content"]) for m in msgs) > GEN_MAX_PROMPT_CHARS:
        raise HTTPException(413, f"prompt over {GEN_MAX_PROMPT_CHARS} chars")
    req = {"max_tokens": mt, "messages": msgs, **sampling_params()}
    for k in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "seed"):
        v = getattr(body, k)
        if v is not None:
            req[k] = v
    if body.stop is not None:
        req["stop"] = [body.stop] if isinstance(body.stop, str) else body.stop[:4]
    req["model"] = model
    return req


@app.get("/v1/models")
def chat_models(user: str = Depends(scope_from_headers)):
    now = int(time.time())
    return {"object": "list", "data": [{"id": m, "object": "model", "created": now, "owned_by": "enclave"}
                                       for m in chat_upstreams()]}


@app.post("/v1/chat/completions")
def chat_completions(body: ChatIn, user: str = Depends(scope_from_headers)):
    """OpenAI-compatible, stateless. Model id must be in the in-enclave allowlist; each id maps
    to a fixed closed vLLM. No tools, text only, capped, one request in flight per engine (429).
    Nothing persisted; logs are content-free counts. stream=true gets a single-chunk SSE."""
    if not CHAT_ENABLED:
        raise HTTPException(503, "chat disabled")
    ups = chat_upstreams()
    if body.model not in ups:
        raise HTTPException(404, f"model must be one of {sorted(ups)}")
    if body.tools:
        raise HTTPException(400, "tools are not supported")
    req = _chat_upstream_body(body, body.model)
    slot = chat_slot(body.model)
    if not slot.acquire(blocking=False):
        raise HTTPException(429, "model busy, one request in flight")
    try:
        t0 = time.time()
        try:
            r = httpx.post(f"{ups[body.model]}/chat/completions", json=req,
                           headers={"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {},
                           timeout=LLM_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            choice = data["choices"][0]
        except Exception as e:
            print(f"[swarm-memory] chat upstream error model={body.model} {type(e).__name__}")
            raise HTTPException(502, "in-enclave model unavailable")
        text = strip_think((choice.get("message") or {}).get("content") or "")
        q = quality(text)
        latency = round(time.time() - t0, 2)
        print(f"[swarm-memory] chat model={body.model} chars={q['chars']} ratio={q['letter_ratio']} "
              f"degenerate={q['degenerate']} finish={choice.get('finish_reason')} s={latency}")
    finally:
        slot.release()
    cid = "chatcmpl-" + hashlib.sha256(f"{t0}{os.getpid()}{threading.get_ident()}".encode()).hexdigest()[:24]
    created = int(t0)
    finish = choice.get("finish_reason") or "stop"
    usage = data.get("usage")
    if not body.stream:
        return {"id": cid, "object": "chat.completion", "created": created, "model": body.model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                             "finish_reason": finish}],
                "usage": usage}

    def sse():
        base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": body.model}
        yield "data: " + json.dumps({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                                                          "finish_reason": None}]}) + "\n\n"
        yield "data: " + json.dumps({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                                     "usage": usage}) + "\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(sse(), media_type="text/event-stream")


@app.get("/graph")
def graph():
    """Operator/public view. Personal memories never appear. No person labels.
    Topic words shown only for open-derived orgs; sealed/signal orgs show heat
    with redacted labels."""
    with _lock:
        orgs = conn().execute("SELECT id, name, wall, wall_changed FROM orgs ORDER BY created").fetchall()
        mcount = dict(conn().execute("SELECT org, COUNT(*) FROM members GROUP BY org").fetchall())
    wall = {o[0]: o[2] for o in orgs}
    node_mem: Counter = Counter()
    node_scopes: dict[tuple, set] = {}
    edges: Counter = Counter()
    for sc, org, topics in backend.all_topics():
        if org is None or org not in wall:
            continue
        for t in topics:
            node_mem[(org, t)] += 1
            node_scopes.setdefault((org, t), set()).add(sc)
        for a, b in combinations(sorted(set(topics)), 2):
            edges[(org, a, b)] += 1
    visible = {k for k, s in node_scopes.items() if len(s) >= GRAPH_K}
    nodes, ids = [], {}
    for org, t in sorted(visible, key=lambda k: -node_mem[k])[:160]:
        opened = wall[org] == "open-derived"
        nid = f"{org}:{t}" if opened else f"{org}:#{hashlib.sha256((org + t).encode()).hexdigest()[:6]}"
        ids[(org, t)] = nid
        nodes.append({"id": nid, "org": org, "label": t if opened else None,
                      "weight": node_mem[(org, t)], "contributors": len(node_scopes[(org, t)])})
    links = [{"source": ids[(o, a)], "target": ids[(o, b)], "weight": w}
             for (o, a, b), w in edges.most_common(600) if (o, a) in ids and (o, b) in ids]
    st = backend.stats()
    return {
        "orgs": [{"id": o[0], "name": o[1], "wall": o[2], "wall_changed": o[3], "members": mcount.get(o[0], 0),
                  "color": PALETTE[i % len(PALETTE)]} for i, o in enumerate(orgs)],
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
    if _ledger_cache["v"] and now - _ledger_cache["t"] < 5:
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
    with _lock:
        orgs = conn().execute("SELECT id, created FROM orgs").fetchall()
    days = {o[0]: max(1, int((now - o[1]) // 86400) + 1) for o in orgs}
    out["workspaces"] = {"plan": f"${ORG_DAY_USD:g}/org/day (stub, not billed)", "orgs": len(orgs),
                         "accrued_usd": round(sum(days.values()) * ORG_DAY_USD, 2),
                         "run_rate_usd_month": round(len(orgs) * ORG_DAY_USD * 30, 2)}
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


# ---------------------------------------------------------------- private fine-tuning (org adapters)
from app import finetune as _finetune  # noqa: E402  (imports this module; must stay last)

_finetune.init_tables()
app.include_router(_finetune.router)
