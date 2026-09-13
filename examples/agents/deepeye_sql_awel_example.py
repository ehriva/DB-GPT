"""AWEL workflow example exposing the DeepEye-SQL pipeline over HTTP.

Run from the repository root:

    export SILICONFLOW_MODEL_VERSION=Qwen/Qwen2.5-Coder-32B-Instruct
    export SILICONFLOW_API_KEY=sk-xxx
    python examples/agents/deepeye_sql_awel_example.py

Then POST to ``http://127.0.0.1:5556/api/v1/deepeye_sql`` with a JSON body::

    {"query": "How many tables are in the database?", "db_name": null, "hint": ""}
"""

import os

from dbgpt.agent.expand.deepeye_sql import build_deepeye_sql_dag
from dbgpt.agent.util.llm.llm_client import AIWrapper
from dbgpt.configs.model_config import ROOT_PATH
from dbgpt.core.awel import setup_dev_environment
from dbgpt_ext.datasource.rdbms.conn_sqlite import SQLiteConnector

test_plugin_dir = os.path.join(ROOT_PATH, "test_files")


def connector_provider(db_name=None):
    """Return a connector for the given (or default) database name."""
    # db_name is unused for this single-SQLite example; swap in a
    # ConnectorManager lookup for multi-database deployments.
    return SQLiteConnector.from_file_path(f"{test_plugin_dir}/dbgpt.db")


def llm_provider():
    """Return ``(AIWrapper, model_name)`` for the DeepEye-SQL pipeline."""
    from dbgpt.model.proxy.llms.siliconflow import SiliconFlowLLMClient

    model_name = os.getenv(
        "SILICONFLOW_MODEL_VERSION", "Qwen/Qwen2.5-Coder-32B-Instruct"
    )
    llm_client = SiliconFlowLLMClient(model_alias=model_name)
    return AIWrapper(llm_client=llm_client), model_name


if __name__ == "__main__":
    dag = build_deepeye_sql_dag(
        connector_provider, llm_provider, endpoint="/api/v1/deepeye_sql"
    )
    setup_dev_environment([dag], host="127.0.0.1", port=5556)
