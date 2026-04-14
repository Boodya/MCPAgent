# MCPAgent

Universal AI agent with MCP tool integration. Replicates key VS Code Copilot capabilities as a standalone CLI program — with a built-in workflow engine and job scheduler.

## Features

### Core Agent
- **Azure OpenAI** — streaming chat completions with tool calling
- **MCP Integration** — connects to MCP servers via `mcp.json` (VS Code compatible format)
- **ReAct Agent Loop** — plan → execute tools → observe → iterate
- **Built-in Tools** — `read_file`, `write_file`, `list_dir`, `grep_search`, `run_command`, memory CRUD
- **Configurable Tools** — each built-in tool can be toggled on/off with custom settings (timeout, max results, file size limits)
- **Agent Presets** — named agent profiles (`agents/*.md`) with custom system prompts and MCP server subsets
- **Skills** — injectable domain-specific instructions (`skills/*/SKILL.md`) loaded into agent context
- **Memory System** — three-scoped markdown memory (user / session / repo), all under one configurable directory
- **Rich CLI** — streaming output, tool call display, slash commands

### Workflow Engine
- **YAML Workflow Definitions** — declarative DAG-based workflows in `workflows/*.yaml`
- **DAG Execution** — steps run in topological order; independent steps execute in parallel via `asyncio.gather`
- **Variable Interpolation** — `{{vars.topic}}` and `{{steps.gather.result}}` templates in prompts
- **Conditional Branching** — per-step `condition` expressions evaluated in a safe AST sandbox
- **Retry & Timeout** — configurable per step (`retry.count`, `retry.delay`, `timeout`)
- **Failure Handling** — `on_failure: stop | continue` controls whether downstream steps are skipped

### Job Scheduler
- **Built-in Daemon** — `mcpagent scheduler start` runs an asyncio-based scheduler process
- **Cron & Interval Triggers** — standard 5-field cron expressions or fixed interval (seconds)
- **SQLite State Store** — all workflow runs and step results persisted in `mcpagent.db`
- **Headless Execution** — `mcpagent run` for one-shot agent tasks without interactive CLI
- **Job Management CLI** — `mcpagent job list|run|history|status` for workflow operations

### Storage & Observability
- **Chat History** — conversations saved as JSON for review and continuity
- **Execution Logs** — structured JSONL logs of all tool calls per session
- **Run History** — SQLite records for every workflow run with per-step status, prompts, and results
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
# Interactive chat (default)
mcpagent

# One-shot headless run
mcpagent run --agent default --message "Summarize the project README"

# List available workflows
mcpagent job list

# Run a workflow manually
mcpagent job run example-research

# Start the scheduler daemon
mcpagent scheduler start
```

## CLI Subcommands

| Subcommand | Description |
|---|---|
| `mcpagent` / `mcpagent chat` | Interactive chat session (default) |
| `mcpagent run -a AGENT -m MSG` | Run agent headlessly with a single message |
| `mcpagent job list` | List available workflows |
| `mcpagent job run NAME` | Run a workflow by name (manual trigger) |
| `mcpagent job history [NAME]` | Show run history (optionally filtered) |
| `mcpagent job status RUN_ID` | Show status of a specific run |
| `mcpagent scheduler start` | Start the scheduler daemon |
| `mcpagent scheduler status` | Show scheduled workflows and next run times |

### Interactive Slash Commands

| Command    | Description                     |
|------------|---------------------------------|
| `/help`    | Show available commands         |
| `/tools`   | List all available tools        |
| `/servers` | Show MCP server status          |
| `/memory`  | Show user memory contents       |
| `/clear`   | Clear conversation history      |
| `/exit`    | Exit the agent                  |

## Workflows

Workflows are YAML files in the `workflows/` directory. Each workflow defines a DAG of steps where each step runs an agent with a rendered prompt.

### Example: `workflows/example-research.yaml`

```yaml
name: example-research
description: "Web research with analysis and bilingual summary"
schedule: "0 9 * * *"   # daily at 09:00 UTC
enabled: true

vars:
  topic: "AI agents frameworks 2026"
  output_dir: "reports"

steps:
  - id: gather
    agent: web-researcher
    prompt: "Search the web for: {{vars.topic}}"
    timeout: 300

  - id: analyze
    agent: default
    prompt: "Analyze these results: {{steps.gather.result}}"
    depends_on: [gather]

  - id: summary-ru
    agent: default
    prompt: "Write a summary IN RUSSIAN: {{steps.analyze.result}}"
    depends_on: [analyze]

  - id: summary-en
    agent: default
    prompt: "Write a summary IN ENGLISH: {{steps.analyze.result}}"
    depends_on: [analyze]

  - id: save-report
    agent: default
    prompt: "Save report to {{vars.output_dir}}/report.md ..."
    depends_on: [summary-ru, summary-en]
```

This creates the following DAG:

```
gather → analyze → ┬─ summary-ru ─┬─ save-report
                   └─ summary-en ─┘
```

Steps `summary-ru` and `summary-en` run **in parallel** since they share the same dependency level.

### Step Options

| Field | Type | Default | Description |
|---|---|---|---|
| `id` | string | required | Unique step identifier |
| `agent` | string | `"default"` | Agent preset to use |
| `prompt` | string | required | Prompt template (supports `{{vars.*}}` and `{{steps.*.result}}`) |
| `depends_on` | list | `[]` | Step IDs that must complete first |
| `condition` | string | `null` | Python expression — step runs only if truthy |
| `timeout` | int | `600` | Max seconds before step is killed |
| `retry.count` | int | `0` | Number of retry attempts |
| `retry.delay` | float | `5.0` | Seconds between retries |
| `on_failure` | string | `"stop"` | `"stop"` skips dependents; `"continue"` ignores failure |

### Workflow-Level Options

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Workflow identifier |
| `schedule` | string | `null` | Cron expression (5-field: `min hour dom month dow`) |
| `interval` | int | `null` | Run every N seconds (alternative to cron) |
| `enabled` | bool | `true` | Whether the scheduler picks up this workflow |
| `vars` | dict | `{}` | Variables accessible as `{{vars.KEY}}` in step prompts |

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

default_model: default
default_agent: default

agent:
  max_iterations: 30

storage:
  data_dir: ".mcpagent"
  chat_history: true
  logs: true

workflows_dir: "workflows"      # directory with workflow YAML files

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

## Architecture

```
                        ┌──────────────────────────────┐
                        │      CLI Subcommands         │
                        │  chat │ run │ job │ scheduler │
                        └──────┬───────────────────────┘
                               │
          ┌────────────────────┼────────────────────┐
          ▼                    ▼                     ▼
   Interactive CLI      Headless Factory      Scheduler Daemon
   (Rich REPL)         (create_agent)        (asyncio + cron)
          │                    │                     │
          └────────────┬───────┘                     │
                       ▼                             │
               Agent (ReAct loop)                    │
                       │                             │
               Tool Registry                  Workflow Engine
               ├─ Built-in tools              ├─ DAG resolver
               └─ MCP tools                   ├─ Parallel executor
                  ├─ stdio servers             └─ Condition evaluator
                  └─ HTTP servers                    │
                                                     ▼
                                              SQLite Job Store
                                              (runs + step_runs)
```

## Storage Layout

All persistent data lives under a single configurable `data_dir`:

```
<data_dir>/
├── mcpagent.db               # SQLite — workflow runs & step results
├── history/                   # Chat history (one JSON per session)
│   └── chat_20260414_145618.json
├── logs/                      # Execution logs (one JSONL per session)
│   └── session_20260414_145618.jsonl
└── memories/
    ├── user/                  # Persistent across all sessions
    ├── session/               # Current conversation only
    └── repo/                  # Per-project notes
```

Memory is exposed as tools (`memory_view`, `memory_create`, `memory_update`, `memory_delete`) so the agent can read/write notes during conversations.

## Roadmap

- [ ] **Phase 2**: Skills (SKILL.md) & Agent presets
- [ ] **Phase 3**: Sub-agent orchestration, Textual TUI, @references
