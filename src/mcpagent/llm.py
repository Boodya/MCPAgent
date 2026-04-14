"""Azure OpenAI LLM client with streaming and retry logic."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncIterator

from openai import AsyncAzureOpenAI, APIStatusError

from mcpagent.config import ModelConfig

log = logging.getLogger(__name__)


class LLMClient:
    """Thin async wrapper around Azure OpenAI Chat Completions."""

    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        api_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
        if not api_key:
            raise ValueError(
                "Environment variable AZURE_OPENAI_API_KEY is not set. "
                "Please set it in your .env file or environment."
            )
        self._client = AsyncAzureOpenAI(
            azure_endpoint=config.endpoint,
            api_key=api_key,
            api_version=config.api_version,
        )
        self.deployment = config.deployment

    # ------------------------------------------------------------------
    # Streaming chat
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[Any]:
        """Stream chat completion chunks. Yields raw OpenAI chunk objects."""
        kwargs: dict[str, Any] = {
            "model": self.deployment,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_completion_tokens": max_tokens or self.config.max_tokens,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["parallel_tool_calls"] = True

        return await self._request_with_retry(**kwargs)

    # ------------------------------------------------------------------
    # Non-streaming (used internally for simple completions)
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        """Non-streaming completion. Returns full ChatCompletion response."""
        kwargs: dict[str, Any] = {
            "model": self.deployment,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_completion_tokens": max_tokens or self.config.max_tokens,
            "stream": False,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["parallel_tool_calls"] = True

        return await self._request_with_retry(**kwargs)

    # ------------------------------------------------------------------
    # Retry logic with exponential backoff for 429 and 5xx
    # ------------------------------------------------------------------

    async def _request_with_retry(
        self, *, _max_retries: int = 3, _base_delay: float = 1.0, **kwargs: Any
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(_max_retries + 1):
            try:
                return await self._client.chat.completions.create(**kwargs)
            except APIStatusError as exc:
                status = exc.status_code
                if status in (429, 500, 502, 503) and attempt < _max_retries:
                    delay = _base_delay * (2 ** attempt)
                    log.warning(
                        "LLM request failed (%s), retrying in %.1fs (attempt %d/%d)",
                        status, delay, attempt + 1, _max_retries,
                    )
                    last_exc = exc
                    await asyncio.sleep(delay)
                else:
                    raise
        raise last_exc  # type: ignore[misc]

    async def close(self) -> None:
        await self._client.close()
