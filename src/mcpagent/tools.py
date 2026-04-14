"""Unified tool registry — built-in filesystem/memory tools + MCP tools."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Awaitable

from mcpagent.config import ToolsConfig
from mcpagent.memory import MemoryManager
from mcpagent.mcp_manager import MCPManager


# Type for a tool handler: async (arguments: dict) -> str
ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict:
    s: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        s["required"] = required
    return s


# Default descriptions — used when config doesn't override
_DEFAULT_DESCRIPTIONS: dict[str, str] = {
    "read_file": "Read the contents of a file. Specify startLine/endLine for partial reads.",
    "write_file": "Create or overwrite a file with the given content.",
    "list_dir": "List contents of a directory.",
    "grep_search": "Search for a text pattern (regex) in files under a directory.",
    "run_command": "Run a shell command and return stdout/stderr. Use with caution.",
    "memory_view": "View a memory file or list a memory directory. Paths: memories/user/..., memories/session/..., memories/repo/...",
    "memory_create": "Create a new memory file. Fails if it already exists.",
    "memory_update": "Replace an exact string in a memory file (must appear exactly once).",
    "memory_delete": "Delete a memory file or directory.",
}


class ToolRegistry:
    """Central registry for all tools available to the agent."""

    def __init__(
        self,
        memory: MemoryManager,
        mcp: MCPManager | None = None,
        working_dir: str | None = None,
        tools_config: ToolsConfig | None = None,
    ) -> None:
        self.memory = memory
        self.mcp = mcp
        self.working_dir = working_dir or os.getcwd()
        self.tools_config = tools_config or ToolsConfig()

        # name → (handler, openai_tool_definition)
        self._tools: dict[str, tuple[ToolHandler, dict[str, Any]]] = {}
        self._register_builtins()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, name: str, handler: ToolHandler, description: str, parameters: dict) -> None:
        self._tools[name] = (
            handler,
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            },
        )

    # ------------------------------------------------------------------
    # OpenAI format export
    # ------------------------------------------------------------------

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """Return all tools (built-in + MCP) in OpenAI function-calling format."""
        tools = [defn for _, defn in self._tools.values()]
        if self.mcp:
            tools.extend(self.mcp.get_all_tools_openai())
        return tools

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call a tool by name. Routes to built-in or MCP."""
        # Built-in first
        entry = self._tools.get(tool_name)
        if entry:
            handler, _ = entry
            try:
                return await handler(arguments)
            except Exception as exc:
                return json.dumps({"error": str(exc)})

        # MCP fallback
        if self.mcp and self.mcp.is_mcp_tool(tool_name):
            return await self.mcp.call_tool(tool_name, arguments)

        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    # ------------------------------------------------------------------
    # Built-in tools
    # ------------------------------------------------------------------

    def _desc(self, name: str) -> str:
        """Return description: config override if set, else default."""
        cfg = getattr(self.tools_config, name, None)
        if cfg and cfg.description:
            return cfg.description
        return _DEFAULT_DESCRIPTIONS.get(name, "")

    def _register_builtins(self) -> None:
        tc = self.tools_config

        # --- read_file ---
        if tc.read_file.enabled:
            self.register(
                "read_file",
                self._read_file,
                self._desc("read_file"),
                _schema(
                    {
                        "filePath": {"type": "string", "description": "Absolute or relative path to the file."},
                        "startLine": {"type": "integer", "description": "1-based start line (optional)."},
                        "endLine": {"type": "integer", "description": "1-based end line inclusive (optional)."},
                    },
                    required=["filePath"],
                ),
            )

        # --- write_file ---
        if tc.write_file.enabled:
            self.register(
                "write_file",
                self._write_file,
                self._desc("write_file"),
                _schema(
                    {
                        "filePath": {"type": "string", "description": "Path to the file."},
                        "content": {"type": "string", "description": "Content to write."},
                    },
                    required=["filePath", "content"],
                ),
            )

        # --- list_dir ---
        if tc.list_dir.enabled:
            self.register(
                "list_dir",
                self._list_dir,
                self._desc("list_dir"),
                _schema(
                    {"path": {"type": "string", "description": "Directory path."}},
                    required=["path"],
                ),
            )

        # --- grep_search ---
        if tc.grep_search.enabled:
            self.register(
                "grep_search",
                self._grep_search,
                self._desc("grep_search"),
                _schema(
                    {
                        "pattern": {"type": "string", "description": "Regex pattern to search for."},
                        "path": {"type": "string", "description": "Directory to search in (default: working dir)."},
                        "includePattern": {"type": "string", "description": "Glob filter for files (e.g. '*.py')."},
                    },
                    required=["pattern"],
                ),
            )

        # --- run_command ---
        if tc.run_command.enabled:
            self.register(
                "run_command",
                self._run_command,
                self._desc("run_command"),
                _schema(
                    {
                        "command": {"type": "string", "description": "The shell command to run."},
                        "cwd": {"type": "string", "description": "Working directory (optional)."},
                    },
                    required=["command"],
                ),
            )

        # --- memory tools ---
        if tc.memory_view.enabled:
            self.register(
                "memory_view",
                self._memory_view,
                self._desc("memory_view"),
                _schema(
                    {
                        "path": {"type": "string", "description": "Virtual memory path."},
                        "startLine": {"type": "integer", "description": "Optional start line (1-based)."},
                        "endLine": {"type": "integer", "description": "Optional end line (1-based)."},
                    },
                    required=["path"],
                ),
            )

        if tc.memory_create.enabled:
            self.register(
                "memory_create",
                self._memory_create,
                self._desc("memory_create"),
                _schema(
                    {
                        "path": {"type": "string", "description": "Virtual memory path."},
                        "content": {"type": "string", "description": "File content."},
                    },
                    required=["path", "content"],
                ),
            )

        if tc.memory_update.enabled:
            self.register(
                "memory_update",
                self._memory_update,
                self._desc("memory_update"),
                _schema(
                    {
                        "path": {"type": "string", "description": "Virtual memory path."},
                        "oldStr": {"type": "string", "description": "Exact string to find."},
                        "newStr": {"type": "string", "description": "Replacement string."},
                    },
                    required=["path", "oldStr", "newStr"],
                ),
            )

        if tc.memory_delete.enabled:
            self.register(
                "memory_delete",
                self._memory_delete,
                self._desc("memory_delete"),
                _schema(
                    {"path": {"type": "string", "description": "Virtual memory path."}},
                    required=["path"],
                ),
            )

    # ------------------------------------------------------------------
    # Handler implementations
    # ------------------------------------------------------------------

    async def _read_file(self, args: dict[str, Any]) -> str:
        p = Path(args["filePath"])
        if not p.is_absolute():
            p = Path(self.working_dir) / p
        if not p.exists():
            return f"Error: file not found: {p}"
        max_bytes = self.tools_config.read_file.max_size_kb * 1024
        if p.stat().st_size > max_bytes:
            return f"Error: file too large ({p.stat().st_size} bytes, limit {max_bytes} bytes). Use startLine/endLine."
        text = p.read_text(encoding="utf-8", errors="replace")
        start = args.get("startLine")
        end = args.get("endLine")
        if start or end:
            lines = text.splitlines(keepends=True)
            s = (start or 1) - 1
            e = end or len(lines)
            return "".join(lines[s:e])
        return text

    async def _write_file(self, args: dict[str, Any]) -> str:
        p = Path(args["filePath"])
        if not p.is_absolute():
            p = Path(self.working_dir) / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(args["content"], encoding="utf-8")
        return f"Written: {p}"

    async def _list_dir(self, args: dict[str, Any]) -> str:
        p = Path(args["path"])
        if not p.is_absolute():
            p = Path(self.working_dir) / p
        if not p.is_dir():
            return f"Error: not a directory: {p}"
        entries = sorted(p.iterdir())
        lines = [e.name + ("/" if e.is_dir() else "") for e in entries]
        return "\n".join(lines) if lines else "(empty)"

    async def _grep_search(self, args: dict[str, Any]) -> str:
        search_dir = Path(args.get("path", self.working_dir))
        if not search_dir.is_absolute():
            search_dir = Path(self.working_dir) / search_dir
        pattern = re.compile(args["pattern"], re.IGNORECASE)
        glob_filter = args.get("includePattern", "*")
        max_results = self.tools_config.grep_search.max_results

        results: list[str] = []
        for fpath in search_dir.rglob(glob_filter):
            if not fpath.is_file():
                continue
            try:
                for i, line in enumerate(fpath.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if pattern.search(line):
                        results.append(f"{fpath}:{i}: {line.strip()}")
            except Exception:
                continue
            if len(results) >= max_results:
                break

        return "\n".join(results) if results else "(no matches)"

    async def _run_command(self, args: dict[str, Any]) -> str:
        cwd = args.get("cwd", self.working_dir)
        timeout = self.tools_config.run_command.timeout
        try:
            proc = await asyncio.create_subprocess_shell(
                args["command"],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out = stdout.decode(errors="replace")
            err = stderr.decode(errors="replace")
            result = f"Exit code: {proc.returncode}\n"
            if out:
                result += f"STDOUT:\n{out}\n"
            if err:
                result += f"STDERR:\n{err}\n"
            return result.strip()
        except asyncio.TimeoutError:
            return f"Error: command timed out after {timeout}s"
        except Exception as exc:
            return f"Error: {exc}"

    async def _memory_view(self, args: dict[str, Any]) -> str:
        return self.memory.view(args["path"], args.get("startLine"), args.get("endLine"))

    async def _memory_create(self, args: dict[str, Any]) -> str:
        return self.memory.create(args["path"], args["content"])

    async def _memory_update(self, args: dict[str, Any]) -> str:
        return self.memory.str_replace(args["path"], args["oldStr"], args["newStr"])

    async def _memory_delete(self, args: dict[str, Any]) -> str:
        return self.memory.delete(args["path"])
