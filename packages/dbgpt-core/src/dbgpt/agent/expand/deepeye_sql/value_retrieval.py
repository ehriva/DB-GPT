"""Semantic value retrieval (Stage 1a of DeepEye-SQL).

Offline: index only TEXT/VARCHAR/CHAR columns, skipping columns that are
all-UUID or all-numeric. Online: extract keywords with the LLM, embed them,
then retrieve the top-K (``max_values_per_column``) most similar values per
column, ranked by cosine distance (``1 - cosine_similarity``).

Two index backends are provided:

* :class:`EmbeddingValueIndex` — real embeddings (sentence-transformers or an
  OpenAI-compatible API), optionally persisted to disk as a per-database
  "local index".
* :class:`InMemoryValueIndex` — a deterministic token-overlap fallback used
  when no embedding backend is configured.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .embeddings import Embedder, cosine_distance
from .llm import LLMComplete
from .prompts import render_keyword_extraction
from .schema_profile import is_text_column
from .schemas import RetrievedValue, RetrievedValues
from .util import extract_json, extract_xml_result, quote_ident

logger = logging.getLogger(__name__)

_NUMERIC_RE = re.compile(r"^\s*[-+]?\d+(\.\d+)?\s*$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_DEFAULT_MAX_VALUES_PER_COLUMN = 5
_DEFAULT_MAX_VALUE_LENGTH = 100
_DEFAULT_MAX_INDEXED_VALUES = 2000


class ValueIndex:
    """A per-column value index supporting distance-ordered search."""

    def add(self, column_key: str, values: List[str]) -> None:
        raise NotImplementedError

    def search(self, column_key: str, keyword: str, top_k: int) -> List[Tuple[str, float]]:
        raise NotImplementedError

    def columns(self) -> List[str]:
        raise NotImplementedError


class InMemoryValueIndex(ValueIndex):
    """Deterministic token-overlap index (fallback; no embeddings required)."""

    def __init__(self) -> None:
        self._store: Dict[str, List[str]] = {}

    def add(self, column_key: str, values: List[str]) -> None:
        self._store[column_key] = values

    def columns(self) -> List[str]:
        return list(self._store.keys())

    def search(self, column_key: str, keyword: str, top_k: int) -> List[Tuple[str, float]]:
        values = self._store.get(column_key, [])
        kw = _tokenize(keyword)
        if not kw:
            return []
        scored: List[Tuple[float, str]] = []
        for value in values:
            vt = _tokenize(value)
            if not vt:
                continue
            overlap = len(kw & vt)
            jaccard = overlap / max(1.0, len(kw | vt))
            lower_kw = keyword.lower()
            lower_val = value.lower()
            sub_bonus = 1.0 if (lower_kw in lower_val or lower_val in lower_kw) else 0.0
            similarity = 0.5 * jaccard + 0.5 * sub_bonus
            if similarity > 0:
                scored.append((1.0 - similarity, value))
        scored.sort(key=lambda x: (x[0], x[1]))
        return [(v, d) for d, v in scored[:top_k]]


class EmbeddingValueIndex(ValueIndex):
    """Cosine-distance value index backed by an embedding function."""

    def __init__(self, embedder: Embedder):
        self._embedder = embedder
        self._columns: Dict[str, List[Tuple[str, List[float]]]] = {}
        self._query_cache: Dict[str, List[float]] = {}

    def add(self, column_key: str, values: List[str]) -> None:
        if not values:
            return
        vectors = self._embedder.embed(values)
        self.add_embeddings(column_key, list(zip(values, vectors)))

    def add_embeddings(
        self, column_key: str, pairs: Sequence[Tuple[str, List[float]]]
    ) -> None:
        self._columns.setdefault(column_key, []).extend(pairs)

    def columns(self) -> List[str]:
        return list(self._columns.keys())

    def _embed_query(self, keyword: str) -> List[float]:
        if keyword not in self._query_cache:
            self._query_cache[keyword] = self._embedder.embed_query(keyword)
        return self._query_cache[keyword]

    def search(self, column_key: str, keyword: str, top_k: int) -> List[Tuple[str, float]]:
        pairs = self._columns.get(column_key, [])
        if not pairs:
            return []
        qv = self._embed_query(keyword)
        scored: List[Tuple[float, str]] = []
        for value, vec in pairs:
            scored.append((cosine_distance(qv, vec), value))
        scored.sort(key=lambda x: (x[0], x[1]))
        return [(v, d) for d, v in scored[:top_k]]

    # ------------------------------------------------------------------
    # Local persistent index (JSON) — the reference's "local_index" mode.
    # ------------------------------------------------------------------
    def save(self, directory: str) -> None:
        os.makedirs(directory, exist_ok=True)
        manifest = {"metric": "cosine", "columns": list(self._columns.keys())}
        for column_key, pairs in self._columns.items():
            with open(_column_file(directory, column_key), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "values": [v for v, _ in pairs],
                        "embeddings": [e for _, e in pairs],
                    },
                    f,
                )
        with open(os.path.join(directory, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f)

    @classmethod
    def load(cls, directory: str, embedder: Embedder) -> "EmbeddingValueIndex":
        index = cls(embedder)
        with open(os.path.join(directory, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        for column_key in manifest.get("columns", []):
            path = _column_file(directory, column_key)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            pairs = list(
                zip(data.get("values", []), data.get("embeddings", []))
            )
            index.add_embeddings(column_key, pairs)
        return index


def _column_file(directory: str, column_key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", column_key)
    return os.path.join(directory, f"{safe}.json")


def _tokenize(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _is_skip_column(name: str, col_type: str, sample_values: List[str]) -> bool:
    t = (col_type or "").lower()
    if "uuid" in t or "guid" in t:
        return True
    if sample_values:
        numeric = sum(1 for v in sample_values if v and _NUMERIC_RE.match(v))
        uuidish = sum(1 for v in sample_values if v and _UUID_RE.match(v))
        total = len(sample_values)
        if total and (numeric + uuidish) / total >= 0.9:
            return True
    return False


def _distinct_values(
    connector: Any, table: str, column: str, limit: int, max_value_length: int
) -> List[str]:
    dialect = (getattr(connector, "dialect", "") or "").lower()
    t = quote_ident(table, dialect)
    c = quote_ident(column, dialect)
    if dialect in ("mssql", "sqlserver"):
        sql = (
            f"SELECT DISTINCT TOP ({limit}) {c} FROM {t} "
            f"WHERE {c} IS NOT NULL AND {c} <> ''"
        )
    else:
        sql = (
            f"SELECT DISTINCT {c} FROM {t} "
            f"WHERE {c} IS NOT NULL AND {c} <> '' LIMIT {limit}"
        )
    try:
        result = connector.run(sql)
        if not result:
            return []
        out = []
        for r in result[1:]:
            if r and r[0] is not None:
                s = str(r[0])
                if len(s) <= max_value_length:
                    out.append(s)
        return out
    except Exception as e:  # pragma: no cover
        logger.debug("distinct value extraction failed for %s.%s: %s", table, column, e)
        return []


def _iter_indexable_columns(connector: Any, max_value_length: int, max_indexed_values: int):
    """Yield ``(table, column, col_type, sample_values)`` for indexable columns."""
    for table in connector.get_table_names():
        try:
            columns = connector.get_columns(table)
        except Exception as e:  # pragma: no cover
            logger.debug("get_columns(%s) failed: %s", table, e)
            continue
        for col in columns:
            name = col.get("name", "")
            col_type = str(col.get("type", "") or "")
            if not name or not is_text_column(col_type):
                continue
            sample = _distinct_values(
                connector, table, name, min(50, max_indexed_values), max_value_length
            )
            if _is_skip_column(name, col_type, sample):
                continue
            yield table, name, col_type, sample


def build_value_index(
    connector: Any,
    embedder: Optional[Embedder] = None,
    *,
    max_value_length: int = _DEFAULT_MAX_VALUE_LENGTH,
    max_indexed_values: int = _DEFAULT_MAX_INDEXED_VALUES,
    index_dir: Optional[str] = None,
    db_id: Optional[str] = None,
) -> ValueIndex:
    """Build (or load) a value index, optionally persisted under ``index_dir``.

    Returns an :class:`EmbeddingValueIndex` when ``embedder`` is provided,
    otherwise an :class:`InMemoryValueIndex`.
    """
    if index_dir and db_id and embedder is not None:
        directory = os.path.join(index_dir, _safe_name(db_id))
        manifest = os.path.join(directory, "manifest.json")
        if os.path.exists(manifest):
            try:
                logger.info("Loading value index from %s", directory)
                return EmbeddingValueIndex.load(directory, embedder)
            except Exception as e:  # pragma: no cover
                logger.warning("Failed to load value index: %s", e)

    index: ValueIndex = EmbeddingValueIndex(embedder) if embedder else InMemoryValueIndex()
    for table, column, _col_type, _sample in _iter_indexable_columns(
        connector, max_value_length, max_indexed_values
    ):
        values = _distinct_values(connector, table, column, max_indexed_values, max_value_length)
        if values:
            index.add(f"{table}.{column}", values)
    if index_dir and db_id and embedder is not None:
        try:
            index.save(os.path.join(index_dir, _safe_name(db_id)))
            logger.info("Saved value index to %s", index_dir)
        except Exception as e:  # pragma: no cover
            logger.warning("Failed to persist value index: %s", e)
    return index


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name) or "default"


class SemanticValueRetriever:
    """Semantic value retrieval over a database connector."""

    def __init__(
        self,
        connector: Any,
        complete: LLMComplete,
        *,
        top_k: int = _DEFAULT_MAX_VALUES_PER_COLUMN,
        max_value_length: int = _DEFAULT_MAX_VALUE_LENGTH,
        max_indexed_values: int = _DEFAULT_MAX_INDEXED_VALUES,
        index: Optional[ValueIndex] = None,
        embedder: Optional[Embedder] = None,
    ):
        self._connector = connector
        self._complete = complete
        self._top_k = top_k
        self._max_value_length = max_value_length
        self._max_indexed_values = max_indexed_values
        self._index = index
        self._embedder = embedder

    @property
    def index(self) -> ValueIndex:
        if self._index is None:
            self._index = build_value_index(
                self._connector,
                self._embedder,
                max_value_length=self._max_value_length,
                max_indexed_values=self._max_indexed_values,
            )
        return self._index

    async def _extract_keywords(self, question: str, hint: str) -> List[str]:
        prompt = render_keyword_extraction(question, hint)
        try:
            raw = await self._complete.complete(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
                max_new_tokens=512,
            )
            data = extract_json(extract_xml_result(raw))
            if isinstance(data, list):
                return _post_process_keywords(data)
        except Exception as e:  # pragma: no cover - depends on LLM
            logger.warning("Keyword extraction failed (%s); using fallback", e)
        return list(dict.fromkeys((question + " " + hint).split()))

    async def retrieve(
        self, question: str, hint: str = "", top_k: Optional[int] = None
    ) -> RetrievedValues:
        """Online: retrieve relevant values for each indexed column."""
        index = self.index
        k = top_k or self._top_k
        keywords = await self._extract_keywords(question, hint)
        if not keywords:
            return RetrievedValues(values={})

        retrieved: Dict[str, List[RetrievedValue]] = {}

        async def _search_column(column_key: str) -> Tuple[str, List[RetrievedValue]]:
            best: Dict[str, float] = {}
            for kw in keywords:
                for value, distance in index.search(column_key, kw, k):
                    best[value] = min(best.get(value, 1.0), distance)
            ordered = sorted(best.items(), key=lambda x: (x[1], x[0]))
            return column_key, [
                RetrievedValue(value=v, distance=d) for v, d in ordered[:k]
            ]

        results = await asyncio.gather(
            *[_search_column(c) for c in index.columns()]
        )
        for column_key, values in results:
            if values:
                retrieved[column_key] = values
        return RetrievedValues(values=retrieved)


def _post_process_keywords(keywords: Sequence[str]) -> List[str]:
    """Dedupe keywords and add every whitespace-split token of each keyword."""
    seen: List[str] = []
    seen_set = set()
    for kw in keywords:
        for token in str(kw).split():
            if token and token not in seen_set:
                seen_set.add(token)
                seen.append(token)
    return seen
