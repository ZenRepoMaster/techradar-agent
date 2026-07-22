"""Embedding model wrapper.

Model: BAAI/bge-small-en-v1.5 via fastembed (ONNX, CPU-friendly — the dev
machine is an Intel Mac where torch wheels are no longer published, and a
384-dim model keeps a 50k+ corpus index small and fast).

Alternatives considered (see docs/DESIGN.md): all-MiniLM-L6-v2 (slightly
weaker on retrieval benchmarks at the same size), bge-base (better quality,
~3x slower on CPU — poor fit for a local-only constraint at this scale).

bge queries benefit from an instruction prefix; ``embed_query`` applies it,
``embed_passages`` does not — this asymmetry is part of the model contract.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Iterable

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIM = 384
_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


@lru_cache(maxsize=1)
def _model():
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=MODEL_NAME)


def embed_passages(texts: list[str], batch_size: int = 256) -> Iterable[list[float]]:
    for vec in _model().embed(texts, batch_size=batch_size):
        yield vec.tolist()


def embed_query(query: str) -> list[float]:
    vec = next(iter(_model().embed([_QUERY_PREFIX + query])))
    return vec.tolist()
