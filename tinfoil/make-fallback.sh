#!/usr/bin/env bash
# Regenerate the gpt-oss-20b fallback config from the primary Qwen config.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p fallback
ROOT=fa8945a2fd0ffe7ddfd31b5da869e2c7c045c971e0230e2fd500fc125315f0bf
sed -e 's|^  # FALLBACK.*|  # FALLBACK config: gpt-oss-20b (MXFP4, ~14GB). Swap in for tinfoil-config.yml if Qwen misbehaves.|' \
    -e 's|tinfoil model wrap Qwen/Qwen3.8-27B|tinfoil model wrap openai/gpt-oss-20b|' \
    -e 's|repo: "Qwen/Qwen3.8-27B@[0-9a-f]*"|repo: "openai/gpt-oss-20b@6cee5e81ee83917806bbde320786a8fb61efebee"|' \
    -e "s|mpk: \"[^\"]*\"|mpk: \"${ROOT}_41301471232_8b6516c9-adf7-5235-a86a-f135dba359c1\"|" \
    -e "s|/tinfoil/mpk/mpk-[A-Za-z0-9_]*|/tinfoil/mpk/mpk-${ROOT}|" \
    -e 's|"qwen3.8-27b"|"gpt-oss-20b"|g' \
    -e 's|"--reasoning-parser", "qwen3"|"--reasoning-parser", "openai_gptoss"|' \
    -e 's|Qwen3.8-27B (bf16, ~54GB)|gpt-oss-20b (MXFP4, ~14GB)|' \
    tinfoil-config.yml > fallback/tinfoil-config.gpt-oss-20b.yml
echo wrote fallback/tinfoil-config.gpt-oss-20b.yml
