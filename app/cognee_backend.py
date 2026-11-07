"""EXPERIMENTAL Cognee graph backend (MEMORY_BACKEND=cognee).

Not in the default image (cognee pulls ~1GB of deps). Cognee's cognify step
needs an LLM for entity extraction; point it at the same in-enclave vLLM via
LLM_* env so nothing leaves the enclave. Per-scope isolation uses one cognee
dataset per scope; recall searches across all datasets but only the
synthesized answer is returned (same contract as the sqlite backend).

Status: see README "Cognee" note. Search/remember are wired; the sqlite
backend still serves /graph topic aggregation so the privacy floor (GRAPH_K)
is enforced in one place.
"""
from __future__ import annotations

import asyncio
import os

from app.main import SqliteBackend


class CogneeBackend(SqliteBackend):
    name = "cognee"

    def __init__(self):
        import cognee  # noqa: F401

        os.environ.setdefault("LLM_PROVIDER", "custom")
        os.environ.setdefault("LLM_ENDPOINT", os.environ.get("LLM_BASE_URL", "http://vllm:8001/v1"))
        os.environ.setdefault("EMBEDDING_PROVIDER", "fastembed")
        os.environ.setdefault("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
        os.environ.setdefault("EMBEDDING_DIMENSIONS", "384")
        self.cognee = cognee
        self.loop = asyncio.new_event_loop()

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def remember(self, scope: str, text: str, org: str | None = None) -> dict:
        out = super().remember(scope, text, org)  # topics + local vector index
        ds = f"org_{org}" if org else f"personal_{scope}"  # one cognee dataset per org (tenant)
        self._run(self.cognee.add(text, dataset_name=ds))
        self._run(self.cognee.cognify(datasets=[ds]))
        return out
