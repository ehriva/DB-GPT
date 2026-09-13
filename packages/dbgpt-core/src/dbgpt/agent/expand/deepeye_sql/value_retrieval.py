"""Semantic value retrieval (Stage 1a of DeepEye-SQL).

Offline: index only TEXT/VARCHAR/CHAR columns, skipping columns that are
all-UUID or all-numeric. Online: extract keywords with the LLM, embed them,
then retrieve the top-K (``max_values_per_column``) most similar values per
column, ranked by cosine distance (``1 - cosine_similarity``).

The vector backend is pluggable. When no embedding model / Chroma is
available, a deterministic token-overlap index is used so the pipeline still
runs, returning a cosine-distance-compatible score.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .llm import LLMComplete
from .prompts import render_keyword_extraction
from .schema_profile import is_text_column
from .schemas import RetrievedValue, RetrievedValues
from .util import extract_json, extract_xml_result

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
    """Deterministic token-overlap index (fallback; no embeddings required).

    Similarity is a blend of token-set Jaccard overlap and substring
    containment, mapped to ``distance = 1 - similarity`` so the downstream
    distance-based threshold remains meaningful.
    """

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
    if dialect in ("mssql", "sqlserver"):
        sql = (
            f"SELECT DISTINCT TOP ({limit}) {column} FROM {table} "
            f"WHERE {column} IS NOT NULL AND {column} <> ''"
        )
    else:
        sql = (
            f"SELECT DISTINCT {column} FROM {table} "
            f"WHERE {column} IS NOT NULL AND {column} <> '' LIMIT {limit}"
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
    ):
        self._connector = connector
        self._complete = complete
        self._top_k = top_k
        self._max_value_length = max_value_length
        self._max_indexed_values = max_indexed_values
        self._index = index or InMemoryValueIndex()
        self._built = False

    @property
    def index(self) -> ValueIndex:
        return self._index

    def build(self) -> None:
        """Offline: extract and index distinct values for selected TEXT columns."""
        if self._built:
            return
        tables = list(self._connector.get_table_names())
        for table in tables:
            try:
                columns = self._connector.get_columns(table)
            except Exception as e:  # pragma: no cover
                logger.debug("get_columns(%s) failed: %s", table, e)
                continue
            for col in columns:
                name = col.get("name", "")
                col_type = str(col.get("type", "") or "")
                if not name or not is_text_column(col_type):
                    continue
                sample = _distinct_values(
                    self._connector, table, name, min(50, self._max_indexed_values),
                    self._max_value_length,
                )
                if _is_skip_column(name, col_type, sample):
                    continue
                values = _distinct_values(
                    self._connector, table, name, self._max_indexed_values,
                    self._max_value_length,
                )
                if values:
                    self._index.add(f"{table}.{name}", values)
        self._built = True
        logger.info("Value index built with %d columns", len(self._index.columns()))

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
        # Fallback: whitespace-split of question + hint.
        return list(dict.fromkeys((question + " " + hint).split()))

    async def retrieve(
        self, question: str, hint: str = "", top_k: Optional[int] = None
    ) -> RetrievedValues:
        """Online: retrieve relevant values for each indexed column."""
        if not self._built:
            self.build()
        k = top_k or self._top_k
        keywords = await self._extract_keywords(question, hint)
        if not keywords:
            return RetrievedValues(values={})

        retrieved: Dict[str, List[RetrievedValue]] = {}

        async def _search_column(column_key: str) -> Tuple[str, List[RetrievedValue]]:
            # Merge candidates across keywords, sort by distance ascending,
            # dedupe, keep top-k unique values.
            best: Dict[str, float] = {}
            for kw in keywords:
                for value, distance in self._index.search(column_key, kw, k):
                    best[value] = min(best.get(value, 1.0), distance)
            ordered = sorted(best.items(), key=lambda x: (x[1], x[0]))
            return column_key, [
                RetrievedValue(value=v, distance=d) for v, d in ordered[:k]
            ]

        results = await asyncio.gather(
            *[_search_column(c) for c in self._index.columns()]
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
