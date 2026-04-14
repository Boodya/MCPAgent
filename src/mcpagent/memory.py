"""Memory system — three-scoped markdown-file memory under a single data directory."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


class MemoryManager:
    """Manages a three-tier memory system backed by markdown files on disk.

    All data lives under ``data_dir``:
        data_dir/memories/user/     — persistent user notes
        data_dir/memories/session/  — per-conversation (cleared on exit)
        data_dir/memories/repo/     — project-specific notes
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        mem_root = self.data_dir / "memories"
        self.user_dir = mem_root / "user"
        self.session_dir = mem_root / "session"
        self.repo_dir = mem_root / "repo"

        for d in (self.user_dir, self.session_dir, self.repo_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def _resolve(self, virtual_path: str) -> Path | None:
        """Map a virtual path like ``memories/user/notes.md`` to a real filesystem path."""
        vp = virtual_path.strip().replace("\\", "/").strip("/")

        # Root: memories/
        if vp in ("memories", "memories/"):
            return self.data_dir / "memories"

        if vp.startswith("memories/user"):
            rel = vp.removeprefix("memories/user").strip("/")
            return self.user_dir / rel if rel else self.user_dir

        if vp.startswith("memories/session"):
            rel = vp.removeprefix("memories/session").strip("/")
            return self.session_dir / rel if rel else self.session_dir

        if vp.startswith("memories/repo"):
            rel = vp.removeprefix("memories/repo").strip("/")
            return self.repo_dir / rel if rel else self.repo_dir

        # Bare "memories/something" without scope → user scope
        if vp.startswith("memories/"):
            rel = vp.removeprefix("memories/").strip("/")
            return self.user_dir / rel if rel else self.user_dir

        # Fallback: user scope
        return self.user_dir / vp

    # ------------------------------------------------------------------
    # Operations (mirror VS Code Copilot memory tool)
    # ------------------------------------------------------------------

    def view(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        """View a file's contents or list a directory."""
        real = self._resolve(path)
        if real is None:
            return "Error: repository memory not configured."
        if not real.exists():
            return f"Error: path does not exist: {path}"

        if real.is_dir():
            entries = sorted(real.iterdir())
            lines = []
            for e in entries:
                name = e.name + ("/" if e.is_dir() else "")
                lines.append(name)
            return "\n".join(lines) if lines else "(empty directory)"

        text = real.read_text(encoding="utf-8")
        if start_line is not None or end_line is not None:
            all_lines = text.splitlines(keepends=True)
            s = (start_line or 1) - 1
            e = end_line or len(all_lines)
            return "".join(all_lines[s:e])
        return text

    def create(self, path: str, content: str) -> str:
        """Create a new file. Fails if it already exists."""
        real = self._resolve(path)
        if real is None:
            return "Error: repository memory not configured."
        if real.exists():
            return f"Error: file already exists: {path}"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text(content, encoding="utf-8")
        return f"Created: {path}"

    def str_replace(self, path: str, old_str: str, new_str: str) -> str:
        """Replace exactly one occurrence of *old_str* with *new_str*."""
        real = self._resolve(path)
        if real is None:
            return "Error: repository memory not configured."
        if not real.exists():
            return f"Error: file does not exist: {path}"

        text = real.read_text(encoding="utf-8")
        count = text.count(old_str)
        if count == 0:
            return f"Error: old_str not found in {path}"
        if count > 1:
            return f"Error: old_str appears {count} times (must be exactly 1)"
        text = text.replace(old_str, new_str, 1)
        real.write_text(text, encoding="utf-8")
        return f"Updated: {path}"

    def insert(self, path: str, line: int, text: str) -> str:
        """Insert *text* at line *line* (0-based; 0 = before first line)."""
        real = self._resolve(path)
        if real is None:
            return "Error: repository memory not configured."
        if not real.exists():
            return f"Error: file does not exist: {path}"

        lines = real.read_text(encoding="utf-8").splitlines(keepends=True)
        insert_lines = text.splitlines(keepends=True)
        if not text.endswith("\n"):
            insert_lines[-1] += "\n"
        lines[line:line] = insert_lines
        real.write_text("".join(lines), encoding="utf-8")
        return f"Inserted at line {line}: {path}"

    def delete(self, path: str) -> str:
        """Delete a file or directory."""
        real = self._resolve(path)
        if real is None:
            return "Error: repository memory not configured."
        if not real.exists():
            return f"Error: path does not exist: {path}"

        if real.is_dir():
            shutil.rmtree(real)
        else:
            real.unlink()
        return f"Deleted: {path}"

    def rename(self, old_path: str, new_path: str) -> str:
        """Rename / move a file or directory (within the same scope)."""
        real_old = self._resolve(old_path)
        real_new = self._resolve(new_path)
        if real_old is None or real_new is None:
            return "Error: repository memory not configured."
        if not real_old.exists():
            return f"Error: path does not exist: {old_path}"
        real_new.parent.mkdir(parents=True, exist_ok=True)
        real_old.rename(real_new)
        return f"Renamed: {old_path} → {new_path}"

    # ------------------------------------------------------------------
    # Auto-load user memory for system prompt injection
    # ------------------------------------------------------------------

    def load_user_memory_summary(self, max_lines: int = 200) -> str:
        """Load first *max_lines* lines of all user memory files for injection into system prompt."""
        if not self.user_dir.exists():
            return ""

        parts: list[str] = []
        for f in sorted(self.user_dir.rglob("*.md")):
            rel = f.relative_to(self.user_dir)
            text = f.read_text(encoding="utf-8")
            lines = text.splitlines()[:max_lines]
            parts.append(f"## {rel}\n" + "\n".join(lines))

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_session(self) -> None:
        """Clear session memory contents (but keep the directory)."""
        if self.session_dir.exists():
            shutil.rmtree(self.session_dir, ignore_errors=True)
            self.session_dir.mkdir(parents=True, exist_ok=True)
