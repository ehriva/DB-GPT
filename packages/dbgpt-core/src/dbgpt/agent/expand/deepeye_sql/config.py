"""Configuration for the DeepEye-SQL pipeline.

All knobs are gathered in :class:`DeepEyeSQLConfig` and can be loaded from a
TOML file, environment variables, or a plain dictionary. Explicit keyword
arguments passed to :class:`~dbgpt.agent.expand.deepeye_sql.pipeline
.DeepEyeSQLPipeline` / :class:`~dbgpt.agent.expand.deepeye_sql.agent
.DeepEyeSQLAgent` still override the config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore

_ENV_PREFIX = "DEEPEYE_SQL_"


@dataclass
class DeepEyeSQLConfig:
    """Configuration dataclass for the DeepEye-SQL pipeline."""

    # --- LLM ------------------------------------------------------------
    temperature: float = 0.7

    # --- Semantic value retrieval --------------------------------------
    top_k: int = 5
    max_value_length: int = 100
    max_indexed_values: int = 2000
    # Embedding backend (optional). ``embedding_model`` may be a
    # sentence-transformers model name or an OpenAI-compatible model name when
    # ``embedding_api_base`` is also set.
    embedding_model: Optional[str] = None
    embedding_api_base: Optional[str] = None
    embedding_api_key: Optional[str] = None
    # Directory for the persistent per-database value index (Chroma or local).
    index_dir: Optional[str] = None
    # When True, prefer the local (numpy/JSON) persistent index; when False,
    # prefer Chroma. Chroma is used only if available.
    use_local_index: bool = True

    # --- Robust schema linking -----------------------------------------
    value_distance_threshold: float = 0.05
    direct_linking_sampling_budget: int = 1
    reversed_linking_sampling_budget: int = 1

    # --- N-version generation ------------------------------------------
    generation_sampling_budget: int = 1
    few_shot_examples_path: Optional[str] = None
    num_examples: int = 5
    question_weight: float = 0.6
    sql_weight: float = 0.4

    # --- Tool-chain -----------------------------------------------------
    checker_sampling_budget: int = 1

    # --- Confidence-aware selection ------------------------------------
    confidence_threshold: float = 0.6
    filter_top_k: int = 2
    evaluator_sampling_budget: int = 3

    # --- Execution ------------------------------------------------------
    max_rows: int = 100
    execution_timeout: Optional[float] = None
    read_only: bool = True

    # --- Schema ---------------------------------------------------------
    include_value_stats: bool = True
    # Token budget for the rendered schema (progressive stripping). None = no
    # limit.
    max_schema_tokens: Optional[int] = None

    # --- Observability --------------------------------------------------
    enable_tracing: bool = True

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeepEyeSQLConfig":
        """Create a config from a dict, ignoring unknown keys."""
        fields = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in fields})

    @classmethod
    def from_toml(
        cls, path: str, section: str = "deepeye_sql"
    ) -> "DeepEyeSQLConfig":
        """Load config from a TOML file section (default ``[deepeye_sql]``)."""
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls.from_dict(data.get(section, {}))

    @classmethod
    def from_env(cls, prefix: str = _ENV_PREFIX) -> "DeepEyeSQLConfig":
        """Load config from environment variables.

        Environment variable names are the upper-cased field names prefixed by
        ``DEEPEYE_SQL_`` (e.g. ``DEEPEYE_SQL_TEMPERATURE``, ``DEEPEYE_SQL_INDEX_DIR``).
        Booleans accept ``1/true/yes``, ints/floats are coerced, ``null``/``none``
        map to ``None``.
        """
        data: Dict[str, Any] = {}
        fields = {f.name for f in cls.__dataclass_fields__.values()}
        for name in fields:
            env_name = prefix + name.upper()
            if env_name not in os.environ:
                continue
            data[name] = _coerce(os.environ[env_name])
        return cls.from_dict(data)


def _coerce(value: str) -> Any:
    v = value.strip()
    low = v.lower()
    if low in ("null", "none", ""):
        return None
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off"):
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return value
