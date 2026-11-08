"""Private per-org fine-tuning ("own the soul").

An org's consented memory becomes a LoRA adapter trained by the in-enclave trainer
sidecar. Dataset, adapter weights and the trained model never leave the enclave:
  - the dataset is built here, from sqlite, and posted over the closed `train` network
  - the trainer writes the adapter to the enclave-local adapter volume
  - there is no route that returns adapter bytes, only hashes and a signed receipt
  - generation with an org adapter is member-only

Consent (walls):
  - org memories are used only after an org admin turns on the org policy
  - a member's personal memories are used only if that member opted in, for that org
  - another org's memories are NEVER used, whatever its wall (sealed / signal /
    open-derived). Weights can memorise raw text, so open-derived's "derived answer only"
    promise can't be kept by an adapter. Raw cross-org text stays out of training.
  - withdrawing personal consent deletes every adapter of that org that included it
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import app.main as M  # app.main imports this module at its very end; import main first

FT_ENABLED = os.environ.get("FT_ENABLED", "0") not in ("0", "false", "")
FT_TRAINER_URL = os.environ.get("FT_TRAINER_URL", "http://trainer:8090").rstrip("/")
# vllm = vLLM --enable-lora; trainer = HF+PEFT on the trainer; auto = vllm, falling back to trainer
# if the engine refuses the adapter (e.g. LoRA unsupported for this arch in the pinned image)
FT_SERVE = os.environ.get("FT_SERVE", "auto")
FT_ADAPTER_ROOT = os.environ.get("FT_ADAPTER_ROOT", "/adapters")  # path as seen by vLLM
FT_MAX_RANK = int(os.environ.get("FT_MAX_RANK", "16"))  # must be <= vLLM --max-lora-rank
FT_MAX_EXAMPLES = int(os.environ.get("FT_MAX_EXAMPLES", "4096"))
FT_BASE_ID = os.environ.get("FT_BASE_ID", "")  # e.g. mpk root hash of the base weights
RECEIPT_KEY_DIR = Path(os.environ.get("RECEIPT_KEY_DIR", "/run/tinfoil/keys/hivemind-receipts"))
TOKEN_SALT = b"hivemind-trainer"

router = APIRouter()
http = httpx  # swapped for an in-process client in tests
_vllm_loaded: set[str] = set()
_vllm_lock = threading.Lock()


# ---------------------------------------------------------------- storage
def init_tables():
    c = M.conn()
    with M._lock:
        c.execute("""CREATE TABLE IF NOT EXISTS ft_policy(
                       org TEXT PRIMARY KEY, org_memories INTEGER NOT NULL, updated REAL NOT NULL,
                       by TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS ft_consent(
                       org TEXT NOT NULL, user TEXT NOT NULL, personal INTEGER NOT NULL,
                       updated REAL NOT NULL, PRIMARY KEY(org, user))""")
        c.execute("""CREATE TABLE IF NOT EXISTS ft_jobs(
                       id TEXT PRIMARY KEY, org TEXT NOT NULL, requested_by TEXT NOT NULL,
                       created REAL NOT NULL, dataset_sha TEXT NOT NULL, examples INTEGER NOT NULL,
                       memories INTEGER NOT NULL, contributors TEXT NOT NULL, state TEXT NOT NULL,
                       adapter_sha TEXT, result TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS ft_adapters(
                       org TEXT NOT NULL, sha TEXT NOT NULL, job TEXT NOT NULL, dataset_sha TEXT NOT NULL,
                       contributors TEXT NOT NULL, created REAL NOT NULL, receipt TEXT NOT NULL,
                       PRIMARY KEY(org, sha))""")
        c.commit()


def trainer_token() -> str:
    return hmac.new(M.SWARM_SECRET.encode(), TOKEN_SALT, hashlib.sha256).hexdigest()


def trainer(method: str, path: str, body: dict | None = None, timeout: float = 30):
    try:
        r = http.request(method, f"{FT_TRAINER_URL}{path}", json=body, timeout=timeout,
                         headers={"Authorization": f"Bearer {trainer_token()}"})
    except Exception as e:
        print(f"[finetune] trainer unreachable {type(e).__name__}")
        raise HTTPException(502, "in-enclave trainer unavailable")
    return r


def require_member(user: str, org: str) -> str:
    if not M.org_row(org):
        raise HTTPException(404, "no such org")
    role = M.role_of(user, org)
    if not role:
        # same answer as "no such org": membership isn't probeable through this API
        raise HTTPException(404, "no such org")
    return role


def require_enabled():
    if not FT_ENABLED:
        raise HTTPException(503, "fine-tuning disabled")


# ---------------------------------------------------------------- dataset
def org_system(org: str) -> str:
    name = M.org_row(org)[1]
    return (f"You are the private memory model of the org \"{name}\", running in a sealed enclave. "
            "Answer from what the org knows, directly and briefly. Never reveal names, emails, handles, "
            "phone numbers, keys or links.")


def consented_rows(org: str) -> tuple[list[tuple], bool]:
    """(scope, org, text, topics) rows this org has consented to train on. The SQL
    names exactly two sources; every other org's rows are unreachable from here."""
    with M._lock:
        pol = M.conn().execute("SELECT org_memories FROM ft_policy WHERE org=?", (org,)).fetchone()
        use_org = bool(pol and pol[0])
        opted = [r[0] for r in M.conn().execute(
            """SELECT c.user FROM ft_consent c JOIN members m ON m.org=c.org AND m.user=c.user
               WHERE c.org=? AND c.personal=1""", (org,))]
        rows = []
        if use_org:
            rows += M.conn().execute(
                "SELECT scope, org, text, topics FROM memories WHERE org=? ORDER BY id", (org,)).fetchall()
        if opted:
            rows += M.conn().execute(
                f"SELECT scope, org, text, topics FROM memories WHERE org IS NULL AND scope IN ({M._inq(opted)}) "
                "ORDER BY id", tuple(opted)).fetchall()
    return rows, use_org


def build_dataset(org: str) -> dict:
    rows, use_org = consented_rows(org)
    for _, o, _, _ in rows:
        assert o in (None, org), "cross-org row in dataset"  # belt and braces, see consented_rows
    system = org_system(org)
    examples, seen = [], set()

    def add(q: str, a: str):
        key = (q, a)
        if a and key not in seen:
            seen.add(key)
            examples.append({"messages": [{"role": "system", "content": system},
                                          {"role": "user", "content": q},
                                          {"role": "assistant", "content": a}]})

    by_topic: dict[str, list[str]] = {}
    for _, _, text, topics in rows:
        a = M.scrub(text.strip())
        tp = json.loads(topics) or M.extract_topics(text)
        if tp:
            add(f"What do we know about {', '.join(tp[:3])}?", a)
            add(f"Remind me what our notes say about {tp[0]}.", a)
            for t in tp[:2]:
                by_topic.setdefault(t, []).append(a)
        else:
            add("What's one thing the org remembers?", a)
    for t, notes in by_topic.items():
        if len(notes) > 1:
            add(f"Summarize everything we know about {t}.", " ".join(notes[:3]))
    examples = examples[:FT_MAX_EXAMPLES]
    jsonl = "\n".join(json.dumps(e, sort_keys=True, ensure_ascii=False) for e in examples)
    return {"examples": examples, "jsonl_sha256": hashlib.sha256(jsonl.encode()).hexdigest(),
            "memories": len(rows), "contributors": sorted({r[0] for r in rows}),
            "org_memories_used": use_org}


# ---------------------------------------------------------------- receipts
def sign_receipt(body: dict) -> dict:
    """Canonical JSON receipt. Signed with the enclave's attested ed25519 key when the
    config grants one (public key is in the attestation document); unsigned otherwise."""
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"))
    out = {"receipt": body, "canonical_sha256": hashlib.sha256(payload.encode()).hexdigest()}
    keyf = RECEIPT_KEY_DIR / "private_key.pem"
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        key = load_pem_private_key(keyf.read_bytes(), password=None)
        out["sig_ed25519"] = base64.b64encode(key.sign(payload.encode())).decode()
        out["public_key_pem"] = (RECEIPT_KEY_DIR / "public_key.pem").read_text()
        out["key_id"] = "hivemind-receipts"
    except FileNotFoundError:
        out["sig_ed25519"] = None
        out["note"] = "unsigned: no attested receipt key granted in this config"
    return out


def attestation_sha() -> str | None:
    att = M.TINFOIL_DIR / "attestation.json"
    try:
        return hashlib.sha256(att.read_bytes()).hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------- models
class PolicyIn(BaseModel):
    model_config = {"extra": "forbid"}
    org_memories: bool


class ConsentIn(BaseModel):
    model_config = {"extra": "forbid"}
    personal: bool


class FinetuneIn(BaseModel):
    model_config = {"extra": "forbid"}
    r: int = Field(16, ge=4, le=64)
    epochs: int = Field(3, ge=1, le=10)
    lr: float = Field(2e-4, ge=1e-6, le=5e-3)
    max_steps: int = Field(600, ge=1, le=5000)
    max_len: int = Field(1024, ge=64, le=4096)
    grad_accum: int = Field(4, ge=1, le=64)


class OrgGenIn(BaseModel):
    model_config = {"extra": "forbid"}
    messages: list[M.GenMessage] = Field(..., min_length=1, max_length=16)
    max_tokens: int = Field(256, ge=1, le=1024)


# ---------------------------------------------------------------- routes
@router.post("/v1/orgs/{org}/finetune/policy")
def set_policy(org: str, body: PolicyIn, user: str = Depends(M.scope_from_headers)):
    M.require_admin(user, org)
    with M._lock:
        M.conn().execute("INSERT OR REPLACE INTO ft_policy VALUES(?,?,?,?)",
                         (org, int(body.org_memories), time.time(), user))
        M.conn().commit()
    dropped = [] if body.org_memories else drop_adapters(org, lambda a: a["org_memories_used"])
    return {"org": org, "org_memories": body.org_memories, "adapters_deleted": len(dropped)}


@router.post("/v1/orgs/{org}/finetune/consent")
def set_consent(org: str, body: ConsentIn, user: str = Depends(M.scope_from_headers)):
    """A member's own choice to let their PERSONAL memories train this org's adapter.
    Only the member can set it. Withdrawing deletes adapters that included them."""
    require_member(user, org)
    with M._lock:
        M.conn().execute("INSERT OR REPLACE INTO ft_consent VALUES(?,?,?,?)",
                         (org, user, int(body.personal), time.time()))
        M.conn().commit()
    dropped = [] if body.personal else drop_adapters(org, lambda a: user in a["contributors"])
    return {"org": org, "personal": body.personal, "adapters_deleted": len(dropped)}


@router.post("/v1/orgs/{org}/finetune", status_code=202)
def start_finetune(org: str, body: FinetuneIn | None = None, user: str = Depends(M.scope_from_headers)):
    require_enabled()
    M.require_admin(user, org)
    body = body or FinetuneIn()
    if body.r > FT_MAX_RANK:
        raise HTTPException(422, f"r must be <= {FT_MAX_RANK} (serving max-lora-rank)")
    with M._lock:
        running = M.conn().execute("SELECT id FROM ft_jobs WHERE state IN ('queued','loading','training')"
                                   ).fetchone()
    if running:
        refresh(running[0])
        with M._lock:
            st = M.conn().execute("SELECT state FROM ft_jobs WHERE id=?", (running[0],)).fetchone()[0]
        if st in ("queued", "loading", "training"):
            raise HTTPException(409, "a fine-tune job is already running in this enclave")
    ds = build_dataset(org)
    if not ds["examples"]:
        raise HTTPException(409, "no consented memory to train on: set the org policy or member consent")
    job_id = secrets.token_hex(16)
    hp = {"r": body.r, "alpha": body.r * 2, "epochs": body.epochs, "lr": body.lr,
          "max_steps": body.max_steps, "max_len": body.max_len, "grad_accum": body.grad_accum}
    r = trainer("POST", "/jobs", {"job_id": job_id, "org": org, "examples": ds["examples"], "hparams": hp},
                timeout=120)
    if r.status_code == 409:
        raise HTTPException(409, "trainer busy")
    if r.status_code != 202:
        print(f"[finetune] trainer rejected job status={r.status_code}")
        raise HTTPException(502, "trainer rejected job")
    with M._lock:
        M.conn().execute("INSERT INTO ft_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (job_id, org, user, time.time(), ds["jsonl_sha256"], len(ds["examples"]),
                          ds["memories"], json.dumps({"users": ds["contributors"],
                                                      "org_memories_used": ds["org_memories_used"]}),
                          "queued", None, None))
        M.conn().commit()
    print(f"[finetune] job {job_id[:8]} org={org} examples={len(ds['examples'])} memories={ds['memories']}")
    return {"job_id": job_id, "state": "queued",
            "dataset": {"examples": len(ds["examples"]), "memories": ds["memories"],
                        "contributors": len(ds["contributors"]), "sha256": ds["jsonl_sha256"]}}


def refresh(job_id: str) -> dict:
    with M._lock:
        row = M.conn().execute("SELECT id, org, state, dataset_sha, contributors, adapter_sha, result, created, "
                               "examples, memories FROM ft_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such job")
    job = {"job_id": row[0], "org": row[1], "state": row[2], "dataset_sha256": row[3],
           "adapter_sha256": row[5], "created": row[7], "examples": row[8], "memories": row[9]}
    if row[2] in ("done", "failed", "deleted"):
        job["result"] = json.loads(row[6]) if row[6] else None
        return job
    r = trainer("GET", f"/jobs/{job_id}")
    if r.status_code != 200:
        return {**job, "state": "lost" if r.status_code == 404 else row[2]}
    t = r.json()
    job.update(state=t["state"], step=t.get("step"), total=t.get("total"), loss=t.get("loss"))
    if t["state"] == "done":
        res = {k: v for k, v in t["result"].items() if k in (
            "adapter_sha256", "weights_sha256", "config_sha256", "steps", "examples", "tokens",
            "loss_first", "loss_last", "seconds", "params", "hparams", "targets")}
        contributors = json.loads(row[4])
        receipt = sign_receipt({
            "v": 1, "kind": "hivemind.org-adapter", "org": row[1], "job_id": job_id,
            "adapter_sha256": res["adapter_sha256"], "weights_sha256": res["weights_sha256"],
            "dataset_sha256": row[3], "examples": row[8], "memories": row[9],
            "contributors": len(contributors["users"]), "base": FT_BASE_ID or None,
            "hparams": res["hparams"], "targets": res["targets"], "created": int(time.time()),
            "enclave_attestation_sha256": attestation_sha(), "exportable": False})
        with M._lock:
            M.conn().execute("UPDATE ft_jobs SET state='done', adapter_sha=?, result=? WHERE id=?",
                             (res["adapter_sha256"], json.dumps(res), job_id))
            M.conn().execute("INSERT OR REPLACE INTO ft_adapters VALUES(?,?,?,?,?,?,?)",
                             (row[1], res["adapter_sha256"], job_id, row[3], row[4], time.time(),
                              json.dumps(receipt)))
            M.conn().commit()
        job.update(adapter_sha256=res["adapter_sha256"], result=res, receipt=receipt)
    elif t["state"] == "failed":
        with M._lock:
            M.conn().execute("UPDATE ft_jobs SET state='failed', result=? WHERE id=?",
                             (json.dumps({"error": t.get("error")}), job_id))
            M.conn().commit()
        job["result"] = {"error": t.get("error")}
    return job


@router.get("/v1/orgs/{org}/finetune/{job_id}")
def finetune_status(org: str, job_id: str, user: str = Depends(M.scope_from_headers)):
    require_enabled()
    require_member(user, org)
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(404, "no such job")
    with M._lock:
        own = M.conn().execute("SELECT 1 FROM ft_jobs WHERE id=? AND org=?", (job_id, org)).fetchone()
    if not own:
        raise HTTPException(404, "no such job")
    return refresh(job_id)


def adapters_of(org: str) -> list[dict]:
    with M._lock:
        rows = M.conn().execute("SELECT sha, job, dataset_sha, contributors, created, receipt FROM ft_adapters "
                                "WHERE org=? ORDER BY created DESC", (org,)).fetchall()
    out = []
    for r in rows:
        c = json.loads(r[3])
        out.append({"adapter_sha256": r[0], "job_id": r[1], "dataset_sha256": r[2], "created": r[4],
                    "contributors": c["users"], "org_memories_used": c["org_memories_used"],
                    "receipt": json.loads(r[5])})
    return out


@router.get("/v1/orgs/{org}/adapters")
def list_adapters(org: str, user: str = Depends(M.scope_from_headers)):
    require_member(user, org)
    # hashes + receipts only. Contributor ids stay in the enclave (count only).
    return {"org": org, "adapters": [{k: (len(v) if k == "contributors" else v) for k, v in a.items()}
                                     for a in adapters_of(org)]}


def drop_adapters(org: str, pred=lambda a: True) -> list[str]:
    gone = [a["adapter_sha256"] for a in adapters_of(org) if pred(a)]
    if not gone:
        return []
    for sha in gone:
        r = trainer("DELETE", f"/adapters/{org}/{sha}")
        if r.status_code not in (200, 404):
            raise HTTPException(502, "trainer could not delete adapter")
    with M._lock:
        M.conn().execute(f"DELETE FROM ft_adapters WHERE org=? AND sha IN ({M._inq(gone)})", (org, *gone))
        M.conn().execute(f"UPDATE ft_jobs SET state='deleted' WHERE org=? AND adapter_sha IN ({M._inq(gone)})",
                         (org, *gone))
        M.conn().commit()
    for sha in gone:
        name = lora_name(org, sha)
        if name in _vllm_loaded:
            try:
                http.request("POST", f"{vllm_root()}/v1/unload_lora_adapter", json={"lora_name": name},
                             timeout=30)
            except Exception:
                pass
            _vllm_loaded.discard(name)
    print(f"[finetune] org={org} adapters deleted={len(gone)}")
    return gone


@router.delete("/v1/orgs/{org}/adapters")
def delete_adapters(org: str, user: str = Depends(M.scope_from_headers)):
    M.require_admin(user, org)
    return {"org": org, "deleted": len(drop_adapters(org))}


def lora_name(org: str, sha: str) -> str:
    return f"org-{org}-{sha[:16]}"


def vllm_root() -> str:
    return re.sub(r"/v1$", "", M.LLM_BASE_URL)


def ensure_vllm_lora(org: str, sha: str) -> str:
    name = lora_name(org, sha)
    with _vllm_lock:
        if name in _vllm_loaded:
            return name
        r = http.request("POST", f"{vllm_root()}/v1/load_lora_adapter", timeout=120,
                         json={"lora_name": name, "lora_path": f"{FT_ADAPTER_ROOT}/{org}/{sha}"})
        if r.status_code != 200 and "already been loaded" not in r.text:
            print(f"[finetune] vllm lora load failed status={r.status_code}")
            raise HTTPException(502, "adapter could not be loaded")
        _vllm_loaded.add(name)
    return name


@router.post("/v1/orgs/{org}/generate")
def org_generate(org: str, body: OrgGenIn, user: str = Depends(M.scope_from_headers)):
    """Generate with the org's newest adapter. Members only. Stateless, nothing stored,
    content-free logs. The org system prompt is always first."""
    require_enabled()
    require_member(user, org)
    ads = adapters_of(org)
    if not ads:
        raise HTTPException(404, "org has no adapter yet")
    sha = ads[0]["adapter_sha256"]
    msgs = [m.model_dump() for m in body.messages]
    sys_extra = [m["content"] for m in msgs if m["role"] == "system"]
    msgs = [{"role": "system", "content": "\n\n".join([org_system(org), *sys_extra])}] + \
           [m for m in msgs if m["role"] != "system"]
    if sum(len(m["content"]) for m in msgs) > M.GEN_MAX_PROMPT_CHARS:
        raise HTTPException(413, f"prompt over {M.GEN_MAX_PROMPT_CHARS} chars")
    if not M._gen_slot.acquire(blocking=False):
        raise HTTPException(429, "generation busy, one request in flight")
    t0 = time.time()
    served = FT_SERVE
    try:
        text = None
        if FT_SERVE in ("vllm", "auto"):
            try:
                name = ensure_vllm_lora(org, sha)
                req = {"model": name, "messages": msgs, "max_tokens": body.max_tokens, **M.sampling_params()}
                try:
                    r = http.request("POST", f"{M.LLM_BASE_URL}/chat/completions", json=req,
                                     timeout=M.LLM_TIMEOUT)
                    r.raise_for_status()
                    ch = r.json()["choices"][0]
                    text, finish = (ch.get("message") or {}).get("content") or "", ch.get("finish_reason")
                    served = "vllm"
                except Exception as e:
                    print(f"[finetune] org generate upstream error {type(e).__name__}")
                    raise HTTPException(502, "in-enclave model unavailable")
            except HTTPException:
                if FT_SERVE == "vllm":
                    raise
                print("[finetune] vllm lora path failed, falling back to trainer serving")
        if text is None:
            served = "trainer"
            r = trainer("POST", "/generate", {"org": org, "adapter_sha256": sha, "messages": msgs,
                                              "max_tokens": body.max_tokens}, timeout=600)
            if r.status_code != 200:
                raise HTTPException(502 if r.status_code >= 500 else r.status_code, "trainer generate failed")
            j = r.json()
            text, finish = j["text"], j["finish_reason"]
    finally:
        M._gen_slot.release()
    text = M.scrub(M.strip_think(text))
    q = M.quality(text)
    print(f"[finetune] org generate chars={q['chars']} ratio={q['letter_ratio']} finish={finish} "
          f"s={time.time() - t0:.1f}")
    return {"org": org, "adapter_sha256": sha, "text": text, "finish_reason": finish, "quality": q,
            "serve": served, "latency_s": round(time.time() - t0, 2)}
