"""DeepEyeSQLAgent dialogue example.

Run this example from the repository root after setting your LLM endpoint env
vars, e.g.:

    export SILICONFLOW_MODEL_VERSION=Qwen/Qwen2.5-Coder-32B-Instruct
    export SILICONFLOW_API_KEY=sk-xxx
    python examples/agents/deepeye_sql_agent_example.py
"""

import asyncio
import os

from dbgpt.agent import AgentContext, AgentMemory, LLMConfig, UserProxyAgent
from dbgpt.agent.expand import DeepEyeSQLAgent
from dbgpt.agent.resource import SQLiteDBResource
from dbgpt.configs.model_config import ROOT_PATH
from dbgpt.util.tracer import initialize_tracer

test_plugin_dir = os.path.join(ROOT_PATH, "test_files")

initialize_tracer("/tmp/deepeye_agent_trace.jsonl", create_system_app=True)


async def main():
    from dbgpt.model.proxy.llms.siliconflow import SiliconFlowLLMClient

    llm_client = SiliconFlowLLMClient(
        model_alias=os.getenv(
            "SILICONFLOW_MODEL_VERSION", "Qwen/Qwen2.5-Coder-32B-Instruct"
        ),
    )
    context: AgentContext = AgentContext(conv_id="deepeye_test")

    agent_memory = AgentMemory()
    agent_memory.gpts_memory.init(conv_id="deepeye_test")

    sqlite_resource = SQLiteDBResource("SQLite Database", f"{test_plugin_dir}/dbgpt.db")

    user_proxy = await UserProxyAgent().bind(agent_memory).bind(context).build()

    deepeye_sql_boy = (
        await DeepEyeSQLAgent()
        .bind(context)
        .bind(LLMConfig(llm_client=llm_client))
        .bind(sqlite_resource)
        .bind(agent_memory)
        .build()
    )

    await user_proxy.initiate_chat(
        recipient=deepeye_sql_boy,
        reviewer=user_proxy,
        message="How many tables are in the database, and what is the schema "
        "of each table?",
    )

    # dbgpt-vis message infos
    print(await agent_memory.gpts_memory.app_link_chat_message("deepeye_test"))


if __name__ == "__main__":
    asyncio.run(main())
