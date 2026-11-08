# Private per-org fine-tuning in the enclave ("own the soul")

Lane B, 2026-11-07. Branch `feat/private-finetune` on wakesync/hivemind. Code: `app/finetune.py`, `trainer/`, `tests/finetune_test.py`, `tinfoil/finetune/`.

One sentence: an org's consented memory becomes a LoRA adapter, trained by a sidecar inside the same attested Tinfoil enclave as memory-api and vLLM. Neither the dataset nor the adapter weights ever leave the enclave, and vLLM serves the adapter per request, only to that org's members.

## 1. Threat model

**Protected assets:** org memories (raw text), the derived training JSONL, adapter weights (they are a compressed copy of the memories and can be prompted into regurgitating them), and per-org generations.

**Trusted:** the AMD SEV-SNP CVM and H200 in CC mode (Tinfoil's attestation of both), the measured `tinfoil-config.yml` at a release tag, and the pinned image digests (memory-api, trainer, vLLM) that this config names. Clients verify all of this with `tinfoil attestation verify` / SecureClient before sending anything.

**Untrusted:** Tinfoil operators and the host (iridium-1), the network, other orgs' members, our own admins outside the enclave (no SSH, `debug: false`), and the client of any other org.

**Adversary goals and what stops them**

| Goal | Mitigation |
| - | - |
| Host or operator reads the dataset or weights | Kept only in CVM memory (tmpfs) or on a Tinfoil encrypted volume whose key is released only after attestation. Never written to unencrypted disk. |
| Exfiltrate adapter bytes over the API | No route returns adapter bytes, in memory-api or in the trainer. Tests assert that no export/download/weights path exists in either OpenAPI. The trainer sits on its own closed network with no egress. vLLM's LoRA-load API is reachable only from memory-api. |
| Member of org B uses org A's adapter | Adapters are keyed `(org, sha)`. `/v1/orgs/{org}/generate` checks membership (non-members get 404, the same as for a missing org). The vLLM model name is server-chosen (`org-<org>-<sha16>`), and the caller can't name a model or a path (`extra=forbid`). |
| A training job pulls in data the org didn't consent to | The dataset is built from exactly two SQL sources: rows with `org = <this org>`, only if the admin policy is on, and personal rows of members who opted in for this org. Rows from any other org are unreachable, whatever its wall. An assert also re-checks that every row is `org ∈ {None, this org}`. |
| Membership inference / regurgitation by the org's own members | This is accepted. Members already see org memories through /recall. Personal memories are trained only with the author's explicit opt-in, and withdrawing consent deletes every adapter that included them. PII scrubbing (`scrub`) runs on training targets and on outputs. |
| Malicious adapter loaded into the shared vLLM | Only the trainer writes adapters. vLLM loads them by enclave-local path, and the path is built server-side from a validated org id and a 64-hex sha. |
| Tampered or substituted adapter | The adapter sha256 (over weights + config) is recomputed on every HF-path load and bound into a receipt signed with an attested key. |
| Log leakage | Logs carry counts only (examples, steps, loss, chars, letter ratio). Trainer exception logs print the type and frames but never the message. |

**Not covered:** DP-SGD. Adapters memorise. That's acceptable when the only people who can query an adapter are the members who already own the data, and not acceptable if adapters ever get shared across orgs (see §3). Side channels on the shared GPU are out of scope here, as they are for Tinfoil generally.

## 2. Where training runs

There's a third container, `trainer`, in the **same measured `tinfoil-config.yml`**, on the same single H200.

- Image: `ghcr.io/wakesync/hivemind-trainer@sha256:…`, built from `trainer/Dockerfile`. It's the qwen38 vLLM image with peft 0.21.2 + accelerate + flash-linear-attention 0.6.0 added. Reusing the image already proven to run Qwen3.8-27B on this H200 means the same torch/CUDA 13 stack, so we don't introduce a second CUDA userspace.
- Networks: `train: {}` (closed, memory-api ↔ trainer only). No egress, and the shim doesn't expose it. memory-api authenticates to it with `HMAC(SWARM_SECRET, "hivemind-trainer")`.
- Base weights: the same dm-verity mpk (`/tinfoil/mpk/mpk-19a7de4e…`), mounted read-only. No download happens at runtime.
- GPU sharing: v0.2.2 runs vLLM at `--gpu-memory-utilization 0.90`, which leaves about 14 GB. bf16 LoRA on 27B doesn't fit there (§4). Two modes, chosen by config:
  - **A. Static split (recommended first GPU run).** vLLM drops to 0.50 (≈70 GB: 50 GB weights + ≈15 GB KV/activations; `--max-model-len 16384`, `--max-num-seqs 8`). The trainer gets ≈65 GB and runs QLoRA (nf4 base ≈ 16.4 GB) with room to spare. Both stay up all the time, so serving never pauses. This is the same split pattern Lane A just proved for the two engines at 0.46/0.46.
  - **B. Time-sliced (bf16 LoRA, best quality).** vLLM runs with `VLLM_SERVER_DEV_MODE=1 --enable-sleep-mode`. Before a job, memory-api calls `POST vllm:8001/sleep?level=1` (weights offloaded to CPU RAM, KV freed). The trainer loads bf16 27B (50 GB) and trains. Afterwards the trainer frees the GPU and memory-api calls `/wake_up`. Serving is paused for the duration of the job. Needs `memory: 131072`, because the sleeping weights (50 GB) plus the trainer's host copy have to fit in CVM RAM. Note: dev mode also exposes `/collective_rpc` on vLLM. That's tolerable only because vLLM is on closed networks that nothing but memory-api can reach.
  - Lane A's v0.3.0 already runs two 27B engines at 0.46 each, which leaves ~7 GB. Fine-tuning can't coexist with that, so the trainer config has to replace `vllm-abl` or sit in its own release (see B-READY).

## 3. Data flow and consent (walls)

```
memories (sqlite, tmpfs) ──► consented_rows(org) ──► scrub + topic prompts ──► chat JSONL (in RAM)
                                │                                                   │ sha256 = dataset id
                                │ only: org rows if policy on                       ▼
                                │       personal rows of opted-in members   POST trainer:8090/jobs (closed net)
                                │ never: any other org, any wall                    ▼
                                                                       LoRA train → /adapters/<org>/<sha>/
```

- **Org policy** `POST /v1/orgs/{org}/finetune/policy {org_memories}`, admin only. Turning it off deletes adapters that used org memories.
- **Personal consent** `POST /v1/orgs/{org}/finetune/consent {personal}`, set by the member themselves and scoped per org. Withdrawing it deletes every adapter that included them (it removes the files, unloads the adapter from vLLM, and marks the job `deleted`).
- **Walls:** sealed, signal and open-derived orgs are all excluded from other orgs' datasets. Sealed and signal never release content. For open-derived, the promise is "only a derived answer leaves, never raw". An adapter can reproduce raw text, so training on another org's open-derived notes would break that promise. A future opt-in `share_adapter_with: [orgs]` would need DP-SGD or a distillation step, and isn't built.
- **Examples:** each memory gives 2 Q→A pairs ("What do we know about {topics}?", "Remind me what our notes say about {topic}"), and topics shared by several notes add a summary pair. The system prompt is the org persona (the same one used at serve time). The assistant target is the PII-scrubbed memory. Loss runs only on assistant tokens, and the prompt is rendered with the serving chat template with `enable_thinking=false`.
- The trainer payload carries no user ids. Contributor ids stay in memory-api's sqlite (needed for revoke), and receipts and listings expose only counts.

## 4. LoRA vs QLoRA on 27B: the memory math

Prod base (from the pinned config.json): Qwen3_5ForConditionalGeneration, 64 layers (48 GatedDeltaNet linear-attention + 16 full attention), hidden 5120, vocab 248320. Text params are **26.90 B** (embed 1.27 B, lm_head 1.27 B, body linear 24.35 B). The numbers below come from a meta-device count (`tinfoil/finetune/memory_math.py`).

| Item | bf16 LoRA | QLoRA (nf4 body, bf16 embed/head) |
| - | - | - |
| Base weights | 50.1 GiB | ≈16.4 GiB |
| LoRA r=16 on q,k,v,o,gate,up,down (79.7 M params) | weights 0.15 GiB; fp32 master + grad + Adam ≈1.2 GiB | same |
| Activations, seq 1024, grad checkpointing (64 boundaries × 1024 × 5120 × 2 B) | 0.62 GiB + one layer's recompute (~1-2 GiB) | same |
| Logits: assistant tokens only (bool `logits_to_keep`), ≤1024 × 248k × fp32 ×2 | ≤1.9 GiB (typ. <0.5) | same |
| CUDA context, allocator slack, FLA workspace | ~4-6 GiB | ~4-6 GiB |
| **Trainer total** | **≈58-62 GiB** | **≈25-28 GiB** |

H200 = 141 GB (≈131 GiB usable).
- vLLM at 0.90 (current v0.2.2) leaves ≈13 GiB, which fits neither. **vLLM has to shrink or sleep.**
- vLLM at 0.50 (≈65.5 GiB: 50 GiB weights + ≈13 GiB KV) + QLoRA (≈28 GiB) ≈ 94 GiB. That fits with ~35 GiB headroom. **Mode A.** KV at 13 GiB = 65,536 B/token over the 16 full-attention layers, about 200k tokens of KV plus the GDN state, which is plenty for 16k context × 8 seqs.
- vLLM at 0.50 + bf16 LoRA (≈62 GiB) ≈ 128 GiB, which is too tight to run alongside the engine. **Use mode B (sleep)** for bf16.
- Adapter targets: attention + MLP. The GatedDeltaNet projections (`in_proj_qkv/z/a/b`, `out_proj`) stay frozen by default. vLLM does support them (packed `in_proj_qkvz` / `in_proj_ba`), but that path is newer, and r=16 on MLP + full-attention projections is the usual choice for knowledge injection.
- Throughput guess: about 6 × 27e9 × tokens FLOPs, so ≈1.6e14 per 1k-token example. At ~35% of 990 TFLOPS bf16 that's ≈0.5 s/example (QLoRA dequant ~1.5-2× slower). A 300-example × 3-epoch job is ≈8-15 min, plus 3-6 min to load the base from the mpk.
- Choice: **QLoRA for the first GPU run** (no serving pause, comfortable fit), bf16 + sleep mode as the quality follow-up. One caveat: an adapter trained on an nf4 base and served on a bf16 base has a small mismatch. That's standard QLoRA practice and usually fine. If quality suffers, use mode B.

Trainer library: plain **peft 0.21.2 + a 60-line torch loop**. Unsloth was considered. It's fast, but it patches model classes, its support for this brand-new hybrid arch is unverified, and it would add a large unpinned surface to a measured image. trl's SFTTrainer adds nothing we need. The custom loop lets us compute logits only on supervised positions (boolean `logits_to_keep`), which is the single biggest memory saver with a 248k vocab. Speed comes from FLA Triton kernels for the GDN layers, gradient checkpointing, and batch=1 with grad accumulation (no padding waste).

## 5. Adapter storage

- Path: `/adapters/<org>/<adapter_sha256>/{adapter_model.safetensors, adapter_config.json}`. The sha is the content id, `sha256("adapter_model.safetensors:<w>\nadapter_config.json:<c>\n")`.
- Default: a `/adapters` tmpfs (2 GiB ≈ 13 adapters at r=16), shared by trainer + vLLM through a named in-memory Docker volume (`volumes: [adapters:/adapters]`, which Tinfoil documents as "stored in memory"). It's lost on reboot or update, the same as memories (`/data` is tmpfs too), so the adapter's lifetime matches the data it came from.
- Optional durable mode: a Tinfoil encrypted persistent volume (`persistent_volumes: [ftstore:/adapters]`, `key-secret` released by our keyserver only to the attested config). Tinfoil never sees the key. Only turn this on together with durable memories.
- Written atomically (`.tmp-<job>` then rename). Hard-deleted on revoke. Saved bf16. The adapter config carries `base_model_name_or_path: "hivemind-base"`, so no paths leak.
- Keys are written in the **checkpoint layout** (`base_model.model.model.language_model.layers.N…`). We train on the text-only `Qwen3_5ForCausalLM` view and remap on export. vLLM's `Qwen3_5ForConditionalGeneration.hf_to_vllm_mapper` maps `model.language_model.` → `language_model.model.` for LoRA names exactly as it does for weights. The test asserts that every adapter target names a real weight in the base checkpoint.

## 6. Serving

- vLLM flags added: `--enable-lora --max-loras 4 --max-lora-rank 16 --max-cpu-loras 16`, plus env `VLLM_ALLOW_RUNTIME_LORA_UPDATING=True` and the `adapters` volume mounted read-only.
- On the first org generate, memory-api calls `POST vllm:8001/v1/load_lora_adapter {lora_name: "org-<org>-<sha16>", lora_path: "/adapters/<org>/<sha>"}` and caches it. Requests then use `model: "org-<org>-<sha16>"` (vLLM applies the LoRA per request, so the base and N orgs batch together). Revoke calls `unload_lora_adapter`.
- Cost: with `--max-lora-rank 16`, `--max-loras 4` reserves roughly 4 × 80 M × 2 B ≈ 0.6 GiB of GPU memory for LoRA slots, and punica kernels add ~10-20% latency on LoRA requests.
- Fallback (`FT_SERVE=trainer`): the trainer serves `POST /generate` with HF + PEFT, attaching adapters to its resident base. That's the path the CPU tests use, and it's a safety net if vLLM LoRA misbehaves on the qwen38 image.
- Version risk: the qwen38 image is vLLM `wtn-3a09141`, built 2026-08-12. The vLLM upstream `qwen3_5.py` as of 2026-08-07 already has `SupportsLoRA` + the packed GDN mapping, so LoRA support should be in the image, but that's unverified until the GPU run (B-READY test step 3).

## 7. Attestation story

1. The client verifies the enclave (`tinfoil attestation verify --repo wakesync/hivemind`). The measured config pins the trainer image digest, the vLLM flags (`--enable-lora`), the closed `train` network with no egress, and that the shim exposes only memory-api. That's what proves no exfiltration path exists.
2. Optional signed receipt: the config declares an attested key, `attested-keys: [{id: hivemind-receipts, key: ed25519}]`, granted to memory-api. Its public key appears in the attestation document's `crypto_material`. When a job finishes, memory-api signs canonical JSON: `{v, kind: "hivemind.org-adapter", org, job_id, adapter_sha256, weights_sha256, dataset_sha256, examples, memories, contributors (count), base (mpk root), hparams, targets, created, enclave_attestation_sha256, exportable: false}`.
3. Anyone holding the receipt plus the attestation can check that this adapter hash was produced for this org, from a dataset of this hash, by this measured code, and that the code has no export route. Each org generate response carries `adapter_sha256` so members can tie answers to the receipt.
4. The key rotates on every enclave boot (Tinfoil semantics), so receipts reference the attestation they were made under. Without the key grant, receipts are unsigned (`sig_ed25519: null`), which is still useful as a hash binding but has to be verified out of band.

## 8. API (memory-api, HMAC headers as today)

| Route | Who | Does |
| - | - | - |
| `POST /v1/orgs/{org}/finetune/policy {org_memories: bool}` | admin | opt the org's memories in or out; out deletes affected adapters |
| `POST /v1/orgs/{org}/finetune/consent {personal: bool}` | the member | opt your personal memories in or out for this org; out deletes adapters that used them |
| `POST /v1/orgs/{org}/finetune {r?, epochs?, lr?, max_steps?, max_len?, grad_accum?}` | admin | 202 `{job_id, dataset:{examples, memories, contributors, sha256}}`; 409 if nothing is consented or a job is already running (one per enclave) |
| `GET /v1/orgs/{org}/finetune/{job_id}` | member | `queued/loading/training/done/failed/deleted`, step/total/loss, result hashes, and the receipt when done |
| `GET /v1/orgs/{org}/adapters` | member | adapter hashes + receipts (contributor COUNT only) |
| `DELETE /v1/orgs/{org}/adapters` | admin | hard delete all adapters of the org |
| `POST /v1/orgs/{org}/generate {messages, max_tokens}` | member | generate with the newest org adapter; returns `text, finish_reason, adapter_sha256, quality` |

Env: `FT_ENABLED` (default 0, so the code is inert unless a config turns it on), `FT_TRAINER_URL`, `FT_SERVE` (vllm|trainer), `FT_ADAPTER_ROOT`, `FT_MAX_RANK`, `FT_BASE_ID`, `RECEIPT_KEY_DIR`.

## 9. Tests (CPU, this box)

`tests/finetune_test.py` runs memory-api and the trainer in process, with real PEFT training on a real Qwen3.5-family model, and mocks only the vLLM engine. See B-STATUS.md for the run receipts:
- `tiny-random/qwen3.5` (4 M params, same arch): pipeline check, ~1 min.
- `Qwen/Qwen3.5-0.8B` (same `Qwen3_5ForConditionalGeneration` checkpoint layout as prod 27B), strict mode: the adapter has to recall the org fact ("north shelf") and the base alone must not.

Coverage: consent (no-consent 409, sealed/signal/open-derived/non-consented personal canaries absent from both the dataset and the trainer payload), auth (admin/member/non-member, trainer bearer), the one-job lock, the adapter file layout + vLLM-compat config, the receipt signature, no export route, member-only generate, cross-org dir refusal, the vLLM load/chat request shape (org-scoped name, enclave path, loaded once), and revoke → files gone + unload + 404.
