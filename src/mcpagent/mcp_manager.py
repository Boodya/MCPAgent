"""MCP Client Manager — lifecycle, tool discovery, and tool dispatch for MCP servers.

Supports per-agent selective start/stop with connection pooling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from mcpagent.config import McpServerConfig
from mcpagent.ops_log import OpsLog

log = logging.getLogger(__name__)


@dataclass
class ServerConnection:
    """Holds a live MCP session and its metadata."""

    name: str
    config: McpServerConfig
    session: ClientSession
    exit_stack: AsyncExitStack  # per-server stack for independent lifecycle
    tools: list[dict[str, Any]] = field(default_factory=list)


class MCPManager:
    """Manages connections to MCP servers with selective start/stop.

    Supports per-agent server sets: start only what the agent needs,
    stop what it doesn't, keep shared servers running.

    Usage::

        mgr = MCPManager(servers_config)
        await mgr.ensure_servers(["server-a", "server-b"])  # start needed
        await mgr.ensure_servers(["server-b"])               # stops server-a, keeps b
        await mgr.shutdown()                                  # stop all
    """

    def __init__(self, servers: dict[str, McpServerConfig], *, ops: OpsLog | None = None) -> None:
        self._server_configs = servers
        self._connections: dict[str, ServerConnection] = {}
        # Mapping from qualified tool name → (server_name, original_tool_name)
        self._tool_map: dict[str, tuple[str, str]] = {}
        self.ops = ops or OpsLog(None)

    # ------------------------------------------------------------------
    # Lifecycle — selective start/stop
    # ------------------------------------------------------------------

    async def ensure_servers(self, desired: list[str] | None) -> tuple[list[str], list[str]]:
        """Bring running servers in sync with *desired* list.

        Args:
            desired: server names to have running.
                     None means ALL servers from config.

        Returns:
            (started, stopped) — names of servers that changed state.
        """
        if desired is None:
            desired_set = set(self._server_configs.keys())
        else:
            # Only keep names that exist in the config registry
            desired_set = {n for n in desired if n in self._server_configs}

        running = set(self._connections.keys())

        to_start = desired_set - running
        to_stop = running - desired_set

        stopped: list[str] = []
        if to_stop:
            stopped = await self._stop_servers(list(to_stop))

        started: list[str] = []
        if to_start:
            started = await self._start_servers(list(to_start))

        return started, stopped

    async def _start_servers(self, names: list[str]) -> list[str]:
        """Start specific servers in parallel. Returns names that started successfully."""
        started: list[str] = []
        lock = asyncio.Lock()

        async def _safe_start(name: str) -> None:
            cfg = self._server_configs.get(name)
            if not cfg:
                log.warning("MCP server '%s' not found in config, skipping", name)
                return
            try:
                timeout = cfg.startup_timeout
                await asyncio.wait_for(self._start_server(name, cfg), timeout=timeout)
                async with lock:
                    started.append(name)
                log.info("MCP server '%s' connected (%d tools)", name, len(self._connections[name].tools))
            except asyncio.TimeoutError:
                log.error(
                    "MCP server '%s' failed to start within %ds (startup_timeout). "
                    "Check that the command exists and any required services are running.",
                    name, cfg.startup_timeout,
                )
            except Exception:
                log.exception("Failed to start MCP server '%s'", name)

        await asyncio.gather(*[_safe_start(n) for n in names])
        return started

    async def _stop_servers(self, names: list[str]) -> list[str]:
        """Stop specific servers. Returns names that were stopped."""
        stopped: list[str] = []
        for name in names:
            conn = self._connections.pop(name, None)
            if not conn:
                continue
            # Remove tools from tool_map
            to_remove = [qn for qn, (sn, _) in self._tool_map.items() if sn == name]
            for qn in to_remove:
                del self._tool_map[qn]
            # Tear down server's exit stack (with timeout to avoid hanging)
            try:
                await asyncio.wait_for(
                    conn.exit_stack.__aexit__(None, None, None),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                log.warning("MCP server '%s' shutdown timed out, forcing", name)
            except (RuntimeError, BaseExceptionGroup, Exception):
                pass  # MCP/anyio shutdown quirks
            stopped.append(name)
            log.info("MCP server '%s' stopped", name)
        return stopped

    async def start_all(self) -> None:
        """Connect to every server in config (in parallel). Convenience for startup."""
        await self.ensure_servers(None)

    async def shutdown(self) -> None:
        """Stop all running servers."""
        names = list(self._connections.keys())
        if names:
            await self._stop_servers(names)

    async def _start_server(self, name: str, cfg: McpServerConfig) -> None:
        stack = AsyncExitStack()
        await stack.__aenter__()

        try:
            if cfg.type == "stdio":
                if not cfg.command:
                    raise ValueError(f"MCP server '{name}': stdio type requires 'command'")

                # Always inherit the parent process environment so that
                # PATH and other required vars are available to the subprocess.
                # Server-specific env vars are merged on top.
                merged_env = {**os.environ, **cfg.env} if cfg.env else None
                params = StdioServerParameters(
                    command=cfg.command,
                    args=cfg.args,
                    env=merged_env,
                )
                transport = await stack.enter_async_context(stdio_client(params))

            elif cfg.type in ("http", "sse", "streamable-http"):
                if not cfg.url:
                    raise ValueError(f"MCP server '{name}': http type requires 'url'")
                transport = await stack.enter_async_context(
                    streamable_http_client(cfg.url)
                )
            else:
                raise ValueError(f"Unknown MCP transport type: {cfg.type}")

            # transport is (read_stream, write_stream) or (read, write, session_getter)
            if len(transport) == 3:
                read, write, _ = transport
            else:
                read, write = transport

            session: ClientSession = await stack.enter_async_context(
                ClientSession(read, write)
            )
            await session.initialize()

            # Discover tools
            tools_response = await session.list_tools()
            raw_tools = tools_response.tools if hasattr(tools_response, "tools") else tools_response

            conn = ServerConnection(name=name, config=cfg, session=session, exit_stack=stack)

            for tool in raw_tools:
                tool_name = tool.name
                qualified = f"{name}__{tool_name}"
                self._tool_map[qualified] = (name, tool_name)
                conn.tools.append({
                    "qualified_name": qualified,
                    "name": tool_name,
                    "description": getattr(tool, "description", "") or "",
                    "input_schema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
                })

            self._connections[name] = conn

        except Exception:
            # Clean up the stack if server failed to start
            try:
                await stack.__aexit__(None, None, None)
            except Exception:
                pass
            raise

    # ------------------------------------------------------------------
    # Tool discovery
    # ------------------------------------------------------------------

    def get_all_tools_openai(self) -> list[dict[str, Any]]:
        """Return all MCP tools from currently connected servers in OpenAI format."""
        tools: list[dict[str, Any]] = []
        for conn in self._connections.values():
            for t in conn.tools:
                schema = dict(t["input_schema"]) if t["input_schema"] else {}
                schema.pop("additionalProperties", None)
                tools.append({
                    "type": "function",
                    "function": {
                        "name": t["qualified_name"],
                        "description": f"[MCP:{conn.name}] {t['description']}",
                        "parameters": schema or {"type": "object", "properties": {}},
                    },
                })
        return tools

    def get_server_names(self) -> list[str]:
        """Return names of currently connected servers."""
        return list(self._connections.keys())

    def get_available_server_names(self) -> list[str]:
        """Return names of all configured servers (whether connected or not)."""
        return list(self._server_configs.keys())

    async def reload_config(self, servers: dict[str, McpServerConfig]) -> tuple[list[str], list[str]]:
        """Update server configs from a freshly-loaded mcp.json.

        - New servers become available (not started until requested).
        - Removed servers are stopped if running.
        - Existing server configs are updated (takes effect on next connect).

        Returns (added, removed) server name lists.
        """
        old_names = set(self._server_configs.keys())
        new_names = set(servers.keys())

        added = sorted(new_names - old_names)
        removed = sorted(old_names - new_names)

        # Stop removed servers that are currently running
        running_removed = [n for n in removed if n in self._connections]
        if running_removed:
            await self._stop_servers(running_removed)

        # Update config
        self._server_configs = servers

        return added, removed

    def get_server_tool_count(self, name: str) -> int:
        conn = self._connections.get(name)
        return len(conn.tools) if conn else 0

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def call_tool(self, qualified_name: str, arguments: dict[str, Any]) -> str:
        """Dispatch a tool call to the appropriate MCP server."""
        import time as _time

        mapping = self._tool_map.get(qualified_name)
        if not mapping:
            return json.dumps({"error": f"Unknown tool: {qualified_name}"})

        server_name, tool_name = mapping
        conn = self._connections.get(server_name)
        if not conn:
            return json.dumps({"error": f"Server '{server_name}' not connected"})

        t0 = _time.perf_counter()
        try:
            result = await conn.session.call_tool(tool_name, arguments=arguments)
        except Exception as exc:
            elapsed_ms = (_time.perf_counter() - t0) * 1000
            log.exception("Tool call %s failed", qualified_name)
            self.ops.tool_result(
                tool=qualified_name,
                error=str(exc),
                duration_ms=round(elapsed_ms, 1),
            )
            return json.dumps({"error": str(exc)})

        elapsed_ms = (_time.perf_counter() - t0) * 1000

        # Flatten MCP result to string
        if hasattr(result, "content"):
            parts = []
            for item in result.content:
                if hasattr(item, "text"):
                    parts.append(item.text)
                else:
                    parts.append(str(item))
            text = "\n".join(parts) if parts else "(empty result)"
        else:
            text = str(result)

        self.ops.tool_result(
            tool=qualified_name,
            result_length=len(text),
            duration_ms=round(elapsed_ms, 1),
        )
        return text

    def is_mcp_tool(self, name: str) -> bool:
        """Check if a tool name belongs to an MCP server."""
        return name in self._tool_map
