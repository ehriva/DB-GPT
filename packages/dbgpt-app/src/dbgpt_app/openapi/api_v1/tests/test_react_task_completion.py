"""Regressions for the main task's delivery and execution contracts."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, text

from dbgpt.agent import AgentContext, AgentMessage
from dbgpt.agent.core.action.base import ActionOutput
from dbgpt.agent.expand.actions.react_action import Terminate
from dbgpt.agent.expand.react_agent import ReActAgent
from dbgpt.agent.resource.tool.pack import ToolPack
from dbgpt_app.openapi.api_v1.react_task import (
    TaskReActAgent,
    missing_deliverables,
    report_requirements,
    task_result_for_reply,
)
from dbgpt_app.openapi.api_v1.tools.code_interpreter import (
    _run_python_file,
    make_code_interpreter,
)
from dbgpt_app.openapi.api_v1.tools.html_interpreter import make_html_interpreter
from dbgpt_app.openapi.api_v1.tools.sql_query import (
    make_database_profile,
    make_sql_query,
    query_database,
)
from dbgpt_app.openapi.api_v1.tools.todowrite import make_todowrite
from dbgpt_ext.datasource.rdbms.conn_sqlite import SQLiteConnector


def test_verified_explain_policy_does_not_require_model_invented_data_backfill():
    from dbgpt_app.openapi.api_v1.react_task import missing_deliverables

    state = {
        "task_requirements": {
            "ratios": [{"metric": "毛利率", "unavailable_policy": "explain"}]
        },
        "data_warnings": {"毛利率": {"reason": "missing_input"}},
        "ratio_validation_status": {"毛利率": "missing_inputs"},
        "task_plan": [{"content": "补齐缺失工费数据", "status": "pending"}],
    }
    assert missing_deliverables(state) == []
    state["task_requirements"]["ratios"][0]["unavailable_policy"] = "require_value"
    state["task_plan"] = [{"content": "补齐缺失工费数据", "status": "pending"}]
    assert missing_deliverables(state)


def test_user_requested_backfill_is_not_completed_by_a_data_warning():
    from dbgpt_app.openapi.api_v1.react_task import missing_deliverables

    state = {
        "original_user_input": "请补齐工费数据再分析毛利率",
        "task_requirements": {
            "ratios": [{"metric": "毛利率", "unavailable_policy": "explain"}]
        },
        "data_warnings": {"毛利率": {"reason": "missing_input"}},
        "ratio_validation_status": {"毛利率": "missing_inputs"},
        "task_plan": [{"content": "补齐缺失工费数据", "status": "pending"}],
    }
    assert missing_deliverables(state)
    assert state["task_plan"][0]["status"] == "pending"


def test_cancelled_required_steps_cannot_report_success():
    state = {
        "task_plan": [
            {"content": "inspect", "status": "completed"},
            {"content": "query", "status": "cancelled"},
        ]
    }
    reply = AgentMessage(
        success=True,
        action_report=ActionOutput(
            is_exe_success=True, terminate=True, content="no data"
        ),
    )
    assert task_result_for_reply(reply, state).status == "incomplete"
    assert missing_deliverables(state)


def test_rewriting_plan_does_not_erase_cancelled_delivery():
    tasks = []
    state = {"task_plan": tasks}
    tool = make_todowrite(tasks, AsyncMock(), state)
    tool(json.dumps([{"content": "query", "status": "cancelled"}]))
    tool(json.dumps([{"content": "inspect", "status": "completed"}]))
    assert missing_deliverables(state)
    tool(json.dumps([{"content": "query", "status": "completed"}]))
    assert not missing_deliverables(state)


@pytest.mark.parametrize("key", ["output", "result", "final_answer"])
def test_terminate_accepts_real_final_answer_keys(key):
    assert Terminate().execute(**{key: "answer"}) == "answer"


def test_terminate_priority_and_empty_answer():
    assert Terminate().execute(output="first", result="second") == "first"
    assert Terminate().execute("positional") == "positional"
    with pytest.raises(ValueError):
        Terminate().execute()


@pytest.mark.asyncio
async def test_terminate_action_validates_content_on_both_routes():
    action = Terminate()
    assert (
        await action.run('Action: terminate\nAction Input: {"result":"answer"}')
    ).content == "answer"
    assert (await action.run('{"output":"answer"}')).content == "answer"
    with pytest.raises(ValueError):
        await action.run("Action: terminate\nAction Input: {}")


@pytest.mark.asyncio
async def test_final_latex_text_survives_the_real_fallback_route():
    raw = r'{"output":"25元/件\n$$\frac{1000}{40}\text{元}$$"}'
    expected = "25元/件\n" + r"$$\frac{1000}{40}\text{元}$$"
    assert Terminate().execute(raw) == expected
    assert (
        await Terminate().run("Action: terminate\nAction Input: " + raw)
    ).content == expected
    assert (await Terminate().run('{"rows":[]}')).content == '{"rows":[]}'


@pytest.mark.parametrize(
    "user_input,expected",
    [
        ("生成数据库画像和交互式网页报告", (True, True)),
        ("生成数据库画像", (False, True)),
        ("刚才的报告在哪里下载？", (False, False)),
        ("请不要生成报告，只告诉我行数", (False, False)),
        ("解释数据库画像是什么", (False, False)),
        ("Create an HTML report and database profile", (True, True)),
    ],
)
def test_report_checks_apply_to_creation_requests(user_input, expected):
    assert report_requirements(user_input) == expected


@pytest.mark.asyncio
async def test_unrelated_queries_do_not_reset_failed_report_recovery():
    agent = TaskReActAgent(task_state={"requires_report": True})
    for _ in range(5):
        output = ActionOutput(
            action="html_interpreter",
            content=json.dumps(
                {"success": False, "error": "missing report fields", "chunks": []}
            ),
        )
        message = AgentMessage(action_report=output)
        await agent.verify(message, agent)
        await agent.verify(
            AgentMessage(
                action_report=ActionOutput(content=str(_), action="sql_query")
            ),
            agent,
        )
    assert output.have_retry is False
    assert (
        message.get_dict_context()["termination_reason"] == "report_validation_failed"
    )


@pytest.fixture
def database(tmp_path, monkeypatch):
    from dbgpt.configs import model_config

    monkeypatch.setattr(model_config, "PILOT_PATH", str(tmp_path))
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE "sales data" (id INTEGER, amount REAL)'))
        connection.execute(
            text('INSERT INTO "sales data" VALUES (:id, :amount)'),
            [{"id": i, "amount": None if i == 0 else i / 2} for i in range(120)],
        )
    connector = SQLiteConnector(engine)
    yield connector
    engine.dispose()


@pytest.mark.asyncio
async def test_full_query_result_is_readable_by_real_python(database):
    state = {"conv_id": "query-test"}
    result = json.loads(
        await make_sql_query(state, database)('SELECT * FROM "sales data" ORDER BY id')
    )
    assert result["success"] and result["row_count"] == 120
    assert result["preview_truncated"]
    data = json.loads(Path(result["result_file"]).read_text(encoding="utf-8"))
    assert len(data["rows"]) == 120
    assert data["rows"][0] == [0, None]
    assert data["rows"][-1] == [119, 59.5]
    python = json.loads(
        await make_code_interpreter(state)(
            "r=json.load(open(QUERY_RESULT_PATH, encoding='utf-8'))\n"
            "df=pd.DataFrame(r['rows'], columns=r['columns'])\n"
            "print(len(df), df['amount'].sum())"
        )
    )
    assert python["success"]
    assert any("120 3570.0" in str(chunk["content"]) for chunk in python["chunks"])


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", ["DELETE FROM 'sales data'", "SELECT 1; DROP TABLE x"])
async def test_query_rejects_writes_and_multiple_statements(database, sql):
    result = json.loads(await make_sql_query({}, database)(sql))
    assert result["success"] is False
    assert len(database.get_table_names()) == 1


@pytest.mark.asyncio
async def test_profile_and_report_require_real_fields_types_and_counts(database):
    state = {
        "conv_id": "report-test",
        "requires_report": True,
        "requires_database_profile": True,
    }
    assert len(missing_deliverables(state)) == 2
    profile = json.loads(await make_database_profile(state, database)())
    assert profile["success"]
    assert state["database_profile"]["tables"][0]["row_count"] == 120
    render = make_html_interpreter(state, ".")
    incomplete = json.loads(await render(html="<html><body>hello</body></html>"))
    assert not incomplete["success"]
    assert missing_deliverables(state)
    report = json.loads(
        await render(
            html=(
                "<html><body><h1>sales data</h1><p>120 rows</p>"
                "<table><tr><td>id</td><td>INTEGER</td></tr>"
                "<tr><td>amount</td><td>REAL</td></tr></table></body></html>"
            )
        )
    )
    assert report["success"]
    assert missing_deliverables(state) == []
    assert Path(state["rendered_report"]["path"]).is_file()
    failed = json.loads(await render(file_path="does-not-exist.html"))
    assert failed["success"] is False
    assert missing_deliverables(state)


def test_successful_intermediate_tool_is_not_task_success():
    reply = AgentMessage(
        success=True, action_report=ActionOutput(content="script read")
    )
    assert task_result_for_reply(reply, {}).status == "incomplete"


@pytest.mark.asyncio
async def test_terminate_rejected_without_requested_report():
    agent = TaskReActAgent(task_state={"requires_report": True})
    output = ActionOutput(
        content='{"result":"done"}', terminate=True, action="terminate"
    )
    message = AgentMessage(action_report=output)
    passed, reason = await agent.verify(message, agent)
    assert not passed and "html_interpreter" in reason
    assert output.terminate is False
    assert output.is_exe_success is False


@pytest.mark.asyncio
async def test_pending_plan_prevents_premature_terminate():
    agent = TaskReActAgent(
        task_state={"task_plan": [{"content": "generate report", "status": "pending"}]}
    )
    message = AgentMessage(action_report=ActionOutput(content="done", terminate=True))
    passed, reason = await agent.verify(message, agent)
    assert not passed and "generate report" in reason


@pytest.mark.asyncio
async def test_batched_tool_failure_preserves_real_diagnostic():
    agent = TaskReActAgent(task_state={})
    diagnostic = json.dumps(
        {
            "success": False,
            "chunks": [
                {"output_type": "text", "content": "NameError: missing_dataframe"}
            ],
        }
    )
    raw = json.dumps([{"observation": diagnostic}])
    output = ActionOutput(content=raw, observations=raw, action="code_interpreter")
    passed, reason = await agent.verify(AgentMessage(action_report=output), agent)
    assert not passed and "NameError" in reason
    assert output.observations == raw


@pytest.mark.asyncio
async def test_real_agent_tools_recover_from_early_terminate_and_deliver(
    database, monkeypatch
):
    state = {
        "conv_id": "actual-agent",
        "requires_report": True,
        "requires_database_profile": True,
    }
    agent = TaskReActAgent(task_state=state, max_retry_count=None, max_timeout=None)
    tools = [
        make_database_profile(state, database),
        make_sql_query(state, database),
        make_code_interpreter(state),
        make_html_interpreter(state, "."),
    ]
    pack = ToolPack([item._tool for item in tools] + [Terminate()])
    agent.agent_context = AgentContext(conv_id=state["conv_id"])
    agent.resource = pack
    for action in agent.actions:
        action.init_resource(pack)
    html = "<html><body>sales data: 120 rows. id INTEGER; amount REAL.</body></html>"
    actions = iter(
        [
            ("terminate", {"result": "done"}),
            ("database_profile", {}),
            ("sql_query", {"sql": 'SELECT * FROM "sales data"'}),
            (
                "code_interpreter",
                {
                    "code": (
                        "r=json.load(open(QUERY_RESULT_PATH, encoding='utf-8')); "
                        "print(len(r['rows']))"
                    )
                },
            ),
            ("html_interpreter", {"html": html}),
            ("terminate", {"result": "Report delivered"}),
        ]
    )

    async def think(self, *args, **kwargs):
        name, args = next(actions)
        return (
            f"Thought: do next step\nAction: {name}\nAction Input: {json.dumps(args)}",
            "test",
        )

    monkeypatch.setattr(TaskReActAgent, "thinking", think)
    monkeypatch.setattr(
        TaskReActAgent, "_load_thinking_messages", AsyncMock(return_value=([], {}))
    )
    monkeypatch.setattr(TaskReActAgent, "review", AsyncMock(return_value=(True, None)))
    for name in ("send", "write_memories"):
        monkeypatch.setattr(TaskReActAgent, name, AsyncMock())
    events = []

    async def capture(kind, payload):
        if kind == "act":
            events.append(payload["action_output"])

    reply = await agent.generate_reply(
        AgentMessage(content="profile report"), agent, stream_callback=capture
    )
    assert not events[0]["is_exe_success"]
    assert len(events) == 6
    assert task_result_for_reply(reply, state).status == "succeeded"
    assert Path(state["rendered_report"]["path"]).read_text(encoding="utf-8") == html


@pytest.mark.asyncio
async def test_repetition_warns_then_stops_even_when_tools_succeed():
    agent = TaskReActAgent(task_state={})
    outputs = []
    for _ in range(5):
        output = ActionOutput(
            content="same",
            observations="same",
            action="code_interpreter",
            action_input='{"code":"print(1)"}',
        )
        message = AgentMessage(action_report=output)
        outputs.append((output, await agent.verify(message, agent)))
    assert "改变方法" in outputs[2][1][1]
    assert outputs[4][0].have_retry is False
    assert message.get_dict_context()["termination_reason"] == "no_progress"


@pytest.mark.asyncio
async def test_two_tool_cycle_is_also_no_progress():
    agent = TaskReActAgent(task_state={})
    for action in ["sql_query", "code_interpreter"] * 3:
        message = AgentMessage(
            action_report=ActionOutput(
                content="unchanged", action=action, action_input="same arguments"
            )
        )
        await agent.verify(message, agent)
    assert message.action_report.have_retry is False
    assert message.get_dict_context()["termination_reason"] == "no_progress"


async def _drive_loop(monkeypatch, agent, count):
    agent.agent_context = AgentContext(conv_id="loop-test")
    pack = ToolPack([Terminate()])
    agent.resource = pack
    for action in agent.actions:
        action.init_resource(pack)

    async def init_reply(self, *args, **kwargs):
        return AgentMessage(context={})

    async def think(self, *args, **kwargs):
        return "next", "test-model"

    rounds = []

    async def act(self, **kwargs):
        rounds.append(len(rounds) + 1)
        return ActionOutput(
            content=f"result-{len(rounds)}",
            observations=f"result-{len(rounds)}",
            action="query",
            terminate=len(rounds) == count,
        )

    monkeypatch.setattr(type(agent), "_a_init_reply_message", init_reply)
    monkeypatch.setattr(type(agent), "thinking", think)
    monkeypatch.setattr(type(agent), "act", act)
    for name in ("send", "write_memories"):
        monkeypatch.setattr(type(agent), name, AsyncMock())
    monkeypatch.setattr(type(agent), "review", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(
        type(agent), "_load_thinking_messages", AsyncMock(return_value=([], {}))
    )
    # Simulate >600 seconds without slowing the test or changing request deadlines.
    import dbgpt.agent.core.base_agent as base

    times = iter([0] + [1000] * 100)
    monkeypatch.setattr(base.time, "time", lambda: next(times, 1000))
    reply = await agent.generate_reply(AgentMessage(content="task"), agent)
    return reply, rounds


@pytest.mark.asyncio
async def test_main_loop_continues_beyond_30_and_total_timeout(monkeypatch):
    agent = TaskReActAgent(task_state={}, max_retry_count=None, max_timeout=None)
    reply, rounds = await _drive_loop(monkeypatch, agent, 35)
    assert len(rounds) == 35
    assert reply.success
    assert reply.get_dict_context()["termination_reason"] == "completed"


@pytest.mark.asyncio
async def test_bounded_agents_remain_bounded_and_incomplete(monkeypatch):
    agent = ReActAgent(max_retry_count=3, max_timeout=600)
    reply, rounds = await _drive_loop(monkeypatch, agent, 35)
    assert len(rounds) == 1
    assert not reply.success
    assert reply.get_dict_context()["termination_reason"] == "timeout"


@pytest.mark.asyncio
async def test_bounded_step_limit_cannot_be_success(monkeypatch):
    agent = ReActAgent(max_retry_count=3, max_timeout=None)
    reply, rounds = await _drive_loop(monkeypatch, agent, 35)
    assert len(rounds) == 3
    assert not reply.success
    assert reply.get_dict_context()["termination_reason"] == "step_limit"


@pytest.mark.asyncio
async def test_cancelling_python_kills_and_waits_for_child(tmp_path, monkeypatch):
    from dbgpt_app.openapi.api_v1.tools import code_interpreter

    started = asyncio.Event()
    process = None
    original = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        nonlocal process
        process = await original(*args, **kwargs)
        started.set()
        return process

    monkeypatch.setattr(code_interpreter.asyncio, "create_subprocess_exec", capture)
    path = tmp_path / "wait.py"
    path.write_text("import time\ntime.sleep(120)", encoding="utf-8")
    task = asyncio.create_task(_run_python_file(str(path), cwd=str(tmp_path)))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_cancelling_sql_interrupts_query_and_reuses_connection(database):
    task = asyncio.create_task(
        query_database(
            database,
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) "
            "SELECT sum(x) FROM n",
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    columns, rows = await asyncio.wait_for(query_database(database, "SELECT 42"), 3)
    assert rows[0][0] == 42


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        {"query_data_gaps": {"margin": "not computed"}},
        {
            "query_data_gaps": {"margin": "not verified"},
            "ratio_validation_status": {"margin": "pending"},
        },
        {"query_data_gaps": {"sales_cost_join": "join count mismatch"}},
    ],
)
async def test_unverified_query_gap_keeps_early_termination_recoverable(state):
    agent = TaskReActAgent(task_state=state)
    output = ActionOutput(content="done", terminate=True, action="terminate")
    message = AgentMessage(action_report=output)
    passed, reason = await agent.verify(message, agent)
    assert not passed
    assert reason
    assert output.terminate is False
    assert output.have_retry is not False
    assert message.get_dict_context().get("termination_reason") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        {
            "query_data_gaps": {"margin": "wage inputs missing"},
            "ratio_validation_status": {"margin": "missing_inputs"},
        },
        {"cancelled_required_steps": ["query wages"]},
        {"task_plan": [{"content": "query wages", "status": "cancelled"}]},
    ],
)
async def test_known_missing_inputs_and_cancelled_steps_end_incomplete(state):
    agent = TaskReActAgent(task_state=state)
    output = ActionOutput(content="partial", terminate=True, action="terminate")
    message = AgentMessage(action_report=output)
    passed, reason = await agent.verify(message, agent)
    assert not passed
    assert output.have_retry is False
    assert output.is_exe_success is False
    assert message.get_dict_context()["termination_reason"] == "missing_deliverables"
    assert task_result_for_reply(message, state).status == "incomplete"
