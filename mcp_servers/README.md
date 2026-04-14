# Built-in MCP Servers

This directory contains MCP servers that ship with MCPAgent.

Each sub-folder is a standalone server with its own `server.py`.

## test_server

A minimal test/demo server (stdio transport) with 5 tools:

| Tool | Description |
|------|-------------|
| `echo` | Returns whatever you send |
| `add` | Sums two numbers |
| `timestamp` | Current UTC time (ISO-8601) |
| `random_number` | Random int in range |
| `sys_info` | OS / platform / Python version |

### Run standalone

```bash
python mcp_servers/test_server/server.py
```

### Use via mcp.json

Already configured in `config/mcp.json` as `"test-server"` (stdio).
