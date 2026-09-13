"""Auto-discoverable AWEL DAG for the DeepEye-SQL pipeline.

DB-GPT loads every ``.py`` file in the AWEL definition directory (default
``examples/awel``) at startup and registers any module-level ``DAG`` it finds,
so placing this file there makes DeepEye-SQL available at
``POST /api/v1/deepeye_sql`` without any code changes.

Provider resolution is lazy (at request time), so importing this module is
safe even before the model/database components are ready.

Environment variables:

* ``DEEPEYE_SQL_MODEL`` — model alias for the LLM (default
  ``Qwen/Qwen2.5-Coder-32B-Instruct``).
* ``DEEPEYE_SQL_CONFIG`` — optional path to a TOML config file.
"""

import os
from typing import Any, Optional

from dbgpt.agent.expand.deepeye_sql import (
    DeepEyeSQLConfig,
    build_deepeye_sql_dag,
)
from dbgpt.agent.util.llm.llm_client import AIWrapper
from dbgpt.configs.model_config import ROOT_PATH
from dbgpt.core.awel import DAGVar
from dbgpt_ext.datasource.rdbms.conn_sqlite import SQLiteConnector

test_plugin_dir = os.path.join(ROOT_PATH, "test_files")


def _connector_provider(db_name: Optional[str]) -> Any:
    """Resolve a connector by name via the app's ConnectorManager.

    Falls back to the bundled SQLite database when no name is given or the
    manager is unavailable (e.g. a standalone run).
    """
    if db_name:
        try:
            app = DAGVar.get_current_system_app()
            from dbgpt_serve.datasource.manages.connector_manager import ConnectorManager

            manager = app.get_component("ConnectorManager", ConnectorManager)
            return manager.get_connector(db_name)
        except Exception:
            pass
    return SQLiteConnector.from_file_path(f"{test_plugin_dir}/dbgpt.db")


def _llm_provider():
    """Return ``(AIWrapper, model_name)`` built lazily from env config."""
    model_name = os.getenv(
        "DEEPEYE_SQL_MODEL", os.getenv(
            "SILICONFLOW_MODEL_VERSION", "Qwen/Qwen2.5-Coder-32B-Instruct"
        )
    )
    if os.getenv("SILICONFLOW_API_KEY"):
        from dbgpt.model.proxy.llms.siliconflow import SiliconFlowLLMClient

        client = SiliconFlowLLMClient(model_alias=model_name)
    else:
        from dbgpt.model.proxy import OpenAILLMClient

        client = OpenAILLMClient(model_alias=model_name)
    return AIWrapper(llm_client=client), model_name


def _load_config() -> DeepEyeSQLConfig:
    toml_path = os.getenv("DEEPEYE_SQL_CONFIG")
    if toml_path and os.path.exists(toml_path):
        return DeepEyeSQLConfig.from_toml(toml_path)
    return DeepEyeSQLConfig.from_env()


dag = build_deepeye_sql_dag(
    _connector_provider,
    _llm_provider,
    endpoint="/api/v1/deepeye_sql",
    config=_load_config(),
)


if __name__ == "__main__":
    from dbgpt.core.awel import setup_dev_environment

    setup_dev_environment([dag], host="127.0.0.1", port=5556)
