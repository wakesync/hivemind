"""hivemind trainer sidecar: in-enclave LoRA jobs + adapter store.

Only memory-api talks to this service, over the closed `train` network, with a token
derived from the shared SWARM_SECRET (HMAC(secret, "hivemind-trainer")). No shim
path reaches it and its network has no egress, so datasets and adapter weights
never leave the enclave. The service has no export route: there is no endpoint
that returns adapter bytes, only their hashes.

  POST   /jobs              {job_id, org, examples, hparams}   202 (409 if a job is running)
  GET    /jobs/{job_id}     state / progress / result hashes
  POST   /generate          {org, adapter_sha256, messages, max_tokens}  (HF fallback serving)
  DELETE /adapters/{org}    remove every adapter of the org
  GET    /healthz
"""
from __future__ import annotations

import gc
import hashlib
import hmac
import os
import re
import shutil
import threading
import time
import traceback
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

BASE_MODEL = os.environ.get("FT_BASE_MODEL", "/tinfoil/models/qwen3.8-27b")
ADAPTER_DIR = Path(os.environ.get("FT_ADAPTER_DIR", "/adapters"))
QUANT = os.environ.get("FT_QUANT", "")  # "" = bf16 LoRA, "nf4" = QLoRA
KEEP_BASE = os.environ.get("FT_KEEP_BASE", "0") == "1"  # CPU tests: keep the base resident
MAX_EXAMPLES = int(os.environ.get("FT_MAX_EXAMPLES", "4096"))
SERVE_GENERATE = os.environ.get("FT_SERVE_GENERATE", "1") == "1"
TOKEN_SALT = b"hivemind-trainer"
ORG_RE = r"^[a-z0-9][a-z0-9-]{1,40}$"

app = FastAPI(title="hivemind-trainer", version="0.1.0")
_jobs: dict[str, dict] = {}
_gen_lock = threading.Lock()
_busy = threading.Lock()  # one GPU job at a time
_base_lock = threading.Lock()
_base = None


def internal_token() -> str:
    secret = os.environ.get("SWARM_SECRET", "")
    if not secret:
        return ""
    return hmac.new(secret.encode(), TOKEN_SALT, hashlib.sha256).hexdigest()


def auth(authorization: str = Header("")):
    want = internal_token()
    if not want:
        raise HTTPException(503, "SWARM_SECRET not configured")
    got = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(want, got):
        raise HTTPException(401, "bad token")


def get_base():
    global _base
    with _base_lock:
        if _base is None:
            from trainer.train_lora import load_base

            _base = load_base(BASE_MODEL, quant=QUANT or None)
        return _base


def drop_base():
    global _base
    if KEEP_BASE:
        return
    with _base_lock:
        _base = None
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


class Msg(BaseModel):
    model_config = {"extra": "forbid"}
    role: str = Field(..., pattern="^(system|user|assistant)$")
    content: str = Field(..., min_length=1, max_length=16000)


class Example(BaseModel):
    model_config = {"extra": "forbid"}
    messages: list[Msg] = Field(..., min_length=2, max_length=16)


class JobIn(BaseModel):
    model_config = {"extra": "forbid"}
    job_id: str = Field(..., pattern=r"^[a-f0-9]{16,64}$")
    org: str = Field(..., pattern=ORG_RE)
    examples: list[Example] = Field(..., min_length=1)
    hparams: dict = Field(default_factory=dict)


class GenIn(BaseModel):
    model_config = {"extra": "forbid"}
    org: str = Field(..., pattern=ORG_RE)
    adapter_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    messages: list[Msg] = Field(..., min_length=1, max_length=16)
    max_tokens: int = Field(128, ge=1, le=1024)


def org_dir(org: str) -> Path:
    return ADAPTER_DIR / org


def run_job(job: dict, examples: list[dict], hparams: dict):
    from trainer.train_lora import train

    try:
        job["state"] = "loading"
        base = get_base()
        from peft import PeftModel

        if isinstance(base[1], PeftModel):  # adapters attached by /generate: strip them first
            with _gen_lock:
                base = (base[0], base[1].unload())
                globals()["_base"] = base
        job["state"] = "training"
        tmp = org_dir(job["org"]) / f".tmp-{job['job_id']}"

        def progress(step, total, loss):
            job.update(step=step, total=total, loss=round(loss, 4))

        res = train(base, examples, tmp, hparams, progress=progress,
                    log=lambda s: print(s, flush=True))
        _base_tok, _ = base
        globals()["_base"] = (_base_tok, res.pop("_model"))
        final = org_dir(job["org"]) / res["adapter_sha256"]
        if final.exists():
            shutil.rmtree(tmp)
        else:
            tmp.rename(final)
        job.update(state="done", result=res, finished=time.time())
        print(f"[trainer] job {job['job_id'][:8]} done steps={res['steps']} ex={res['examples']} "
              f"loss {res['loss_first']}->{res['loss_last']} s={res['seconds']}", flush=True)
    except Exception as e:
        job.update(state="failed", error=type(e).__name__, finished=time.time())
        # content-free: exception type + frames only, never the message (could echo data)
        print(f"[trainer] job {job['job_id'][:8]} failed {type(e).__name__}", flush=True)
        traceback.print_tb(e.__traceback__)
        shutil.rmtree(org_dir(job["org"]) / f".tmp-{job['job_id']}", ignore_errors=True)
    finally:
        if not SERVE_GENERATE:
            drop_base()
        _busy.release()


@app.get("/healthz")
def healthz():
    return {"ok": True, "busy": _busy.locked(), "quant": QUANT or "bf16", "jobs": len(_jobs)}


@app.post("/jobs", status_code=202, dependencies=[Depends(auth)])
def submit(body: JobIn):
    if len(body.examples) > MAX_EXAMPLES:
        raise HTTPException(413, f"at most {MAX_EXAMPLES} examples")
    if body.job_id in _jobs:
        raise HTTPException(409, "job exists")
    if not _busy.acquire(blocking=False):
        raise HTTPException(409, "trainer busy")
    try:
        from trainer.train_lora import clamp_hparams

        hp = clamp_hparams(body.hparams)
    except ValueError as e:
        _busy.release()
        raise HTTPException(422, str(e))
    job = {"job_id": body.job_id, "org": body.org, "state": "queued", "step": 0, "total": None,
           "loss": None, "created": time.time(), "result": None, "error": None}
    _jobs[body.job_id] = job
    ex = [e.model_dump() for e in body.examples]
    threading.Thread(target=run_job, args=(job, ex, hp), daemon=True).start()
    return {"job_id": body.job_id, "state": "queued"}


@app.get("/jobs/{job_id}", dependencies=[Depends(auth)])
def status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return job


@app.delete("/adapters/{org}/{sha}", dependencies=[Depends(auth)])
def delete_adapter(org: str, sha: str):
    """Hard delete one adapter (dir + any copy attached to the in-memory base)."""
    if not re.fullmatch(ORG_RE, org) or not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise HTTPException(400, "bad id")
    d = org_dir(org) / sha
    if not d.exists():
        raise HTTPException(404, "no such adapter")
    shutil.rmtree(d)
    detach(f"org_{org.replace('-', '_')}_{sha[:12]}")
    return {"org": org, "deleted": 1}


@app.delete("/adapters/{org}", dependencies=[Depends(auth)])
def delete_adapters(org: str):
    if not re.fullmatch(ORG_RE, org):
        raise HTTPException(400, "bad org")
    d = org_dir(org)
    n = len([p for p in d.iterdir()]) if d.exists() else 0
    shutil.rmtree(d, ignore_errors=True)
    detach(f"org_{org.replace('-', '_')}_")
    return {"org": org, "deleted": n}


def detach(tag: str):
    with _gen_lock:
        if _base is not None:
            from peft import PeftModel

            m = _base[1]
            if not isinstance(m, PeftModel):
                return
            doomed = [a for a in list(m.peft_config) if a.startswith(tag)]
            if len(doomed) == len(m.peft_config):
                globals()["_base"] = (_base[0], m.unload())  # strip all LoRA layers
                return
            keep = [a for a in m.peft_config if a not in doomed]
            m.set_adapter(keep[0])
            for name in doomed:
                m.delete_adapter(name)


@app.post("/generate", dependencies=[Depends(auth)])
def generate(body: GenIn):
    """HF + PEFT serving path. Used on CPU tests and as the fallback when vLLM
    LoRA serving isn't enabled. Adapter is looked up by (org, sha): one org can
    never name another org's directory."""
    if not SERVE_GENERATE:
        raise HTTPException(503, "generate disabled on trainer")
    path = org_dir(body.org) / body.adapter_sha256
    if not (path / "adapter_model.safetensors").exists():
        raise HTTPException(404, "no such adapter")
    if _busy.locked():
        raise HTTPException(429, "trainer busy")
    import torch
    from peft import PeftModel

    from trainer.train_lora import adapter_digest, load_adapter_into

    if adapter_digest(path)["adapter_sha256"] != body.adapter_sha256:
        raise HTTPException(409, "adapter hash mismatch")
    with _gen_lock:
        tok, model = get_base()
        name = f"org_{body.org.replace('-', '_')}_{body.adapter_sha256[:12]}"
        if not isinstance(model, PeftModel) or name not in model.peft_config:
            model = load_adapter_into(model, path, name)
            globals()["_base"] = (tok, model)
        model.set_adapter(name)
        prompt = tok.apply_chat_template([m.model_dump() for m in body.messages], tokenize=False,
                                         add_generation_prompt=True, enable_thinking=False)
        ids = tok(prompt, return_tensors="pt", add_special_tokens=False).to(model.device)
        t0 = time.time()
        with torch.no_grad():
            out = model.generate(**ids, max_new_tokens=body.max_tokens, do_sample=False,
                                 eos_token_id=tok.convert_tokens_to_ids("<|im_end|>"),
                                 pad_token_id=tok.pad_token_id or tok.eos_token_id)
        new = out[0][ids["input_ids"].shape[1]:]
        text = tok.decode(new, skip_special_tokens=True)
        finish = "stop" if len(new) < body.max_tokens else "length"
    return {"text": text, "finish_reason": finish, "tokens": int(len(new)),
            "latency_s": round(time.time() - t0, 2)}
