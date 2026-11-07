# swarm-memory: Tinfoil config (public)

This is the public verification recipe for **swarm-memory**, confidential shared memory for multiplayer agents. `tinfoil-config.yml` pins:

- `memory-api`: the swarm-memory image, by sha256 digest
- `vllm`: `vllm/vllm-openai:v0.33.0`, by digest, serving **Qwen3.8-27B** (dm-verity mpk root `19a7de4e…f484`) on a closed network. It isn't exposed through the shim.

Release: `gh workflow run tinfoil-release.yml -f version=v0.0.1` (workflows copied unchanged from tinfoilsh/tinfoil-containers-template).
Fallback model config: `fallback/tinfoil-config.gpt-oss-20b.yml`.
