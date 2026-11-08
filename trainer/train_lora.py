"""In-enclave LoRA trainer for hivemind org adapters ("own the soul").

Runs inside the same attested Tinfoil enclave as memory-api and vLLM. Input is a
list of chat examples built by memory-api from the org's consented memory; output
is a PEFT LoRA adapter written to the enclave-local adapter volume, in the exact
key layout the serving vLLM expects for Qwen3_5ForConditionalGeneration. Nothing
here talks to the network, and nothing logs content (counts, loss, timings only).

Plain peft + a small torch loop, not trl/unsloth: one dependency less in the
measured image, and we need a non-standard loss (assistant-only tokens computed
via a boolean logits_to_keep mask so the 248k-vocab logits are only materialised
for supervised positions). The GatedDeltaNet layers use flash-linear-attention
kernels when installed (GPU image) and the reference torch path otherwise (CPU tests).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import time
from pathlib import Path

import torch
import torch.nn.functional as F

# Attention + MLP projections in every layer. vLLM packs q/k/v -> qkv_proj and
# gate/up -> gate_up_proj; a PEFT adapter on the unpacked names loads onto the packed
# layers. The GatedDeltaNet projections (in_proj_qkv/in_proj_z/in_proj_a/in_proj_b/
# out_proj) are left frozen by default: smaller adapter, and the packed in_proj_qkvz
# LoRA path in vLLM is newer and less exercised.
DEFAULT_TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]

# Trained on the text-only Qwen3_5ForCausalLM view (keys model.layers.N...). The served
# checkpoint is Qwen3_5ForConditionalGeneration (keys model.language_model.layers.N...),
# and vLLM maps adapter names through that model's hf_to_vllm_mapper. Export in the
# checkpoint layout so the adapter names line up 1:1 with the base weights.
PEFT_TEXT_PREFIX = "base_model.model.model."
PEFT_SERVE_PREFIX = "base_model.model.model.language_model."

IM_END = "<|im_end|>"
HPARAM_LIMITS = {"r": (4, 64), "alpha": (4, 128), "epochs": (1, 10), "max_steps": (1, 5000),
                 "lr": (1e-6, 5e-3), "max_len": (64, 4096), "grad_accum": (1, 64)}
DEFAULTS = {"r": 16, "alpha": 32, "epochs": 3, "max_steps": 600, "lr": 2e-4, "max_len": 1024,
            "grad_accum": 4, "dropout": 0.0, "seed": 0}


def clamp_hparams(h: dict | None) -> dict:
    out = dict(DEFAULTS)
    for k, v in (h or {}).items():
        if k not in DEFAULTS:
            raise ValueError(f"unknown hparam {k}")
        out[k] = v
    for k, (lo, hi) in HPARAM_LIMITS.items():
        if not (lo <= out[k] <= hi):
            raise ValueError(f"hparam {k} out of range [{lo}, {hi}]")
    for k in ("r", "alpha", "epochs", "max_steps", "max_len", "grad_accum", "seed"):
        out[k] = int(out[k])
    out["lr"] = float(out["lr"])
    out["dropout"] = float(out["dropout"])
    return out


def pick_device() -> str:
    want = os.environ.get("FT_DEVICE", "")
    if want:
        return want
    return "cuda" if torch.cuda.is_available() else "cpu"


def load_base(path: str, device: str | None = None, quant: str | None = None):
    """Base model + tokenizer from the verified, read-only model mount."""
    from transformers import AutoModelForCausalLM, AutoTokenizer

    device = device or pick_device()
    tok = AutoTokenizer.from_pretrained(path)
    kw: dict = {}
    if device == "cuda":
        kw["dtype"] = torch.bfloat16
        kw["device_map"] = {"": 0}
        if quant == "nf4":  # QLoRA fallback when the GPU budget can't hold bf16 weights
            from transformers import BitsAndBytesConfig

            kw["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=torch.bfloat16)
    else:
        kw["dtype"] = torch.float32
    model = AutoModelForCausalLM.from_pretrained(path, **kw)
    model.config.use_cache = False
    for p in model.parameters():
        p.requires_grad_(False)
    return tok, model


def render(tok, messages: list[dict]) -> tuple[list[int], list[int]]:
    """Token ids + labels. Supervise only the final assistant turn (+ <|im_end|>).
    Prompt rendering matches the serving chat template with thinking disabled."""
    if not messages or messages[-1]["role"] != "assistant":
        raise ValueError("example must end with an assistant turn")
    prompt = tok.apply_chat_template(messages[:-1], tokenize=False, add_generation_prompt=True,
                                     enable_thinking=False)
    p_ids = tok(prompt, add_special_tokens=False)["input_ids"]
    a_ids = tok(messages[-1]["content"].strip() + IM_END, add_special_tokens=False)["input_ids"]
    return p_ids + a_ids, [-100] * len(p_ids) + a_ids


def encode_all(tok, examples: list[dict], max_len: int) -> list[dict]:
    out = []
    for ex in examples:
        ids, labels = render(tok, ex["messages"])
        if len(ids) > max_len:  # keep the head of the prompt + as much answer as fits
            ids, labels = ids[:max_len], labels[:max_len]
        if sum(1 for x in labels[1:] if x != -100) == 0:
            continue
        out.append({"input_ids": ids, "labels": labels})
    return out


def step_loss(model, ex: dict, device: str) -> torch.Tensor:
    ids = torch.tensor([ex["input_ids"]], device=device)
    lab = torch.tensor([ex["labels"]], device=device)
    tgt = lab[:, 1:]
    keep = torch.zeros_like(lab, dtype=torch.bool)
    keep[:, :-1] = tgt != -100
    # run the decoder, then lm_head ONLY on supervised positions -> (n, vocab). With a 248k vocab
    # this is the biggest activation saving. Done by hand (not logits_to_keep) so it works on
    # whatever transformers version the GPU base image ships.
    lm = model.get_base_model() if hasattr(model, "get_base_model") else model
    h = lm.model(input_ids=ids, use_cache=False).last_hidden_state
    logits = lm.lm_head(h[keep])
    return F.cross_entropy(logits.float(), tgt[tgt != -100])


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export_adapter(pm, out_dir: Path) -> dict:
    """Write adapter_model.safetensors + adapter_config.json in serving layout."""
    from peft import get_peft_model_state_dict
    from safetensors.torch import save_file

    sd = get_peft_model_state_dict(pm)
    out = {}
    for k, v in sd.items():
        if not k.startswith(PEFT_TEXT_PREFIX):
            raise ValueError(f"unexpected adapter key {k}")
        out[PEFT_SERVE_PREFIX + k[len(PEFT_TEXT_PREFIX):]] = v.detach().to(torch.bfloat16).cpu().contiguous()
    cfg = pm.peft_config["default"].to_dict()
    cfg = {k: (sorted(v) if isinstance(v, (set, frozenset)) else v) for k, v in cfg.items()}
    cfg["base_model_name_or_path"] = "hivemind-base"  # no host/mount paths in the artifact
    cfg["revision"] = None
    validate_vllm_compat(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    save_file(out, str(out_dir / "adapter_model.safetensors"), metadata={"format": "pt"})
    (out_dir / "adapter_config.json").write_text(json.dumps(cfg, sort_keys=True, indent=1, default=str))
    return {"tensors": len(out), "params": int(sum(v.numel() for v in out.values()))}


def validate_vllm_compat(cfg: dict):
    """Mirror of vLLM PEFTHelper validation (no modules_to_save/DoRA/bias)."""
    errs = []
    if cfg.get("modules_to_save"):
        errs.append("modules_to_save")
    if cfg.get("use_dora"):
        errs.append("use_dora")
    if cfg.get("bias", "none") != "none":
        errs.append("bias")
    if str(cfg.get("peft_type")) not in ("LORA", "PeftType.LORA"):
        errs.append("peft_type")
    if errs:
        raise ValueError(f"adapter not vLLM-compatible: {errs}")


def adapter_digest(out_dir: Path) -> dict:
    w = sha256_file(out_dir / "adapter_model.safetensors")
    c = sha256_file(out_dir / "adapter_config.json")
    # one id over both files: what the receipt binds to the org
    both = hashlib.sha256(f"adapter_model.safetensors:{w}\nadapter_config.json:{c}\n".encode()).hexdigest()
    return {"adapter_sha256": both, "weights_sha256": w, "config_sha256": c}


def train(base, examples: list[dict], out_dir: str | Path, hparams: dict | None = None,
          targets: list[str] | None = None, log=print, progress=None) -> dict:
    """base = (tokenizer, model) from load_base. Trains one LoRA, exports it, and
    removes the LoRA layers again so the same base can serve the next job."""
    from peft import LoraConfig, get_peft_model

    tok, model = base
    hp = clamp_hparams(hparams)
    device = next(model.parameters()).device.type
    random.seed(hp["seed"])
    torch.manual_seed(hp["seed"])
    data = encode_all(tok, examples, hp["max_len"])
    if not data:
        raise ValueError("no trainable examples")
    total_steps = min(hp["max_steps"], math.ceil(len(data) * hp["epochs"] / hp["grad_accum"]))
    cfg = LoraConfig(r=hp["r"], lora_alpha=hp["alpha"], lora_dropout=hp["dropout"], bias="none",
                     target_modules=targets or DEFAULT_TARGETS, task_type="CAUSAL_LM")
    if device == "cuda":
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        model.enable_input_require_grads()
    pm = get_peft_model(model, cfg)
    params = [p for p in pm.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(params, lr=hp["lr"], weight_decay=0.0, betas=(0.9, 0.999))
    warm = max(1, total_steps // 10)

    def lr_at(s):
        if s < warm:
            return (s + 1) / warm
        return 0.5 * (1 + math.cos(math.pi * (s - warm) / max(1, total_steps - warm)))

    sched = torch.optim.lr_scheduler.LambdaLR(opt, lr_at)
    pm.train()
    t0 = time.time()
    order: list[int] = []
    losses: list[float] = []
    first = None
    step = 0
    tokens = 0
    try:
        while step < total_steps:
            acc = 0.0
            for _ in range(hp["grad_accum"]):
                if not order:
                    order = list(range(len(data)))
                    random.shuffle(order)
                ex = data[order.pop()]
                loss = step_loss(pm, ex, device) / hp["grad_accum"]
                loss.backward()
                acc += float(loss.detach())
                tokens += len(ex["input_ids"])
            torch.nn.utils.clip_grad_norm_(params, 1.0)
            opt.step()
            sched.step()
            opt.zero_grad(set_to_none=True)
            step += 1
            losses.append(acc)
            first = acc if first is None else first
            if progress:
                progress(step, total_steps, acc)
            if step == 1 or step % 10 == 0 or step == total_steps:
                log(f"[trainer] step {step}/{total_steps} loss {acc:.4f} tok {tokens} s {time.time() - t0:.1f}")
        out_dir = Path(out_dir)
        stats = export_adapter(pm, out_dir)
    finally:
        # strip LoRA layers (no merge): the base is untouched for the next org
        restored = pm.unload()
        if device == "cuda":
            restored.gradient_checkpointing_disable()
        base_model_ref = restored
        del pm, opt, params
        if device == "cuda":
            torch.cuda.empty_cache()
    tail = losses[-max(1, len(losses) // 5):]
    return {**adapter_digest(out_dir), **stats, "steps": step, "examples": len(data), "tokens": tokens,
            "loss_first": round(first, 4), "loss_last": round(sum(tail) / len(tail), 4),
            "seconds": round(time.time() - t0, 1), "hparams": hp,
            "targets": sorted(targets or DEFAULT_TARGETS), "_model": base_model_ref}


# ------------------------------------------------------------ serving-side helpers (tests)
def serve_to_text_key(k: str) -> str:
    if not k.startswith(PEFT_SERVE_PREFIX):
        raise ValueError(f"adapter key not in serving layout: {k}")
    return PEFT_TEXT_PREFIX + k[len(PEFT_SERVE_PREFIX):]


def load_adapter_into(pm_or_model, adapter_dir: str | Path, name: str):
    """Load an exported adapter onto a text-only HF model under `name`, strictly (every
    tensor must land on a LoRA module), mimicking vLLM's per-request adapter registry."""
    from peft import LoraConfig, PeftModel, get_peft_model, set_peft_model_state_dict
    from safetensors.torch import load_file

    adapter_dir = Path(adapter_dir)
    cfg = json.loads((adapter_dir / "adapter_config.json").read_text())
    validate_vllm_compat(cfg)
    lc = LoraConfig(r=cfg["r"], lora_alpha=cfg["lora_alpha"], target_modules=cfg["target_modules"],
                    lora_dropout=0.0, bias="none", task_type="CAUSAL_LM")
    if isinstance(pm_or_model, PeftModel):
        pm = pm_or_model
        pm.add_adapter(name, lc)
    else:
        pm = get_peft_model(pm_or_model, lc, adapter_name=name)
    sd = {serve_to_text_key(k): v.float() for k, v in load_file(str(adapter_dir / "adapter_model.safetensors")).items()}
    res = set_peft_model_state_dict(pm, sd, adapter_name=name)
    unexpected = list(getattr(res, "unexpected_keys", []) or [])
    if unexpected:
        raise ValueError(f"adapter tensors without a LoRA module: {unexpected[:4]}")
    missing = [k for k in (getattr(res, "missing_keys", []) or []) if f".{name}." in k and "lora_" in k]
    if missing:
        raise ValueError(f"LoRA modules without adapter tensors: {missing[:4]}")
    pm.eval()
    return pm
