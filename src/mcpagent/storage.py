"""Persistent storage — chat history and execution logs."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class StorageManager:
    """Manages chat history and execution logs under data_dir.

    Chat history uses append-only JSONL format — each message is written
    as a separate line the moment it is appended.  This guarantees nothing
    is lost on crash, agent switch, or sub-agent call.
    """

    def __init__(self, data_dir: Path, *, save_history: bool = True, save_logs: bool = True) -> None:
        self.data_dir = data_dir
        self.save_history = save_history
        self.save_logs = save_logs

        self.history_dir = data_dir / "history"
        self.logs_dir = data_dir / "logs"

        if save_history:
            self.history_dir.mkdir(parents=True, exist_ok=True)
        if save_logs:
            self.logs_dir.mkdir(parents=True, exist_ok=True)

        # Current session
        self._session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._log_entries: list[dict[str, Any]] = []

        # Track how many messages we already flushed so we only append new ones
        self._chat_flushed_count: int = 0

    # ------------------------------------------------------------------
    # Chat history — append-only JSONL
    # ------------------------------------------------------------------

    @property
    def _chat_path(self) -> Path:
        return self.history_dir / f"chat_{self._session_id}.jsonl"

    def append_messages(self, messages: list[dict[str, Any]]) -> None:
        """Append only new messages (since last flush) to the JSONL file."""
        if not self.save_history:
            return

        # Skip system messages — they are always the same
        non_system = [m for m in messages if m.get("role") != "system"]
        new_msgs = non_system[self._chat_flushed_count:]
        if not new_msgs:
            return

        with open(self._chat_path, "a", encoding="utf-8") as f:
            for msg in new_msgs:
                f.write(json.dumps(msg, ensure_ascii=False, default=str) + "\n")

        self._chat_flushed_count = len(non_system)
        log.debug("Appended %d chat messages to %s", len(new_msgs), self._chat_path)

    def save_chat(self, messages: list[dict[str, Any]]) -> Path | None:
        """Flush any remaining messages to the JSONL file. Returns the path.

        This is the backward-compatible entry point called at end of session
        and after each exchange.
        """
        if not self.save_history:
            return None

        self.append_messages(messages)
        return self._chat_path if self._chat_path.exists() else None

    def write_event(self, event_type: str, **data: Any) -> None:
        """Write a marker event (agent switch, sub-agent call) into chat history."""
        if not self.save_history:
            return
        entry = {
            "role": "event",
            "event": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **data,
        }
        with open(self._chat_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def write_sub_agent_messages(self, agent_name: str, messages: list[dict[str, Any]]) -> None:
        """Append sub-agent messages to the chat history, tagged with the agent name."""
        if not self.save_history or not messages:
            return
        with open(self._chat_path, "a", encoding="utf-8") as f:
            for msg in messages:
                tagged = {**msg, "_sub_agent": agent_name}
                f.write(json.dumps(tagged, ensure_ascii=False, default=str) + "\n")

    def list_history(self, limit: int = 20) -> list[dict[str, str]]:
        """List recent chat history files."""
        if not self.history_dir.exists():
            return []
        files = sorted(
            list(self.history_dir.glob("chat_*.jsonl")) + list(self.history_dir.glob("chat_*.json")),
            reverse=True,
        )[:limit]
        result = []
        for f in files:
            result.append({
                "file": f.name,
                "date": f.stem.removeprefix("chat_"),
                "size": f"{f.stat().st_size / 1024:.1f} KB",
            })
        return result

    # ------------------------------------------------------------------
    # Execution logs
    # ------------------------------------------------------------------

    def log_event(self, event_type: str, **data: Any) -> None:
        """Append a structured event to the current session log."""
        if not self.save_logs:
            return
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            **data,
        }
        self._log_entries.append(entry)

    def flush_logs(self) -> Path | None:
        """Write accumulated log entries to disk. Called at end of session or periodically."""
        if not self.save_logs or not self._log_entries:
            return None

        path = self.logs_dir / f"session_{self._session_id}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            for entry in self._log_entries:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        count = len(self._log_entries)
        self._log_entries.clear()
        log.debug("Flushed %d log entries to %s", count, path)
        return path
