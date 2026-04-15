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
# Edit .env — set AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT
# Set MCPAGENT_APP_DIR to your project folder (see "Application Directory" below)
```

### 3. Run

```bash
# Interactive chat (default — uses MCPAGENT_APP_DIR from .env)
mcpagent

# Same, but pointing at a specific project folder
mcpagent -d .example-project

# One-shot headless run
mcpagent run -a default -m "Summarize the project README"

# List available workflows
mcpagent job list

# Run a workflow manually
mcpagent job run example-research

# Start the scheduler daemon
mcpagent scheduler start
```

## Application Directory

MCPAgent uses one environment variable — `MCPAGENT_APP_DIR` — to point at a self-contained project folder. All configuration, assets, and runtime data live inside it:

```
MCPAGENT_APP_DIR/
├── config.yaml          # app settings (model, agent defaults, tool config)
├── mcp.json             # MCP server definitions (VS Code compatible)
├── agents/              # agent preset .md files
├── skills/              # skill folders with SKILL.md
├── workflows/           # workflow YAML definitions
├── memories/            # agent memory (user / session / repo)
│   ├── user/
│   ├── session/
│   └── repo/
├── history/             # saved chat sessions (JSON)
├── logs/                # execution logs (JSONL)
├── ops/                 # ops log
└── mcpagent.db          # SQLite — workflow runs & step results
```

### How it works

Set the variable in `.env` (or export it):

```env
MCPAGENT_APP_DIR=.local-assistants
```

When `MCPAGENT_APP_DIR` is set:
- `config.yaml` and `mcp.json` are read from this directory
- All relative paths in `config.yaml` (`skills_dir`, `agents_dir`, `workflows_dir`, `storage.data_dir`) resolve from this directory

This means you can have **multiple isolated project setups** side by side — just point `MCPAGENT_APP_DIR` at a different folder:

```
project-root/
├── .env                        # MCPAGENT_APP_DIR=.local-assistants
├── .local-assistants/          # ← your main setup
│   ├── config.yaml
│   ├── mcp.json
│   ├── agents/
│   ├── skills/
│   └── workflows/
├── .experiment/                # ← alternative setup
│   ├── config.yaml
│   ├── mcp.json
│   ├── agents/
│   └── skills/
└── src/mcpagent/               # source code (never touched)
```

Switch between them by changing one line in `.env` — or use the `--app-dir` / `-d` CLI flag:

```bash
mcpagent -d .local-assistants       # uses .local-assistants/
mcpagent -d .experiment             # uses .experiment/
mcpagent                            # uses MCPAGENT_APP_DIR from .env
```

### Legacy mode (without `MCPAGENT_APP_DIR`)

If `MCPAGENT_APP_DIR` is not set, the app falls back to the original layout:

| Variable | Default | Description |
|---|---|---|
| `MCPAGENT_CONFIG_DIR` | `config/` | Directory containing `config.yaml` and `mcp.json` |

Relative paths in `config.yaml` resolve from the **parent** of `config_dir` (i.e. the project root). This keeps backward compatibility with the original `config/` folder layout.

## CLI Reference

### Global Options

Every subcommand supports the global `--app-dir` flag:

```
mcpagent [--app-dir DIR] <command> [args]
```

| Flag | Short | Default | Description |
|---|---|---|---|
| `--app-dir` | `-d` | `$MCPAGENT_APP_DIR` from `.env` | Application directory — overrides the `MCPAGENT_APP_DIR` environment variable for this run |

This lets you switch between independent project setups without editing `.env`:

```bash
mcpagent -d .example-project          # interactive chat with .example-project/
mcpagent -d .local-assistants chat    # same, explicit chat subcommand
mcpagent -d /abs/path/to/setup run -m "Hello"
```

Without `-d`, the value of `MCPAGENT_APP_DIR` from `.env` (or environment) is used. If neither is set, falls back to legacy `MCPAGENT_CONFIG_DIR` mode.

---

### `mcpagent chat` — Interactive REPL (default)

```bash
mcpagent                # equivalent
mcpagent chat
mcpagent -d .my-project chat
```

No additional arguments. Starts a Rich-powered interactive session with streaming output, tool call display, and slash commands.

When launched, the agent:
1. Loads config from `MCPAGENT_APP_DIR` (or `--app-dir`)
2. Connects MCP servers required by the default agent preset
3. Enters the interactive REPL

#### Interactive Slash Commands

| Command | Description |
|---|---|
| `/help` | Show available commands |
| `/exit` | Exit the agent |
| `/clear` | Clear conversation history (starts fresh) |
| `/tools` | List all available tools (built-in + MCP) |
| `/servers` | Show MCP server connection status |
| `/memory` | Show memory directory contents |
| `/agents` | List available agent presets |
| `/agent <name>` | Switch to a different agent preset (manages MCP servers) |
| `/skills` | List available skill modules |
| `/context` | Show context window usage (tokens used / remaining) |
| `/bg` | Show background workflow tasks and their status |
| `/reload` | Reload agents, skills, and MCP config from disk (hot-reload) |

---

### `mcpagent run` — Headless One-Shot Execution

Run an agent with a single message and exit. Useful for scripting, CI/CD, and cron jobs.

```bash
mcpagent run --message "Summarize the README"
mcpagent run -a web-researcher -m "Find latest news on AI agents"
mcpagent -d .my-project run -a analyst -m "Analyze sales data"
```

| Flag | Short | Required | Default | Description |
|---|---|---|---|---|
| `--message` | `-m` | **yes** | — | Message to send to the agent |
| `--agent` | `-a` | no | `default_agent` from config | Agent preset name to use |

The agent runs a full ReAct loop (tool calls, iterations), prints the final response to stdout, and exits. Chat history is saved to the history directory.

---

### `mcpagent job` — Workflow Management

Manage and run YAML-defined workflows.

#### `mcpagent job list`

List all available workflows from the `workflows/` directory.

```bash
mcpagent job list
mcpagent -d .my-project job list
```

Output columns: `Name`, `Schedule`, `Enabled`, `Steps count`.

#### `mcpagent job run <name>`

Run a workflow by name (manual trigger, synchronous).

```bash
mcpagent job run example-research
mcpagent -d .my-project job run power-platform-news
```

| Argument | Required | Description |
|---|---|---|
| `name` | **yes** | Workflow name (matches `name:` field in YAML, or filename without `.yaml`) |

Runs the full DAG: resolves dependencies, executes steps in topological order (parallel where possible), respects retries and conditions. Prints per-step status on completion.

#### `mcpagent job history [name]`

Show workflow run history.

```bash
mcpagent job history                 # all workflows
mcpagent job history example-research  # filter by name
mcpagent job history -n 50           # last 50 runs
```

| Argument / Flag | Required | Default | Description |
|---|---|---|---|
| `name` | no | all | Filter by workflow name |
| `--limit` / `-n` | no | `20` | Maximum number of rows to display |

#### `mcpagent job status <run_id>`

Show detailed status of a specific workflow run including per-step results.

```bash
mcpagent job status 42
```

| Argument | Required | Description |
|---|---|---|
| `run_id` | **yes** | Numeric run ID (from `job history` output) |

---

### `mcpagent scheduler` — Workflow Scheduler Daemon

#### `mcpagent scheduler start`

Start a long-running scheduler daemon that triggers workflows based on their `schedule` (cron) or `interval` settings.

```bash
mcpagent scheduler start
mcpagent -d .my-project scheduler start
```

The scheduler:
- Scans `workflows/` for enabled workflows with `schedule` or `interval`
- Runs indefinitely, triggering workflows at their configured times
- Uses headless agent instances for each workflow step
- Persists all run results to `mcpagent.db`

Stop with `Ctrl+C`.

#### `mcpagent scheduler status`

Show configured scheduled workflows and their next run times.

```bash
mcpagent scheduler status
```

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

Azure OpenAI credentials and the application directory are set here:

```env
# Azure OpenAI (required)
AZURE_OPENAI_API_KEY=your-key-here
AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-4o
AZURE_OPENAI_API_VERSION=2025-01-01-preview

# Application directory (recommended)
MCPAGENT_APP_DIR=.local-assistants

# Logging level
MCPAGENT_LOG_LEVEL=WARNING
```

### config.yaml

Located inside `MCPAGENT_APP_DIR` (or `config/` in legacy mode):

```yaml
models:
  default:
    provider: azure

default_model: default
default_agent: assistant          # agent preset from agents/*.md

agent:
  max_iterations: 30
  context_window: 128000
  summarize_threshold: 0.7
  max_tool_result_tokens: 8000
  summary_max_tokens: 1000

storage:
  data_dir: "."                   # relative to MCPAGENT_APP_DIR
  chat_history: true
  logs: true

skills_dir: "skills"              # relative to MCPAGENT_APP_DIR
agents_dir: "agents"
workflows_dir: "workflows"

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

When using `MCPAGENT_APP_DIR`, all relative paths (`data_dir`, `skills_dir`, `agents_dir`, `workflows_dir`) resolve from that directory. Setting `data_dir: "."` keeps everything in one place.

### mcp.json

Located alongside `config.yaml`. Uses the same format as VS Code `mcp.json`. Supports `stdio` and `http` transport types. Environment variable placeholders (`${input:NAME}`, `${env:NAME}`) are resolved from `.env` or environment variables.

```json
{
  "servers": {
    "my-server": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "some-mcp-server"],
      "env": { "API_KEY": "${env:MY_API_KEY}" }
    },
    "remote-server": {
      "type": "http",
      "url": "https://example.com/api/mcp"
    }
  }
}
```

### Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `MCPAGENT_APP_DIR` | *(not set)* | Self-contained project directory. When set, `config.yaml`, `mcp.json`, and all relative paths resolve from here |
| `MCPAGENT_CONFIG_DIR` | `config/` | Legacy: directory with `config.yaml` + `mcp.json`. Ignored when `MCPAGENT_APP_DIR` is set |
| `MCPAGENT_LOG_LEVEL` | `WARNING` | Python logging level (`DEBUG`, `INFO`, `WARNING`) |
| `AZURE_OPENAI_API_KEY` | — | Azure OpenAI API key |
| `AZURE_OPENAI_ENDPOINT` | — | Azure OpenAI endpoint URL |
| `AZURE_OPENAI_DEPLOYMENT` | — | Model deployment name |
| `AZURE_OPENAI_API_VERSION` | `2024-12-01-preview` | API version |

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

All persistent data lives under `data_dir` (configured in `config.yaml`). When using `MCPAGENT_APP_DIR` with `data_dir: "."`, everything stays in one folder:

```
MCPAGENT_APP_DIR/
├── config.yaml
├── mcp.json
├── agents/
│   └── assistant.md
├── skills/
│   └── summarizer/
│       └── SKILL.md
├── workflows/
│   └── example-research.yaml
├── mcpagent.db               # SQLite — workflow runs & step results
├── history/                   # Chat history (one JSON per session)
│   └── chat_20260414_145618.json
├── logs/                      # Execution logs (one JSONL per session)
│   └── session_20260414_145618.jsonl
├── ops/                       # Operational metrics
└── memories/
    ├── user/                  # Persistent across all sessions
    ├── session/               # Current conversation only
    └── repo/                  # Per-project notes
```

Memory is exposed as tools (`memory_view`, `memory_create`, `memory_update`, `memory_delete`) so the agent can read/write notes during conversations.

## Roadmap

- [ ] Sub-agent orchestration
- [ ] Textual TUI
- [ ] @references
