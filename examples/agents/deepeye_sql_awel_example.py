"""AWEL workflow example exposing the DeepEye-SQL pipeline over HTTP.

Run from the repository root:

    export SILICONFLOW_MODEL_VERSION=Qwen/Qwen2.5-Coder-32B-Instruct
    export SILICONFLOW_API_KEY=sk-xxx
    python examples/agents/deepeye_sql_awel_example.py

Then POST to ``http://127.0.0.1:5556/api/v1/deepeye_sql`` with a JSON body::

    {"query": "How many tables are in the database?", "db_name": null, "hint": ""}

The ``db_name`` field routes to a database registered in DB-GPT's
``ConnectorManager``; when it is null the bundled SQLite test database is used.
"""

import os

from dbgpt.agent.expand.deepeye_sql import (
    DeepEyeSQLConfig,
    build_deepeye_sql_dag,
)
from dbgpt.agent.util.llm.llm_client import AIWrapper
from dbgpt.configs.model_config import ROOT_PATH
from dbgpt.core.awel import DAGVar, setup_dev_environment
from dbgpt_ext.datasource.rdbms.conn_sqlite import SQLiteConnector

test_plugin_dir = os.path.join(ROOT_PATH, "test_files")


def connector_provider(db_name=None):
    """Return a connector for the given database name.

    Uses DB-GPT's ``ConnectorManager`` for named databases and falls back to
    the bundled SQLite database when ``db_name`` is null or the manager is
    unavailable (e.g. running this example standalone).
    """
    if db_name:
        try:
            app = DAGVar.get_current_system_app()
            from dbgpt_serve.datasource.manages.connector_manager import ConnectorManager

            manager = app.get_component("ConnectorManager", ConnectorManager)
            return manager.get_connector(db_name)
        except Exception:
            # Fall through to the default SQLite connector.
            pass
    return SQLiteConnector.from_file_path(f"{test_plugin_dir}/dbgpt.db")


def llm_provider():
    """Return ``(AIWrapper, model_name)`` for the DeepEye-SQL pipeline."""
    from dbgpt.model.proxy.llms.siliconflow import SiliconFlowLLMClient

    model_name = os.getenv(
        "SILICONFLOW_MODEL_VERSION", "Qwen/Qwen2.5-Coder-32B-Instruct"
    )
    llm_client = SiliconFlowLLMClient(model_alias=model_name)
    return AIWrapper(llm_client=llm_client), model_name


def _load_config() -> DeepEyeSQLConfig:
    """Load config from ``DEEPEYE_SQL_CONFIG`` (TOML) or env vars."""
    toml_path = os.getenv("DEEPEYE_SQL_CONFIG")
    if toml_path and os.path.exists(toml_path):
        return DeepEyeSQLConfig.from_toml(toml_path)
    return DeepEyeSQLConfig.from_env()


if __name__ == "__main__":
    dag = build_deepeye_sql_dag(
        connector_provider,
        llm_provider,
        endpoint="/api/v1/deepeye_sql",
        config=_load_config(),
    )
    setup_dev_environment([dag], host="127.0.0.1", port=5556)
