"""AWEL workflow wiring for the DeepEye-SQL pipeline.

Exposes the pipeline over an HTTP endpoint. Because AWEL operators are
serialized, the connector and LLM providers are module-level callables that
must be registered with :func:`set_providers` before the DAG is served.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from dbgpt._private.pydantic import BaseModel, Field
from dbgpt.core.awel import DAG, HttpTrigger, MapOperator

from .pipeline import DeepEyeSQLPipeline

logger = logging.getLogger(__name__)

# Module-level provider registry (AWEL operators must be serializable, so we
# cannot close over arbitrary objects in the map function).
_connector_provider: Optional[Callable[[Optional[str]], Any]] = None
_llm_provider: Optional[Callable[[], Any]] = None


class DeepEyeSQLRequestBody(BaseModel):
    """HTTP request body for the DeepEye-SQL endpoint."""

    query: str = Field(..., description="Natural-language question")
    db_name: Optional[str] = Field(None, description="Database name / resource name")
    hint: Optional[str] = Field("", description="Optional hint / evidence")


class DeepEyeSQLResponseBody(BaseModel):
    """HTTP response body for the DeepEye-SQL endpoint."""

    success: bool
    sql: Optional[str] = None
    columns: List[str] = Field(default_factory=list)
    rows: List[List[Any]] = Field(default_factory=list)
    confidence: float = 0.0
    selected_by: str = "none"
    error: Optional[str] = None


def set_providers(
    connector_provider: Callable[[Optional[str]], Any],
    llm_provider: Callable[[], Any],
) -> None:
    """Register the connector and LLM providers for the AWEL workflow.

    Args:
        connector_provider: ``(db_name) -> connector``. For DB-GPT's
            ``ConnectorManager`` use ``lambda name: manager.get_connector(name)``.
        llm_provider: ``() -> complete``. The returned object may be an
            ``LLMComplete`` (anything exposing ``async complete(...)``) or a
            DB-GPT ``AIWrapper`` together with a model name, e.g. a
            ``(wrapper, model_name)`` tuple.
    """
    global _connector_provider, _llm_provider
    _connector_provider = connector_provider
    _llm_provider = llm_provider


async def _run_deepeye(body: DeepEyeSQLRequestBody) -> dict:
    if _connector_provider is None or _llm_provider is None:
        raise RuntimeError(
            "DeepEye-SQL providers are not set; call set_providers() first."
        )
    connector = _connector_provider(body.db_name)
    llm = _llm_provider()
    model_name = None
    if isinstance(llm, (tuple, list)):
        llm, model_name = llm[0], llm[1]
    pipeline = DeepEyeSQLPipeline(connector, llm, model_name=model_name)
    result = await pipeline.run(body.query, hint=body.hint or "")
    return result.to_dict()


def build_deepeye_sql_dag(
    connector_provider: Callable[[Optional[str]], Any],
    llm_provider: Callable[[], Any],
    *,
    endpoint: str = "/api/v1/deepeye_sql",
) -> DAG:
    """Build the AWEL DAG exposing the DeepEye-SQL pipeline over HTTP."""
    set_providers(connector_provider, llm_provider)
    with DAG("deepeye_sql_pipeline_dag") as dag:
        trigger = HttpTrigger(
            endpoint=endpoint,
            methods=["POST"],
            request_body=DeepEyeSQLRequestBody,
            response_model=DeepEyeSQLResponseBody,
        )
        operator = MapOperator(map_function=_run_deepeye)
        trigger >> operator
    return dag
