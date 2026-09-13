"""Cross-conversation caches for schema profiles and value indices.

Building a schema profile (reflecting tables + computing per-column value
statistics/examples) and a value index (distinct values + embeddings) is
expensive, so results are cached process-wide keyed by a connector fingerprint.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from .schema_profile import SchemaProfile
from .value_retrieval import ValueIndex, build_value_index

logger = logging.getLogger(__name__)


def connector_fingerprint(connector: Any) -> str:
    """Return a stable fingerprint for a connector."""
    try:
        return str(connector.db_url)
    except Exception:
        pass
    try:
        name = connector.get_current_db_name()
        if name:
            return f"{getattr(connector, 'dialect', '')}:{name}"
    except Exception:
        pass
    return f"id:{id(connector)}"


class SchemaProfileCache:
    def __init__(self) -> None:
        self._data: Dict[Tuple[str, bool], SchemaProfile] = {}

    def get(self, connector: Any, include_value_stats: bool) -> Optional[SchemaProfile]:
        return self._data.get((connector_fingerprint(connector), include_value_stats))

    def set(
        self, connector: Any, include_value_stats: bool, profile: SchemaProfile
    ) -> None:
        self._data[(connector_fingerprint(connector), include_value_stats)] = profile

    def clear(self) -> None:
        self._data.clear()


class ValueIndexCache:
    def __init__(self) -> None:
        self._data: Dict[Tuple[str, str, str], ValueIndex] = {}

    def key(
        self, connector: Any, embedding_model: Optional[str], index_dir: Optional[str]
    ) -> Tuple[str, str, str]:
        return (
            connector_fingerprint(connector),
            embedding_model or "token",
            index_dir or "",
        )

    def get(
        self, connector: Any, embedding_model: Optional[str], index_dir: Optional[str]
    ) -> Optional[ValueIndex]:
        return self._data.get(self.key(connector, embedding_model, index_dir))

    def set(
        self,
        connector: Any,
        embedding_model: Optional[str],
        index_dir: Optional[str],
        index: ValueIndex,
    ) -> None:
        self._data[self.key(connector, embedding_model, index_dir)] = index

    def clear(self) -> None:
        self._data.clear()


_schema_cache = SchemaProfileCache()
_value_index_cache = ValueIndexCache()


def get_schema_profile_cache() -> SchemaProfileCache:
    return _schema_cache


def get_value_index_cache() -> ValueIndexCache:
    return _value_index_cache


def clear_caches() -> None:
    """Clear all cross-conversation caches."""
    _schema_cache.clear()
    _value_index_cache.clear()


def get_or_build_schema_profile(
    connector: Any, include_value_stats: bool = True
) -> SchemaProfile:
    """Return a cached (or newly built) schema profile for a connector."""
    cached = _schema_cache.get(connector, include_value_stats)
    if cached is not None:
        return cached
    profile = SchemaProfile.from_connector(
        connector, include_value_stats=include_value_stats, include_value_examples=True
    )
    _schema_cache.set(connector, include_value_stats, profile)
    return profile


def get_or_build_value_index(
    connector: Any,
    embedder: Any,
    *,
    max_value_length: int,
    max_indexed_values: int,
    index_dir: Optional[str],
    db_id: Optional[str],
) -> ValueIndex:
    """Return a cached (or newly built) value index for a connector."""
    if embedder is None:
        key_model = "token"
    else:
        key_model = getattr(embedder, "model_name", None) or type(embedder).__name__
    cached = _value_index_cache.get(connector, str(key_model), index_dir)
    if cached is not None:
        return cached
    index = build_value_index(
        connector,
        embedder,
        max_value_length=max_value_length,
        max_indexed_values=max_indexed_values,
        index_dir=index_dir,
        db_id=db_id,
    )
    _value_index_cache.set(connector, str(key_model), index_dir, index)
    return index
