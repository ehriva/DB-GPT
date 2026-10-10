"""User-visible responses must not be copied to server stdout."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from dbgpt.agent import AgentMessage
from dbgpt.agent.core.base_agent import ConversableAgent
from dbgpt.agent.util.llm.llm_client import AIWrapper
from dbgpt.core import ModelInferenceMetrics, ModelMessage, ModelOutput, ModelRequest
from dbgpt.model.cluster.worker.default_worker import DefaultModelWorker
from dbgpt.model.proxy.llms.chatgpt import OpenAILLMClient


def test_streaming_model_text_is_returned_without_stdout(capsys, caplog):
    caplog.set_level(logging.DEBUG)
    worker = DefaultModelWorker.__new__(DefaultModelWorker)
    marker = "PRIVATE_TEST_BODY_9FIX"
    result, delta, text, metrics = worker._handle_output(
        ModelOutput.build(marker), "", {}, ModelInferenceMetrics.create_metrics(), True
    )
    assert result.text == marker and delta == marker and text == marker
    assert marker not in capsys.readouterr().out
    assert marker not in caplog.text


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_complete_worker_stream_logs_only_metrics(
    monkeypatch, capsys, caplog, asynchronous
):
    caplog.set_level(logging.DEBUG)
    import dbgpt.model.cluster.worker.default_worker as module

    monkeypatch.setattr(module.root_tracer, "start_span", Mock(return_value=Mock()))
    worker = DefaultModelWorker.__new__(DefaultModelWorker)
    worker.model = None
    worker.tokenizer = None
    worker.context_len = 4096
    marker = "PRIVATE_TEST_COMPLETE_STREAM_9FIX"

    def generate(*args):
        yield marker

    async def async_generate(*args):
        yield marker

    worker._prepare_generate_stream = lambda params, **kw: (
        params,
        {},
        async_generate if asynchronous else generate,
        Mock(),
    )
    if asynchronous:
        outputs = [
            output async for output in worker.async_generate_stream({"prompt": marker})
        ]
    else:
        outputs = list(worker.generate_stream({"prompt": marker}))
    assert outputs[-1].text == marker
    assert marker not in capsys.readouterr().out
    assert marker not in caplog.text


def test_provider_error_does_not_echo_private_error_body(capsys, caplog):
    caplog.set_level(logging.DEBUG)
    marker = "PRIVATE_TEST_EXCEPTION_9FIX"
    worker = DefaultModelWorker.__new__(DefaultModelWorker)
    result = worker._handle_exception(RuntimeError(marker))
    assert result.error_code != 0
    assert marker not in result.text
    assert marker not in caplog.text


@pytest.mark.asyncio
async def test_proxy_client_does_not_log_model_messages(caplog):
    caplog.set_level(logging.DEBUG)
    marker = "PRIVATE_PROXY_CANARY_9FIX"
    client = OpenAILLMClient.__new__(OpenAILLMClient)
    client.local_covert_message = lambda request, converter: request
    client._build_request = lambda request: {"model": "test"}
    client.generate_v1 = AsyncMock(return_value=ModelOutput.build(marker))
    request = ModelRequest.build_request(
        model="test", messages=[ModelMessage(role="human", content=marker)]
    )
    assert (await client.generate(request)).text == marker
    assert marker not in caplog.text


@pytest.mark.asyncio
async def test_agent_llm_wrapper_does_not_log_request_body(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    marker = "PRIVATE_WRAPPER_CANARY_9FIX"

    async def generate_stream(request):
        yield ModelOutput.build(marker)

    wrapper = AIWrapper(SimpleNamespace(generate_stream=generate_stream))
    result = await wrapper.create(
        llm_model="test",
        temperature=0,
        max_new_tokens=50,
        messages=[{"role": "user", "content": marker}],
        stream_out=False,
    )
    assert result == marker
    assert marker not in caplog.text
    assert marker not in capsys.readouterr().out


def test_agent_message_is_not_printed_as_a_business_log(capsys, caplog):
    caplog.set_level(logging.DEBUG)
    marker = "PRIVATE_TEST_AGENT_BODY_9FIX"
    message = AgentMessage(content=marker, model_name="test")
    sender = SimpleNamespace(name="sender", role="user")
    receiver = SimpleNamespace(name="receiver", role="assistant")
    ConversableAgent._print_received_message(receiver, message, sender)
    assert marker not in capsys.readouterr().out
    assert marker not in caplog.text
