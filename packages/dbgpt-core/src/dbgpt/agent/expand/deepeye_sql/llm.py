"""LLM invocation abstraction for the DeepEye-SQL pipeline.

The pipeline components only depend on an async ``complete`` callable so that
the core is testable and can be driven either by a DB-GPT
:class:`~dbgpt.agent.util.llm.llm_client.AIWrapper` or by any compatible client.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional, Protocol, Sequence, Union

logger = logging.getLogger(__name__)


class LLMComplete(Protocol):
    """Async protocol for completing a chat request.

    ``messages`` is a list of ``{"role": ..., "content": ...}`` dicts.
    """

    async def complete(
        self,
        messages: Sequence[dict],
        *,
        temperature: float = 0.0,
        max_new_tokens: int = 2048,
        **kwargs: Any,
    ) -> str:
        """Complete a chat request and return the text."""
        ...


LLMCompleteFunc = Callable[..., Awaitable[str]]


class AIWrapperAdapter:
    """Adapt a DB-GPT ``AIWrapper`` (or ``LLMClient``) to the ``complete`` protocol.

    ``AIWrapper.create`` accepts ``messages`` plus ``llm_model``, ``temperature``,
    ``max_new_tokens`` and returns the generated text. This adapter pins those
    arguments so the pipeline can call a uniform interface.
    """

    def __init__(
        self,
        wrapper: Any,
        model_name: Optional[str] = None,
        conv_id: Optional[str] = None,
    ):
        self._wrapper = wrapper
        self._model_name = model_name
        self._conv_id = conv_id

    async def complete(
        self,
        messages: Sequence[dict],
        *,
        temperature: float = 0.0,
        max_new_tokens: int = 2048,
        **kwargs: Any,
    ) -> str:
        """Complete a chat request via the wrapped DB-GPT client."""
        if self._wrapper is None:
            raise ValueError("LLM wrapper is not configured")

        # AIWrapper supports generate_text / create. Prefer create (full message
        # list) and fall back to generate_text for a single user prompt.
        create = getattr(self._wrapper, "create", None)
        payload: dict = {
            "messages": [dict(m) for m in messages],
            "temperature": temperature,
            "max_new_tokens": max_new_tokens,
            "stream_out": False,
        }
        if self._model_name:
            payload["llm_model"] = self._model_name
        if self._conv_id:
            payload["conv_id"] = self._conv_id
        if callable(create):
            result = await create(**payload)
            return result or ""

        generate_text = getattr(self._wrapper, "generate_text", None)
        if callable(generate_text):
            user_msgs = [
                m["content"] for m in messages if m.get("role") == "user"
            ]
            prompt = "\n\n".join(user_msgs) or (
                messages[-1]["content"] if messages else ""
            )
            return await generate_text(
                prompt,
                llm_model=self._model_name,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                conv_id=self._conv_id,
            )
        raise ValueError(
            "Unsupported LLM wrapper: expected an AIWrapper-like object with "
            "create() or generate_text()"
        )


async def complete_json(
    complete: Union[LLMComplete, LLMCompleteFunc],
    messages: Sequence[dict],
    *,
    temperature: float = 0.0,
    max_new_tokens: int = 2048,
    **kwargs: Any,
) -> str:
    """Complete a request and return the raw text (convenience helper)."""
    return await complete.complete(
        messages,
        temperature=temperature,
        max_new_tokens=max_new_tokens,
        **kwargs,
    )


def to_complete(
    obj: Union[LLMComplete, LLMCompleteFunc, Any],
    model_name: Optional[str] = None,
    conv_id: Optional[str] = None,
) -> LLMComplete:
    """Coerce an LLM client into the ``LLMComplete`` protocol.

    * If ``obj`` already exposes an async ``complete``, it is used as-is.
    * If ``obj`` is a plain async callable, it is wrapped in a tiny adapter.
    * Otherwise (e.g. a DB-GPT ``AIWrapper``), an :class:`AIWrapperAdapter` is
      returned.
    """
    if obj is None:
        raise ValueError("LLM client is required")
    if hasattr(obj, "complete") and callable(obj.complete):
        return obj  # type: ignore[return-value]
    if callable(obj):
        return _CallableAdapter(obj)
    return AIWrapperAdapter(obj, model_name=model_name, conv_id=conv_id)


class _CallableAdapter:
    """Adapt a bare async ``(messages, **kwargs) -> str`` callable."""

    def __init__(self, func: LLMCompleteFunc):
        self._func = func

    async def complete(
        self,
        messages: Sequence[dict],
        *,
        temperature: float = 0.0,
        max_new_tokens: int = 2048,
        **kwargs: Any,
    ) -> str:
        return await self._func(
            list(messages),
            temperature=temperature,
            max_new_tokens=max_new_tokens,
            **kwargs,
        )
