"""Centralized operations log — structured JSONL log for all LLM / tool / workflow events.

Every event is a single JSON line in ``data_dir/ops/ops_YYYYMMDD.log``.
One file per day, auto-rotated. The log captures:
- LLM requests and responses (model, token usage, latency)
- Tool calls with arguments, results, errors
- Workflow lifecycle (start, step begin/end, finish)
- Errors with full context

Fields common to every record:
    ts        – ISO-8601 timestamp
    event     – event type string (see EVENT_* constants)
    agent     – agent preset name (or "system")
    run_id    – workflow run id (if applicable)
    step_id   – workflow step id (if applicable)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Event type constants
EVENT_LLM_REQUEST = "llm.request"
EVENT_LLM_RESPONSE = "llm.response"
EVENT_LLM_ERROR = "llm.error"
EVENT_TOOL_CALL = "tool.call"
EVENT_TOOL_RESULT = "tool.result"
EVENT_TOOL_ERROR = "tool.error"
EVENT_WORKFLOW_START = "workflow.start"
EVENT_WORKFLOW_STEP_START = "workflow.step.start"
EVENT_WORKFLOW_STEP_END = "workflow.step.end"
EVENT_WORKFLOW_END = "workflow.end"
EVENT_ERROR = "error"


class OpsLog:
    """Append-only structured operations logger.

    Thread-safe via file append mode. Each ``emit()`` call writes one JSON
    line and flushes immediately — no buffering. Cheap enough for every
    LLM call (one ``open`` + ``write`` + ``flush``).

    Usage::

        ops = OpsLog(data_dir)
        ops.emit(EVENT_LLM_REQUEST, agent="architect", model="gpt-4.1", messages_count=5)
    """

    def __init__(self, data_dir: Path | None) -> None:
        self._ops_dir: Path | None = None
        if data_dir:
            self._ops_dir = data_dir / "ops"
            self._ops_dir.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self._ops_dir is not None

    def emit(self, event: str, *, agent: str = "system", **fields: Any) -> None:
        """Write one structured log entry."""
        if not self._ops_dir:
            return
        now = datetime.now(timezone.utc)
        record: dict[str, Any] = {
            "ts": now.isoformat(),
            "event": event,
            "agent": agent,
        }
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False, default=str)
        path = self._ops_dir / f"ops_{now.strftime('%Y%m%d')}.log"
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as exc:
            log.warning("OpsLog write failed: %s", exc)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def llm_request(
        self,
        *,
        agent: str = "system",
        model: str = "",
        messages_count: int = 0,
        tools_count: int = 0,
        run_id: int | None = None,
        step_id: str | None = None,
    ) -> "_LLMTimer":
        """Start timing an LLM call. Use as a context manager::

            timer = ops.llm_request(agent="architect", model="gpt-4.1")
            # ... perform LLM call ...
            timer.complete(tokens_prompt=100, tokens_completion=50, text_length=200)
        """
        self.emit(
            EVENT_LLM_REQUEST,
            agent=agent,
            model=model,
            messages_count=messages_count,
            tools_count=tools_count,
            run_id=run_id,
            step_id=step_id,
        )
        return _LLMTimer(self, agent=agent, model=model, run_id=run_id, step_id=step_id)

    def tool_call(
        self,
        *,
        agent: str = "system",
        tool: str,
        args: dict[str, Any] | None = None,
        run_id: int | None = None,
        step_id: str | None = None,
    ) -> None:
        self.emit(
            EVENT_TOOL_CALL,
            agent=agent,
            tool=tool,
            args=_safe_truncate(args),
            run_id=run_id,
            step_id=step_id,
        )

    def tool_result(
        self,
        *,
        agent: str = "system",
        tool: str,
        result_length: int = 0,
        error: str | None = None,
        duration_ms: float | None = None,
        run_id: int | None = None,
        step_id: str | None = None,
    ) -> None:
        event = EVENT_TOOL_ERROR if error else EVENT_TOOL_RESULT
        self.emit(
            event,
            agent=agent,
            tool=tool,
            result_length=result_length,
            error=error,
            duration_ms=duration_ms,
            run_id=run_id,
            step_id=step_id,
        )

    def workflow_start(self, *, workflow: str, run_id: int, trigger: str = "manual") -> None:
        self.emit(EVENT_WORKFLOW_START, workflow=workflow, run_id=run_id, trigger=trigger)

    def workflow_step_start(
        self, *, workflow: str, run_id: int, step_id: str, agent: str
    ) -> None:
        self.emit(
            EVENT_WORKFLOW_STEP_START,
            workflow=workflow,
            run_id=run_id,
            step_id=step_id,
            agent=agent,
        )

    def workflow_step_end(
        self,
        *,
        workflow: str,
        run_id: int,
        step_id: str,
        agent: str,
        status: str,
        error: str | None = None,
        duration_ms: float | None = None,
    ) -> None:
        self.emit(
            EVENT_WORKFLOW_STEP_END,
            workflow=workflow,
            run_id=run_id,
            step_id=step_id,
            agent=agent,
            status=status,
            error=error,
            duration_ms=duration_ms,
        )

    def workflow_end(
        self, *, workflow: str, run_id: int, status: str, error: str | None = None
    ) -> None:
        self.emit(
            EVENT_WORKFLOW_END,
            workflow=workflow,
            run_id=run_id,
            status=status,
            error=error,
        )


class _LLMTimer:
    """Lightweight timer returned by ``OpsLog.llm_request()``."""

    def __init__(
        self,
        ops: OpsLog,
        *,
        agent: str,
        model: str,
        run_id: int | None,
        step_id: str | None,
    ) -> None:
        self._ops = ops
        self._agent = agent
        self._model = model
        self._run_id = run_id
        self._step_id = step_id
        self._start = time.perf_counter()

    def complete(
        self,
        *,
        tokens_prompt: int = 0,
        tokens_completion: int = 0,
        text_length: int = 0,
    ) -> None:
        elapsed_ms = (time.perf_counter() - self._start) * 1000
        self._ops.emit(
            EVENT_LLM_RESPONSE,
            agent=self._agent,
            model=self._model,
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            text_length=text_length,
            duration_ms=round(elapsed_ms, 1),
            run_id=self._run_id,
            step_id=self._step_id,
        )

    def fail(self, error: str) -> None:
        elapsed_ms = (time.perf_counter() - self._start) * 1000
        self._ops.emit(
            EVENT_LLM_ERROR,
            agent=self._agent,
            model=self._model,
            error=error,
            duration_ms=round(elapsed_ms, 1),
            run_id=self._run_id,
            step_id=self._step_id,
        )


def _safe_truncate(obj: Any, max_len: int = 500) -> Any:
    """Truncate arg values for logging (avoid huge payloads)."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if isinstance(v, str) and len(v) > max_len:
                result[k] = v[:max_len] + f"…({len(v)} chars)"
            else:
                result[k] = v
        return result
    return obj
