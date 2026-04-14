"""MCP Client Manager — lifecycle, tool discovery, and tool dispatch for MCP servers."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from mcpagent.config import McpServerConfig

log = logging.getLogger(__name__)


@dataclass
class ServerConnection:
    """Holds a live MCP session and its metadata."""

    name: str
    config: McpServerConfig
    session: ClientSession
    tools: list[dict[str, Any]] = field(default_factory=list)


class MCPManager:
    """Manages connections to all MCP servers defined in config.

    Usage::

        async with MCPManager(servers_config) as mgr:
            tools = mgr.get_all_tools_openai()
            result = await mgr.call_tool("server__tool_name", {"arg": "val"})
    """

    def __init__(self, servers: dict[str, McpServerConfig]) -> None:
        self._server_configs = servers
        self._connections: dict[str, ServerConnection] = {}
        self._exit_stack = AsyncExitStack()
        # Mapping from qualified tool name → (server_name, original_tool_name)
        self._tool_map: dict[str, tuple[str, str]] = {}

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> MCPManager:
        await self._exit_stack.__aenter__()
        await self.start_all()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._exit_stack.__aexit__(*exc)
        self._connections.clear()
        self._tool_map.clear()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_all(self) -> None:
        """Connect to every server defined in config (in parallel)."""

        async def _safe_start(name: str, cfg: McpServerConfig) -> None:
            try:
                await self._start_server(name, cfg)
                log.info("MCP server '%s' connected (%d tools)", name, len(self._connections[name].tools))
            except Exception:
                log.exception("Failed to start MCP server '%s'", name)

        await asyncio.gather(*[
            _safe_start(name, cfg)
            for name, cfg in self._server_configs.items()
        ])

    async def _start_server(self, name: str, cfg: McpServerConfig) -> None:
        if cfg.type == "stdio":
            if not cfg.command:
                raise ValueError(f"MCP server '{name}': stdio type requires 'command'")

            params = StdioServerParameters(
                command=cfg.command,
                args=cfg.args,
                env={**cfg.env} if cfg.env else None,
            )
            transport = await self._exit_stack.enter_async_context(stdio_client(params))

        elif cfg.type in ("http", "sse", "streamable-http"):
            if not cfg.url:
                raise ValueError(f"MCP server '{name}': http type requires 'url'")
            transport = await self._exit_stack.enter_async_context(
                streamable_http_client(cfg.url)
            )
        else:
            raise ValueError(f"Unknown MCP transport type: {cfg.type}")

        # transport is (read_stream, write_stream) or (read, write, session_getter)
        if len(transport) == 3:
            read, write, _ = transport
        else:
            read, write = transport

        session: ClientSession = await self._exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await session.initialize()

        # Discover tools
        tools_response = await session.list_tools()
        raw_tools = tools_response.tools if hasattr(tools_response, "tools") else tools_response

        conn = ServerConnection(name=name, config=cfg, session=session)

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

    # ------------------------------------------------------------------
    # Tool discovery
    # ------------------------------------------------------------------

    def get_all_tools_openai(self) -> list[dict[str, Any]]:
        """Return all MCP tools in OpenAI function-calling format."""
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
        return list(self._connections.keys())

    def get_server_tool_count(self, name: str) -> int:
        conn = self._connections.get(name)
        return len(conn.tools) if conn else 0

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def call_tool(self, qualified_name: str, arguments: dict[str, Any]) -> str:
        """Dispatch a tool call to the appropriate MCP server."""
        mapping = self._tool_map.get(qualified_name)
        if not mapping:
            return json.dumps({"error": f"Unknown tool: {qualified_name}"})

        server_name, tool_name = mapping
        conn = self._connections.get(server_name)
        if not conn:
            return json.dumps({"error": f"Server '{server_name}' not connected"})

        try:
            result = await conn.session.call_tool(tool_name, arguments=arguments)
        except Exception as exc:
            log.exception("Tool call %s failed", qualified_name)
            return json.dumps({"error": str(exc)})

        # Flatten MCP result to string
        if hasattr(result, "content"):
            parts = []
            for item in result.content:
                if hasattr(item, "text"):
                    parts.append(item.text)
                else:
                    parts.append(str(item))
            return "\n".join(parts) if parts else "(empty result)"

        return str(result)

    def is_mcp_tool(self, name: str) -> bool:
        """Check if a tool name belongs to an MCP server."""
        return name in self._tool_map
