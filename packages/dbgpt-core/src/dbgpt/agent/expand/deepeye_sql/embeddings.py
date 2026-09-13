"""Embedding providers for DeepEye-SQL value/few-shot retrieval.

The paper uses ``Qwen3-Embedding-0.6B`` via an OpenAI-compatible API, but any
embedding function can be plugged in. All backends are optional (lazy imports);
when no embedding backend is configured the pipeline falls back to a
deterministic token-overlap index.
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional, Protocol, Sequence

from .config import DeepEyeSQLConfig

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    """Protocol for an embedding function."""

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        """Embed a batch of texts, returning one vector per text."""
        ...

    def embed_query(self, text: str) -> List[float]:
        """Embed a single query text."""
        ...


def _l2_normalize(vec: List[float]) -> List[float]:
    import math

    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class SentenceTransformerEmbedder:
    """Embedder backed by ``sentence-transformers``."""

    def __init__(self, model_name: str):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:  # pragma: no cover - optional dependency
            raise ImportError(
                "sentence-transformers is required for the embedding index; "
                "install it or configure an OpenAI-compatible embedding API."
            ) from e
        self._model = SentenceTransformer(model_name)
        self.model_name = model_name

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        vectors = self._model.encode(list(texts), normalize_embeddings=True)
        return [list(map(float, v)) for v in vectors]

    def embed_query(self, text: str) -> List[float]:
        return self.embed([text])[0]


class OpenAICompatEmbedder:
    """Embedder backed by any OpenAI-compatible ``/embeddings`` endpoint."""

    def __init__(
        self,
        model: str,
        api_base: str,
        api_key: Optional[str] = None,
        timeout: float = 60.0,
    ):
        self._model = model
        self.model_name = model
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    def _request(self, texts: Sequence[str]) -> List[List[float]]:
        import httpx

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = {"model": self._model, "input": list(texts)}
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                f"{self._api_base}/embeddings", json=payload, headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
        items = sorted(data["data"], key=lambda d: d.get("index", 0))
        return [_l2_normalize([float(x) for x in item["embedding"]]) for item in items]

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        return self._request(texts)

    def embed_query(self, text: str) -> List[float]:
        return self.embed([text])[0]


def resolve_embedder(config: DeepEyeSQLConfig) -> Optional[Embedder]:
    """Build an embedder from config, or None if none is configured."""
    if not config.embedding_model:
        return None
    if config.embedding_api_base:
        return OpenAICompatEmbedder(
            config.embedding_model,
            config.embedding_api_base,
            config.embedding_api_key,
        )
    try:
        return SentenceTransformerEmbedder(config.embedding_model)
    except Exception as e:  # pragma: no cover - optional dependency
        logger.warning("Failed to init sentence-transformer embedder: %s", e)
        return None


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Return the cosine similarity of two vectors."""
    import math

    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def cosine_distance(a: List[float], b: List[float]) -> float:
    """Return ``1 - cosine_similarity``."""
    return 1.0 - cosine_similarity(a, b)


# Type alias used by value/few-shot modules.
EmbeddingFunc = Callable[[Sequence[str]], List[List[float]]]
