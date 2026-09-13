"""DeepEye-SQL integration for DB-GPT.

This package implements the DeepEye-SQL pipeline
(`arXiv:2510.17586 <https://arxiv.org/abs/2510.17586>`_, SIGMOD 2026), a
software-engineering-inspired Text-to-SQL framework that reframes Text-to-SQL
as a verifiable SDLC workflow. The pipeline is composed of four stages:

1. **Semantic Value Retrieval** — grounds the user question in the database's
   actual values (keyword extraction + per-column semantic search).
2. **Robust Schema Linking** — combines direct, reversed and value-based
   linking and enforces *relational closure* (PK/FK force-inclusion) over the
   foreign-key graph.
3. **N-Version SQL Generation** — three independent generators (skeleton,
   in-context-learning and divide-and-conquer) run in parallel.
4. **SQL Unit Testing & Confidence-Aware Selection** — a deterministic
   tool-chain of eight checkers drives targeted LLM repair, then
   execution-result clustering plus unbalanced pairwise adjudication selects
   the final, quality-gated SQL.

Integration points:

* :class:`DeepEyeSQLAgent` — a :class:`~dbgpt.agent.ConversableAgent` that can
  be bound to a :class:`~dbgpt.agent.resource.database.DBResource`.
* :func:`build_deepeye_sql_dag` — an AWEL workflow exposing the pipeline over
  HTTP.
"""

from .agent import DeepEyeSQLAgent  # noqa: F401
from .caching import clear_caches  # noqa: F401
from .config import DeepEyeSQLConfig  # noqa: F401
from .few_shot import load_few_shot_examples  # noqa: F401
from .multischema import (  # noqa: F401
    DEFAULT_EXCLUDED_SCHEMAS,
    SchemaAwareConnector,
    wrap_multi_schema,
)
from .pipeline import DeepEyeSQLPipeline  # noqa: F401
from .pusula import PUSULA_SCHEMAS, build_pusula_connector, pusula_db_url  # noqa: F401
from .schema_profile import SchemaProfile  # noqa: F401
from .schemas import (  # noqa: F401
    CandidateSQL,
    CheckerReport,
    DeepEyeSQLResult,
    LinkedSchema,
    RetrievedValue,
    RetrievedValues,
)
from .value_retrieval import build_value_index  # noqa: F401
from .workflow import (  # noqa: F401
    DeepEyeSQLRequestBody,
    DeepEyeSQLResponseBody,
    build_deepeye_sql_dag,
    set_providers,
)

__all__ = [
    "DeepEyeSQLAgent",
    "DeepEyeSQLPipeline",
    "DeepEyeSQLConfig",
    "SchemaAwareConnector",
    "wrap_multi_schema",
    "DEFAULT_EXCLUDED_SCHEMAS",
    "PUSULA_SCHEMAS",
    "build_pusula_connector",
    "pusula_db_url",
    "SchemaProfile",
    "CandidateSQL",
    "CheckerReport",
    "DeepEyeSQLResult",
    "LinkedSchema",
    "RetrievedValue",
    "RetrievedValues",
    "build_deepeye_sql_dag",
    "set_providers",
    "clear_caches",
    "load_few_shot_examples",
    "build_value_index",
    "DeepEyeSQLRequestBody",
    "DeepEyeSQLResponseBody",
]
