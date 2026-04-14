"""Persistent storage — chat history and execution logs."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class StorageManager:
    """Manages chat history and execution logs under data_dir."""

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

    # ------------------------------------------------------------------
    # Chat history
    # ------------------------------------------------------------------

    def save_chat(self, messages: list[dict[str, Any]]) -> Path | None:
        """Save full conversation messages to a JSON file. Returns the path."""
        if not self.save_history:
            return None

        # Filter out system prompt for storage (it's always same)
        chat_messages = [m for m in messages if m.get("role") != "system"]
        if not chat_messages:
            return None

        path = self.history_dir / f"chat_{self._session_id}.json"
        path.write_text(
            json.dumps(chat_messages, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        log.debug("Chat history saved: %s", path)
        return path

    def list_history(self, limit: int = 20) -> list[dict[str, str]]:
        """List recent chat history files."""
        if not self.history_dir.exists():
            return []
        files = sorted(self.history_dir.glob("chat_*.json"), reverse=True)[:limit]
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
