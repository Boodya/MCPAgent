"""Context window management — token counting, truncation, auto-summarization."""

from __future__ import annotations

import json
import logging
from typing import Any

import tiktoken

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

# Cache encoder per model to avoid repeated initialization
_ENCODER_CACHE: dict[str, tiktoken.Encoding] = {}


def _get_encoder(model: str = "gpt-4o") -> tiktoken.Encoding:
    """Get (or cache) a tiktoken encoder for the given model."""
    if model not in _ENCODER_CACHE:
        try:
            _ENCODER_CACHE[model] = tiktoken.encoding_for_model(model)
        except KeyError:
            # Fallback to cl100k_base (covers GPT-4, GPT-4o, GPT-3.5-turbo)
            _ENCODER_CACHE[model] = tiktoken.get_encoding("cl100k_base")
    return _ENCODER_CACHE[model]


def count_tokens(text: str, model: str = "gpt-4o") -> int:
    """Count tokens in a string."""
    enc = _get_encoder(model)
    return len(enc.encode(text))


def count_message_tokens(messages: list[dict[str, Any]], model: str = "gpt-4o") -> int:
    """Count total tokens across a list of OpenAI chat messages.

    Uses the standard overhead calculation:
    - Each message: +3 tokens (role framing)
    - Final reply priming: +3 tokens
    - tool_calls are serialized as JSON for counting
    """
    enc = _get_encoder(model)
    total = 0

    for msg in messages:
        total += 3  # per-message overhead

        # role
        role = msg.get("role", "")
        total += len(enc.encode(role))

        # content
        content = msg.get("content") or ""
        if isinstance(content, str):
            total += len(enc.encode(content))
        elif isinstance(content, list):
            # Multi-part content (text + images etc.)
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += len(enc.encode(part.get("text", "")))

        # name (if present)
        if name := msg.get("name"):
            total += len(enc.encode(name)) + 1

        # tool_calls (assistant message with function calls)
        if tool_calls := msg.get("tool_calls"):
            total += len(enc.encode(json.dumps(tool_calls, ensure_ascii=False)))

    total += 3  # reply priming
    return total


# ---------------------------------------------------------------------------
# Tool result truncation
# ---------------------------------------------------------------------------

def truncate_tool_result(result: str, max_tokens: int, model: str = "gpt-4o") -> str:
    """Truncate a tool result to fit within max_tokens.

    Returns original string if within limit, otherwise truncates and appends
    a notice about truncation.
    """
    if max_tokens <= 0:
        return result

    token_count = count_tokens(result, model)
    if token_count <= max_tokens:
        return result

    enc = _get_encoder(model)
    tokens = enc.encode(result)
    # Leave room for the truncation notice (~30 tokens)
    truncated_tokens = tokens[: max_tokens - 30]
    truncated_text = enc.decode(truncated_tokens)

    notice = (
        f"\n\n... [TRUNCATED: showing ~{max_tokens} of {token_count} tokens. "
        f"Use targeted queries to get specific parts.]"
    )
    return truncated_text + notice


# ---------------------------------------------------------------------------
# Conversation summarization
# ---------------------------------------------------------------------------

SUMMARIZE_SYSTEM_PROMPT = """\
You are a conversation summarizer. Condense the conversation below into a concise summary \
that preserves ALL critical information:
- Key decisions and conclusions
- Important facts, file paths, code snippets mentioned
- Tool calls made and their results (briefly)
- Any pending tasks or open questions
- User preferences expressed

Output ONLY the summary, no preamble. Use bullet points. Be concise but complete.
Write in the same language the user is using."""


async def summarize_messages(
    messages: list[dict[str, Any]],
    llm: Any,
    max_summary_tokens: int = 1000,
) -> str:
    """Use the LLM to summarize a list of messages.

    Args:
        messages: The messages to summarize (excluding system prompt).
        llm: LLMClient instance with a .complete() method.
        max_summary_tokens: Maximum tokens for the summary output.

    Returns:
        A concise summary string.
    """
    # Build a simplified transcript for the summarizer
    transcript_parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content") or ""

        if role == "tool":
            tool_id = msg.get("tool_call_id", "?")
            # Truncate long tool results for the summarizer input
            if len(content) > 2000:
                content = content[:2000] + "...[truncated]"
            transcript_parts.append(f"[tool result {tool_id}]: {content}")
        elif role == "assistant" and msg.get("tool_calls"):
            calls = msg["tool_calls"]
            call_strs = []
            for tc in calls:
                fn = tc.get("function", {})
                call_strs.append(f"{fn.get('name', '?')}({fn.get('arguments', '')[:200]})")
            transcript_parts.append(f"[assistant calls: {', '.join(call_strs)}]")
            if content:
                transcript_parts.append(f"[assistant]: {content}")
        else:
            transcript_parts.append(f"[{role}]: {content}")

    transcript = "\n".join(transcript_parts)

    summary_messages = [
        {"role": "system", "content": SUMMARIZE_SYSTEM_PROMPT},
        {"role": "user", "content": f"Summarize this conversation:\n\n{transcript}"},
    ]

    response = await llm.complete(
        summary_messages,
        max_tokens=max_summary_tokens,
        temperature=0.1,
    )

    return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Context manager: orchestrates counting + summarization
# ---------------------------------------------------------------------------

class ContextManager:
    """Manages the agent's message list to stay within context window limits."""

    def __init__(
        self,
        context_window: int = 128_000,
        summarize_threshold: float = 0.7,
        max_tool_result_tokens: int = 8_000,
        summary_max_tokens: int = 1_000,
        model: str = "gpt-4o",
    ) -> None:
        self.context_window = context_window
        self.summarize_threshold = summarize_threshold
        self.max_tool_result_tokens = max_tool_result_tokens
        self.summary_max_tokens = summary_max_tokens
        self.model = model

        # Running token stats
        self.last_counted_tokens: int = 0
        self.summarization_count: int = 0

    @property
    def token_limit(self) -> int:
        """The token threshold that triggers summarization."""
        return int(self.context_window * self.summarize_threshold)

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Count and cache total tokens in messages."""
        self.last_counted_tokens = count_message_tokens(messages, self.model)
        return self.last_counted_tokens

    def truncate_tool_result(self, result: str) -> str:
        """Truncate a tool result if it exceeds the configured limit."""
        return truncate_tool_result(result, self.max_tool_result_tokens, self.model)

    def needs_summarization(self, messages: list[dict[str, Any]]) -> bool:
        """Check if the conversation needs summarization."""
        total = self.count_tokens(messages)
        return total >= self.token_limit

    async def maybe_summarize(
        self,
        messages: list[dict[str, Any]],
        llm: Any,
    ) -> list[dict[str, Any]]:
        """If context is too large, summarize older messages and return trimmed list.

        Keeps: system prompt (index 0) + recent messages.
        Summarizes: everything between system prompt and recent messages.

        Returns the (possibly modified) messages list.
        """
        total = self.count_tokens(messages)
        if total < self.token_limit:
            return messages

        log.info(
            "Context at %d tokens (limit %d, window %d). Triggering summarization.",
            total, self.token_limit, self.context_window,
        )

        # Find the split point: keep system prompt + last N messages
        # We want to keep enough recent context (~30% of window)
        keep_target = int(self.context_window * 0.25)

        # Walk backwards from end to find how many messages fit in keep_target
        keep_from = len(messages)
        running = 0
        for i in range(len(messages) - 1, 0, -1):
            msg_tokens = count_message_tokens([messages[i]], self.model)
            if running + msg_tokens > keep_target:
                keep_from = i + 1
                break
            running += msg_tokens
        else:
            # All messages fit in keep_target — nothing to summarize
            return messages

        # Ensure we keep at least the last 4 messages
        keep_from = min(keep_from, len(messages) - 4)
        keep_from = max(keep_from, 2)  # Don't summarize just the system prompt

        # Messages to summarize: between system prompt and keep_from
        to_summarize = messages[1:keep_from]
        if not to_summarize:
            return messages

        log.info("Summarizing messages 1..%d (keeping %d..%d)", keep_from - 1, keep_from, len(messages) - 1)

        try:
            summary = await summarize_messages(
                to_summarize, llm,
                max_summary_tokens=self.summary_max_tokens,
            )
        except Exception as exc:
            log.error("Summarization failed: %s", exc)
            return messages

        self.summarization_count += 1

        # Build new messages list: system_prompt + summary + recent messages
        summary_msg: dict[str, Any] = {
            "role": "system",
            "content": (
                f"[Conversation summary #{self.summarization_count} — "
                f"earlier messages were summarized to save context]\n\n{summary}"
            ),
        }

        new_messages = [messages[0], summary_msg] + messages[keep_from:]
        new_total = self.count_tokens(new_messages)

        log.info(
            "Summarization complete: %d → %d tokens (saved %d). "
            "Messages: %d → %d",
            total, new_total, total - new_total,
            len(messages), len(new_messages),
        )

        return new_messages

    def get_stats(self) -> dict[str, Any]:
        """Return current context stats for display."""
        return {
            "tokens": self.last_counted_tokens,
            "context_window": self.context_window,
            "threshold": self.token_limit,
            "usage_pct": round(self.last_counted_tokens / self.context_window * 100, 1)
                if self.context_window > 0 else 0,
            "summarizations": self.summarization_count,
        }
