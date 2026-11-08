# hivemind trainer sidecar (GPU). Runs in the same Tinfoil enclave as memory-api + vLLM.
# Base = the exact vLLM image prod already runs Qwen3.8-27B with on this H200 (torch + CUDA 13 + triton),
# so the trainer adds no second CUDA userspace. We only add peft/accelerate/FLA/bitsandbytes, pinned.
FROM vllm/vllm-openai:qwen38@sha256:4a2f33a884222f7049b983263ad9976f89452bb81affecf5b67d89ad35c1bc31

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 TRITON_CACHE_DIR=/tmp/triton FT_ADAPTER_DIR=/adapters
WORKDIR /srv
COPY trainer/requirements.txt trainer/requirements.txt
# --no-deps for the libs that would otherwise try to swap torch/transformers out from under vLLM
RUN python3 -m pip install --no-cache-dir --no-deps -r trainer/requirements.txt \
 && python3 -c "import torch, transformers, peft, fla, bitsandbytes; print(torch.__version__, transformers.__version__, peft.__version__)"
COPY trainer ./trainer
EXPOSE 8090
ENTRYPOINT []
CMD ["python3", "-m", "uvicorn", "trainer.service:app", "--host", "0.0.0.0", "--port", "8090"]
