"""Run the DeepEye-SQL pipeline against the Pusula (MedipolDB) database.

Pusula tables live in several named PostgreSQL schemas (``Hasta``, ``Tedavi``,
``Ortak``, ``LIS``, ``RIS``, ``Stok``, ``IK``, ``MedipolDB``), so the pipeline
uses a schema-aware connector that reflects all of them with schema-qualified
table names.

Environment:

* ``PUSULA_DB_URL`` — SQLAlchemy URL (defaults to the dev replica
  ``postgresql+psycopg://postgres:postgres@localhost:5433/medipol_dev``).
* ``SILICONFLOW_API_KEY`` / ``SILICONFLOW_MODEL_VERSION`` — LLM endpoint.
* ``DEEPEYE_SQL_CONFIG`` — optional TOML config path.

Usage::

    python examples/agents/deepeye_sql_pusula_example.py \
        "How many patients are in the Hasta schema?"
"""

import asyncio
import os
import sys

from dbgpt.agent.expand.deepeye_sql import (
    DeepEyeSQLAgent,
    DeepEyeSQLConfig,
    DeepEyeSQLPipeline,
    PUSULA_SCHEMAS,
    build_pusula_connector,
)
from dbgpt.agent.resource import RDBMSConnectorResource
from dbgpt.agent import AgentContext, AgentMemory, LLMConfig, UserProxyAgent
from dbgpt.util.tracer import initialize_tracer

DEFAULT_URL = "postgresql+psycopg://postgres:postgres@localhost:5433/medipol_dev"


def _llm():
    """Return ``(raw_llm_client, model_name)``."""
    model_name = os.getenv(
        "SILICONFLOW_MODEL_VERSION", "Qwen/Qwen2.5-Coder-32B-Instruct"
    )
    if os.getenv("SILICONFLOW_API_KEY"):
        from dbgpt.model.proxy.llms.siliconflow import SiliconFlowLLMClient

        client = SiliconFlowLLMClient(model_alias=model_name)
    else:
        from dbgpt.model.proxy import OpenAILLMClient

        client = OpenAILLMClient(model_alias=model_name)
    return client, model_name


def _config() -> DeepEyeSQLConfig:
    path = os.getenv("DEEPEYE_SQL_CONFIG")
    if path and os.path.exists(path):
        return DeepEyeSQLConfig.from_toml(path)
    return DeepEyeSQLConfig.from_env()


async def run_pipeline(question: str) -> None:
    from dbgpt.agent.util.llm.llm_client import AIWrapper

    connector = build_pusula_connector(os.getenv("PUSULA_DB_URL") or DEFAULT_URL)
    raw_client, model_name = _llm()
    pipeline = DeepEyeSQLPipeline(
        connector, AIWrapper(llm_client=raw_client),
        config=_config(), model_name=model_name,
    )
    result = await pipeline.run(question)
    print(f"\nSQL: {result.sql}")
    print(f"Rows ({len(result.rows)}), confidence={result.confidence:.2f} "
          f"({result.selected_by})")
    for row in result.rows[:10]:
        print("  ", row)


async def run_agent(question: str) -> None:
    from dbgpt.datasource.rdbms.base import RDBMSConnector

    initialize_tracer("/tmp/deepeye_pusula_trace.jsonl", create_system_app=True)
    url = os.getenv("PUSULA_DB_URL") or DEFAULT_URL
    raw = RDBMSConnector.from_uri(url)
    resource = RDBMSConnectorResource("Pusula", raw)

    raw_client, _model_name = _llm()
    context = AgentContext(conv_id="pusula_test")
    memory = AgentMemory()
    memory.gpts_memory.init(conv_id="pusula_test")

    user_proxy = await UserProxyAgent().bind(memory).bind(context).build()
    agent = (
        await DeepEyeSQLAgent(multi_schema=True, schemas=PUSULA_SCHEMAS)
        .bind(context)
        .bind(LLMConfig(llm_client=raw_client))
        .bind(resource)
        .bind(memory)
        .build()
    )
    await user_proxy.initiate_chat(recipient=agent, reviewer=user_proxy, message=question)
    print(await memory.gpts_memory.app_link_chat_message("pusula_test"))


if __name__ == "__main__":
    question = sys.argv[1] if len(sys.argv) > 1 else (
        "How many patients (Hasta) are there in total?"
    )
    if os.getenv("DEEPEYE_SQL_USE_AGENT", "").lower() in ("1", "true", "yes"):
        asyncio.run(run_agent(question))
    else:
        asyncio.run(run_pipeline(question))
