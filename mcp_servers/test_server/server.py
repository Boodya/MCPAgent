"""Built-in test MCP server for MCPAgent (stdio transport).

Provides a handful of simple tools for testing the agent ↔ MCP integration:
  • echo       — returns exactly what you send
  • add        — sums two numbers
  • timestamp  — current UTC time
  • random     — random int in range
  • sys_info   — basic platform info
"""

from __future__ import annotations

import platform
import random
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("test-server", instructions="Built-in test/demo MCP server for MCPAgent.")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the input text back. Useful for connectivity checks."""
    return text


@mcp.tool()
def add(a: float, b: float) -> float:
    """Add two numbers and return the sum."""
    return a + b


@mcp.tool()
def timestamp() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


@mcp.tool()
def random_number(min_val: int = 1, max_val: int = 100) -> int:
    """Return a random integer between min_val and max_val (inclusive)."""
    return random.randint(min_val, max_val)


@mcp.tool()
def sys_info() -> dict:
    """Return basic system information: OS, platform, Python version."""
    return {
        "system": platform.system(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
