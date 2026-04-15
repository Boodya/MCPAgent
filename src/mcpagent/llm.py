"""LLM client with streaming and retry logic. Supports Azure OpenAI and any
OpenAI-compatible provider (NVIDIA NIM, OpenAI, etc.)."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncIterator

from openai import AsyncAzureOpenAI, AsyncOpenAI, APIStatusError

from mcpagent.config import ModelConfig
from mcpagent.ops_log import OpsLog

log = logging.getLogger(__name__)

_PROVIDER_KEY_ENV: dict[str, str] = {
    "azure": "AZURE_OPENAI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
}


def _default_key_env(provider: str) -> str:
    return _PROVIDER_KEY_ENV.get(provider, f"{provider.upper()}_API_KEY")


class LLMClient:
    """Thin async wrapper around OpenAI Chat Completions.

    Supports:
    - provider=azure  — Azure OpenAI (default)
    - provider=openai — OpenAI API
    - provider=nvidia (or any other) — OpenAI-compatible endpoint via base_url
    """

    def __init__(self, config: ModelConfig, *, ops: OpsLog | None = None) -> None:
        self.config = config
        self.ops = ops or OpsLog(None)

        # Resolve API key: use api_key_env if specified, else provider defaults
        key_env = config.api_key_env or _default_key_env(config.provider)
        api_key = os.environ.get(key_env, "")
        if not api_key:
            raise ValueError(
                f"Environment variable {key_env} is not set. "
                "Please set it in your .env file or environment."
            )

        if config.provider == "azure":
            self._client: AsyncAzureOpenAI | AsyncOpenAI = AsyncAzureOpenAI(
                azure_endpoint=config.endpoint,
                api_key=api_key,
                api_version=config.api_version,
            )
        else:
            # OpenAI-compatible: OpenAI, NVIDIA NIM, local, etc.
            client_kwargs: dict[str, Any] = {"api_key": api_key}
            if config.endpoint:
                client_kwargs["base_url"] = config.endpoint
            self._client = AsyncOpenAI(**client_kwargs)

        self.deployment = config.deployment or config.model_name

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
        agent_name: str = "system",
        run_id: int | None = None,
        step_id: str | None = None,
    ) -> AsyncIterator[Any]:
        """Stream chat completion chunks. Yields raw OpenAI chunk objects."""
        kwargs: dict[str, Any] = {
            "model": self.deployment,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_completion_tokens": max_tokens or self.config.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["parallel_tool_calls"] = True

        timer = self.ops.llm_request(
            agent=agent_name,
            model=self.deployment,
            messages_count=len(messages),
            tools_count=len(tools) if tools else 0,
            run_id=run_id,
            step_id=step_id,
        )

        try:
            stream = await self._request_with_retry(**kwargs)
            return _LoggingStreamWrapper(stream, timer)
        except Exception as exc:
            timer.fail(str(exc))
            raise

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
        agent_name: str = "system",
        run_id: int | None = None,
        step_id: str | None = None,
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

        timer = self.ops.llm_request(
            agent=agent_name,
            model=self.deployment,
            messages_count=len(messages),
            tools_count=len(tools) if tools else 0,
            run_id=run_id,
            step_id=step_id,
        )

        try:
            result = await self._request_with_retry(**kwargs)
            # Extract token usage from response
            usage = getattr(result, "usage", None)
            timer.complete(
                tokens_prompt=getattr(usage, "prompt_tokens", 0) if usage else 0,
                tokens_completion=getattr(usage, "completion_tokens", 0) if usage else 0,
            )
            return result
        except Exception as exc:
            timer.fail(str(exc))
            raise

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


class _LoggingStreamWrapper:
    """Wraps an async stream and logs usage when exhausted."""

    def __init__(self, stream: Any, timer: Any) -> None:
        self._stream = stream
        self._timer = timer
        self._text_len = 0
        self._tokens_prompt = 0
        self._tokens_completion = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await self._stream.__anext__()
            # Track text length
            if chunk.choices:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    self._text_len += len(delta.content)
            # Capture usage from final chunk (stream_options: include_usage)
            if hasattr(chunk, "usage") and chunk.usage:
                self._tokens_prompt = getattr(chunk.usage, "prompt_tokens", 0)
                self._tokens_completion = getattr(chunk.usage, "completion_tokens", 0)
            return chunk
        except StopAsyncIteration:
            self._timer.complete(
                tokens_prompt=self._tokens_prompt,
                tokens_completion=self._tokens_completion,
                text_length=self._text_len,
            )
            raise
