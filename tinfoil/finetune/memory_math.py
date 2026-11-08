"""Parameter + LoRA size accounting for the prod base (meta device, no weights)."""
import json
import urllib.request

import torch
from transformers import AutoModelForCausalLM

URL = "https://huggingface.co/Qwen/Qwen3.8-27B/resolve/37561816e66b5b9153b1b64c10968c18c1e6f4a0/config.json"
raw = json.load(urllib.request.urlopen(URL))


from transformers import Qwen3_5Config  # noqa: E402

cfg = Qwen3_5Config(**raw)
with torch.device("meta"):
    m = AutoModelForCausalLM.from_config(cfg.text_config)
tot = sum(p.numel() for p in m.parameters())
emb = m.model.embed_tokens.weight.numel()
head = m.lm_head.weight.numel()
lin = sum(mod.weight.numel() for n, mod in m.named_modules()
          if isinstance(mod, torch.nn.Linear) and n != "lm_head")
print(f"text params {tot/1e9:.3f}B  embed {emb/1e9:.3f}B  lm_head {head/1e9:.3f}B  body-linear {lin/1e9:.3f}B")
print(f"bf16 weights {tot*2/2**30:.1f} GiB; nf4 body + bf16 embed/head ~ {(lin*0.5*1.03 + (emb+head)*2 + (tot-lin-emb-head)*2)/2**30:.1f} GiB")
for targets in (["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
                ["gate_proj", "up_proj", "down_proj"]):
    for r in (16, 32):
        n = 0
        for name, mod in m.named_modules():
            if isinstance(mod, torch.nn.Linear) and name.split(".")[-1] in targets:
                n += r * (mod.in_features + mod.out_features)
        print(f"r={r} targets={len(targets)} lora params {n/1e6:.1f}M  bf16 {n*2/2**20:.0f} MiB  "
              f"train state fp32 p+g+adam {n*16/2**30:.2f} GiB")
full = [i for i, t in enumerate(cfg.text_config.layer_types) if t == "full_attention"]
kv_tok = len(full) * cfg.text_config.num_key_value_heads * cfg.text_config.head_dim * 2 * 2
print(f"full-attn layers {len(full)}; KV bytes/token {kv_tok}; 32k ctx seq {kv_tok*32768/2**30:.2f} GiB")
h = cfg.text_config.hidden_size
L = cfg.text_config.num_hidden_layers
V = cfg.text_config.vocab_size
for T in (1024, 2048):
    ckpt = L * T * h * 2
    logits = T * V * 4 * 2
    print(f"seq {T}: grad-ckpt boundaries {ckpt/2**30:.2f} GiB, fp32 logits+grad {logits/2**30:.2f} GiB")
