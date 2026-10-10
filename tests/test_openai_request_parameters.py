"""Preserve explicitly configured OpenAI-compatible sampling parameters."""

from types import SimpleNamespace

import pytest

from dbgpt.core import ModelRequest
from dbgpt.model.proxy.llms.chatgpt import OpenAILLMClient


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("thinking", [False, True, None])
@pytest.mark.parametrize("model", ["LongCat-2.5-Preview", "test-model"])
def test_longcat_request_thinking_override_preserves_other_provider_defaults(model, thinking, stream):
    client = OpenAILLMClient(openai_client=SimpleNamespace(default_headers={}))
    request = ModelRequest.build_request(model=model, messages=[], context={"extra": {"thinking_enabled": thinking}})
    payload = client._build_request(request, stream=stream)
    if model == "LongCat-2.5-Preview" and isinstance(thinking, bool):
        assert payload["extra_body"]["thinking"] == {"type": "enabled" if thinking else "disabled"}
    else:
        assert "extra_body" not in payload


@pytest.mark.parametrize("temperature", [None, 0, 0.3])
@pytest.mark.parametrize("stream", [False, True])
def test_request_temperature_preserves_zero_and_provider_default(temperature, stream):
    client = OpenAILLMClient(
        openai_client=SimpleNamespace(default_headers={}),
        openai_kwargs={"temperature": 0.7},
    )
    request = ModelRequest(model="test-model", messages=[], temperature=temperature)
    payload = client._build_request(request, stream=stream)
    assert payload["temperature"] == (0.7 if temperature is None else temperature)
    assert payload["stream"] is stream


@pytest.mark.asyncio
async def test_nonstream_reasoning_only_response_preserves_length_finish_reason():
    from unittest.mock import AsyncMock
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None, reasoning_content="PRIVATE_REASONING"), finish_reason="length")],
        usage=SimpleNamespace(dict=lambda: {"completion_tokens": 2000}),
    )
    fake = SimpleNamespace(default_headers={}, chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=completion))))
    client = OpenAILLMClient(openai_client=fake)
    output = await client.generate_v1([], {"model": "test", "stream": False})
    assert output.finish_reason == "length"
    assert output.has_text is False
    assert output.has_thinking is True
