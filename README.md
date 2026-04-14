# MCPAgent

Universal AI agent with MCP tool integration. Replicates key VS Code Copilot capabilities as a standalone CLI program.

## Features

- **Azure OpenAI** — streaming chat completions with tool calling
- **MCP Integration** — connects to MCP servers via `mcp.json` (VS Code compatible format)
- **ReAct Agent Loop** — plan → execute tools → observe → iterate
- **Built-in Tools** — `read_file`, `write_file`, `list_dir`, `grep_search`, `run_command`, memory CRUD
- **Configurable Tools** — each built-in tool can be toggled on/off with custom settings (timeout, max results, file size limits)
- **Memory System** — three-scoped markdown memory (user / session / repo), all under one configurable directory
- **Chat History** — conversations saved as JSON for review and continuity
- **Execution Logs** — structured JSONL logs of all tool calls per session
- **Rich CLI** — streaming output, tool call display, slash commands
- **Env-Driven Config** — Azure credentials and model settings from `.env`, with YAML fallbacks

## Quick Start

### 1. Install

```bash
cd MCPAgent
pip install -e .
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — set AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT, etc.
# Edit config/config.yaml — tune agent, storage, and tool settings
# Edit config/mcp.json — add your MCP servers (VS Code format)
```

### 3. Run

```bash
mcpagent
# or
python -m mcpagent
```

## Configuration

### .env

All Azure OpenAI settings are read from environment variables (`.env` file):

```env
AZURE_OPENAI_API_KEY=your-key-here
AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-4o
AZURE_OPENAI_API_VERSION=2025-01-01-preview
```

### config/config.yaml

```yaml
models:
  default:
    provider: azure
    # All model settings override from .env — see above

default_model: default

agent:
  max_iterations: 30

storage:
  data_dir: ".mcpagent"       # all data stored here: memories, chat history, logs
  chat_history: true           # save conversations to data_dir/history/
  logs: true                   # save execution logs to data_dir/logs/

tools:
  read_file:
    enabled: true
    max_size_kb: 512
  grep_search:
    enabled: true
    max_results: 200
  run_command:
    enabled: true
    timeout: 60
    confirm: false
  # ... see config.yaml for all options
```

### config/mcp.json

Uses the same format as VS Code `mcp.json`. Supports `stdio` and `http` transport types. Environment variable placeholders (`${input:NAME}`, `${env:NAME}`) are resolved from `.env` or environment variables.

```json
{
  "servers": {
    "my-server": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "some-mcp-server"],
      "env": { "API_KEY": "${env:MY_API_KEY}" }
    }
  }
}
```

## CLI Commands

| Command    | Description                     |
|------------|---------------------------------|
| `/help`    | Show available commands         |
| `/tools`   | List all available tools        |
| `/servers` | Show MCP server status          |
| `/memory`  | Show user memory contents       |
| `/clear`   | Clear conversation history      |
| `/exit`    | Exit the agent                  |

## Architecture

```
CLI (Rich) → Agent (ReAct loop) → LLM (Azure OpenAI)
                ↓
        Tool Registry
        ├── Built-in tools (file, shell, memory)
        └── MCP tools (via MCP Client Manager)
                ├── stdio servers
                └── HTTP servers
                ↓
        Storage Manager
        ├── Chat history (JSON)
        └── Execution logs (JSONL)
```

## Storage Layout

All persistent data lives under a single configurable `data_dir`:

```
<data_dir>/
├── history/              # Chat history (one JSON file per session)
│   └── chat_20260414_145618.json
├── logs/                 # Execution logs (one JSONL file per session)
│   └── session_20260414_145618.jsonl
└── memories/
    ├── user/             # Persistent across all sessions
    ├── session/          # Current conversation only (cleared on exit)
    └── repo/             # Per-project notes
```

Memory is exposed as tools (`memory_view`, `memory_create`, `memory_update`, `memory_delete`) so the agent can read/write notes during conversations.

## Roadmap

- [ ] **Phase 2**: Skills (SKILL.md) & Agent presets
- [ ] **Phase 3**: Sub-agent orchestration, Textual TUI, @references
