---
name: workflow-authoring
description: "Creates and edits MCPAgent workflow YAML files — DAG-based agent orchestration with scheduling, conditions, retry, and variable interpolation."
---
# Workflow Authoring Skill

This skill covers creating, editing, and debugging MCPAgent workflow YAML files — the system for orchestrating multi-step agent pipelines that run as DAGs.

---

## 1. File Location and Discovery

- Workflows live in the directory specified by `workflows_dir` in `config/config.yaml` (default: `workflows/`)
- The loader scans for `*.yaml` and `*.yml` files in that directory (non-recursive, alphabetically sorted)
- Each file defines exactly **one** workflow
- Filename convention: `<workflow-name>.yaml` — kebab-case, matches the `name` field inside

```
workflows/
├── daily-report.yaml
├── code-review-pipeline.yaml
└── data-sync.yaml
```

---

## 2. Workflow YAML Schema

### Minimal valid workflow

```yaml
name: hello-world
steps:
  - id: greet
    prompt: "Say hello world"
```

### Full schema with all fields

```yaml
# --- Workflow metadata ---
name: my-workflow                  # REQUIRED. Unique identifier (used in CLI and DB)
description: "What this does"      # Optional. Human-readable description
schedule: "30 9 * * 1-5"           # Optional. 5-field cron (UTC). Mutually exclusive concept with interval
interval: 3600                     # Optional. Run every N seconds. Alternative to cron
enabled: true                      # Optional (default: true). Set false to exclude from scheduler

# --- Variables ---
vars:                              # Optional. Key-value dict accessible as {{vars.KEY}} in prompts
  topic: "AI agents"
  output_dir: "reports"
  max_sources: "10"                # NOTE: all values are interpolated as strings

# --- Steps (DAG) ---
steps:                             # REQUIRED. List of step definitions
  - id: step-one                   # REQUIRED. Unique within this workflow (kebab-case recommended)
    agent: default                 # Optional (default: "default"). Agent preset name from agents/ dir
    prompt: "Do something"         # REQUIRED. Prompt template — supports {{vars.*}} and {{steps.*.result}}
    depends_on: []                 # Optional (default: []). List of step IDs that must complete first
    condition: null                # Optional. Python expression — step runs only if truthy
    timeout: 600                   # Optional (default: 600). Max seconds before step is killed
    retry:                         # Optional. Retry configuration
      count: 0                     #   Number of retry attempts (default: 0 = no retry)
      delay: 5.0                   #   Seconds to wait between retries (default: 5.0)
    on_failure: stop               # Optional (default: "stop"). "stop" = skip dependents; "continue" = ignore failure
```

---

## 3. Step IDs — Naming Rules

- Must be unique within the workflow
- Used in `depends_on` references and `{{steps.<id>.result}}` templates
- Recommended: kebab-case (`gather-data`, `analyze-results`)
- Allowed characters: alphanumeric, hyphens, underscores
- **Do NOT use dots** in step IDs — the template engine splits on `.` for resolution

---

## 4. DAG Dependencies

### How it works

Steps form a Directed Acyclic Graph (DAG) via `depends_on`. The engine:
1. Performs topological sort into **levels** (groups of steps with all deps satisfied)
2. Executes all steps within a level **in parallel** via `asyncio.gather()`
3. Proceeds to the next level only after the current level completes

### Rules

- A step with no `depends_on` runs in the **first level** (immediately)
- Multiple root steps run in parallel
- If step A's dependency B failed AND B has `on_failure: stop` (default), step A is **skipped**
- Circular dependencies are detected at YAML load time and raise a `ValueError`
- Referencing a non-existent step ID in `depends_on` raises a `ValueError` at load time

### DAG patterns

**Linear chain:**
```yaml
steps:
  - id: step-1
    prompt: "..."
  - id: step-2
    prompt: "... {{steps.step-1.result}}"
    depends_on: [step-1]
  - id: step-3
    prompt: "... {{steps.step-2.result}}"
    depends_on: [step-2]
```

**Fan-out (parallel split):**
```yaml
steps:
  - id: gather
    prompt: "Collect data"
  - id: analyze-a
    prompt: "Analyze aspect A: {{steps.gather.result}}"
    depends_on: [gather]
  - id: analyze-b
    prompt: "Analyze aspect B: {{steps.gather.result}}"
    depends_on: [gather]
```
`analyze-a` and `analyze-b` run **simultaneously**.

**Fan-in (join):**
```yaml
  - id: merge
    prompt: |
      Combine results:
      A: {{steps.analyze-a.result}}
      B: {{steps.analyze-b.result}}
    depends_on: [analyze-a, analyze-b]
```

**Diamond (fan-out + fan-in):**
```
  root → ┬─ branch-a ─┬─ merge
         └─ branch-b ─┘
```

---

## 5. Variable Interpolation

### Syntax

```
{{vars.VARIABLE_NAME}}
{{steps.STEP_ID.result}}
{{steps.STEP_ID.status}}
```

- Whitespace inside braces is allowed: `{{ vars.topic }}` works
- Templates are rendered at step execution time — `{{steps.X.result}}` is only available if step X has already completed (i.e. it's in `depends_on` or an ancestor thereof)
- **Unresolved placeholders are left as-is** — the literal `{{steps.missing.result}}` string remains in the prompt. This is by design (no crash), but means the agent sees the raw template.

### Important: always declare depends_on when referencing step results

```yaml
# WRONG — analyze may run before gather completes, {{steps.gather.result}} will be unresolved
  - id: analyze
    prompt: "Analyze: {{steps.gather.result}}"

# CORRECT
  - id: analyze
    prompt: "Analyze: {{steps.gather.result}}"
    depends_on: [gather]
```

### vars are simple string substitution

All `vars` values are converted to strings during interpolation. There is no type system — numbers, booleans, lists are all stringified. Use vars for:
- Topics, names, paths, URLs
- Configuration values that you want to change without editing prompts

---

## 6. Agent Presets

Each step's `agent` field references a preset file in `agents/<name>.md`. Available presets are discovered from the `agents/` directory.

### What the agent field controls

- **System prompt** — the agent preset's markdown body becomes the system prompt
- **Tools** — the preset defines which built-in tools the agent has access to (`tools: all` or a list)
- **MCP servers** — the preset defines which MCP servers to start (`mcp_servers: all`, a list, or `[]`)
- **Skills** — the preset defines injected skills (`skills: all` or a list)

### Current presets in this project

| Preset | Description | MCP Servers |
|---|---|---|
| `default` | General-purpose assistant, all tools | all |
| `web-researcher` | Web research via SearXNG | `[searxng]` |
| `architect` | Architecture and design | varies |
| `tester` | Testing agent | varies |

### Important: MCP server startup cost

Each step creates a **fresh agent instance** via `create_agent()`. This means MCP servers are started and stopped per step. For steps that don't need MCP tools, use a preset with `mcp_servers: []` to avoid the startup overhead.

```yaml
  - id: summarize
    agent: default        # starts all MCP servers — slow if you don't need them
    prompt: "Summarize..."

  # Better: create a "summarizer" preset with mcp_servers: [] if no MCP tools needed
```

---

## 7. Scheduling

### Cron expressions (UTC)

Standard 5-field format: `minute hour day_of_month month day_of_week`

| Field | Range | Special |
|---|---|---|
| minute | 0-59 | `*`, `,`, `-`, `/` |
| hour | 0-23 | `*`, `,`, `-`, `/` |
| day of month | 1-31 | `*`, `,`, `-`, `/` |
| month | 1-12 | `*`, `,`, `-`, `/` |
| day of week | 0-6 (Mon=0, Sun=6) | `*`, `,`, `-`, `/` |

**WARNING: day of week uses ISO convention** — 0=Monday, 6=Sunday. Value 7 is also accepted as Sunday.

Examples:
```yaml
schedule: "0 9 * * *"        # daily at 09:00 UTC
schedule: "*/15 * * * *"     # every 15 minutes
schedule: "30 8 * * 1-5"     # weekdays at 08:30 UTC
schedule: "0 0 1 * *"        # 1st of every month at midnight
schedule: "0 9,14 * * *"     # daily at 09:00 and 14:00 UTC
```

### Interval (seconds)

```yaml
interval: 3600    # every hour
interval: 300     # every 5 minutes
```

### Rules

- `schedule` and `interval` are conceptually mutually exclusive — if both are set, `schedule` takes priority in the scheduler loop
- Setting neither means the workflow is **manual only** — run via `mcpagent job run <name>`
- `enabled: false` excludes the workflow from `mcpagent scheduler start` but it can still be run manually
- The scheduler daemon runs all enabled workflows with a schedule/interval; one asyncio task per workflow

---

## 8. Conditions

The `condition` field is a Python expression evaluated in a **safe AST sandbox** before the step runs. If it evaluates to falsy, the step is skipped (status: `skipped`).

### Available context in conditions

```python
steps['step-id']['result']   # string — the text output of a completed step
steps['step-id']['status']   # string — "completed", "failed", or "skipped"
```

### Allowed operations

- Comparisons: `==`, `!=`, `>`, `<`, `>=`, `<=`
- Boolean: `and`, `or`, `not`
- Membership: `in`, `not in`
- Safe builtins: `len()`, `str()`, `int()`, `float()`, `bool()`
- Literals: strings, numbers, `True`, `False`

### Blocked

- Import statements
- Attribute access to private members (`_anything`)
- Function calls to anything outside the whitelist
- Assignment — conditions are `eval()`, not `exec()`

### Examples

```yaml
  - id: notify-if-long
    condition: "len(steps['analyze']['result']) > 5000"
    prompt: "The analysis was very long. Create a TL;DR..."
    depends_on: [analyze]

  - id: fallback
    condition: "steps['primary']['status'] == 'failed'"
    prompt: "Primary failed. Try alternative approach..."
    depends_on: [primary]

  - id: skip-if-empty
    condition: "'no results' not in steps['search']['result'].lower()"
    prompt: "Process search results..."
    depends_on: [search]
```

### Gotcha: condition failure = skip, not error

If the condition expression itself throws an exception (syntax error, missing key, etc.), the step is **silently skipped** (logged as warning). This is defensive by design — a typo in a condition won't crash the entire workflow.

---

## 9. Retry and Timeout

### Timeout

```yaml
  - id: slow-research
    timeout: 900          # 15 minutes max
    prompt: "Deep web research on..."
```

- Default: 600 seconds (10 minutes)
- If the agent doesn't complete within the timeout, the step fails with a timeout error
- Timeout wraps the entire `agent.run_to_completion()` call including all tool calls

### Retry

```yaml
  - id: flaky-api-call
    retry:
      count: 3            # retry up to 3 times (4 total attempts)
      delay: 10.0         # wait 10s between retries
    prompt: "Call the external API..."
```

- Retries happen if the step errors out OR if `AgentResult.error` is set
- Each retry creates a fresh agent instance
- The delay is a fixed sleep between attempts (not exponential)
- The rendered prompt is the same for all attempts

---

## 10. Failure Handling

### `on_failure: stop` (default)

If the step fails (after all retries exhausted), all downstream steps that depend on it (directly or transitively) are **skipped**.

```yaml
  - id: critical-step
    on_failure: stop        # default — dependents won't run if this fails
    prompt: "Must succeed"
  - id: next-step
    depends_on: [critical-step]   # will be SKIPPED if critical-step fails
```

### `on_failure: continue`

The step's failure is recorded, but downstream steps still run. Useful for optional/best-effort steps.

```yaml
  - id: optional-enrichment
    on_failure: continue    # failure won't block dependents
    prompt: "Try to enrich data..."
  - id: save-report
    depends_on: [optional-enrichment]   # runs even if enrichment failed
```

**Note:** when a failed step has `on_failure: continue`, dependents can still reference `{{steps.optional-enrichment.result}}` — it will be an empty string.

---

## 11. Running Workflows

### Manual run (CLI)

```bash
# List available workflows
mcpagent job list

# Run a specific workflow
mcpagent job run daily-report

# View run history
mcpagent job history
mcpagent job history daily-report --limit 10

# Check a specific run
mcpagent job status 42
```

### Scheduled (daemon)

```bash
# Start the scheduler — runs all enabled workflows with schedule/interval
mcpagent scheduler start

# Check what's scheduled
mcpagent scheduler status
```

### Headless one-shot (single agent, no workflow)

```bash
mcpagent run --agent default --message "Summarize the README"
```

---

## 12. SQLite State Store

All runs are persisted in `<data_dir>/mcpagent.db` (default: `.mcpagent/mcpagent.db`).

### Tables

**`runs`** — one row per workflow execution:
| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment run ID |
| `workflow_name` | TEXT | Workflow name |
| `status` | TEXT | `pending` → `running` → `completed` / `failed` |
| `trigger_type` | TEXT | `manual` or `scheduled` |
| `started_at` | TEXT (ISO) | UTC timestamp |
| `finished_at` | TEXT (ISO) | UTC timestamp |
| `error` | TEXT | Error message if failed |

**`step_runs`** — one row per step execution:
| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `run_id` | INTEGER FK | References `runs.id` |
| `step_id` | TEXT | Step ID from YAML |
| `agent_name` | TEXT | Agent preset used |
| `status` | TEXT | `pending` → `running` → `completed` / `failed` / `skipped` |
| `prompt_rendered` | TEXT | Final prompt after template rendering |
| `result_text` | TEXT | Agent's text output |
| `started_at` | TEXT (ISO) | UTC timestamp |
| `finished_at` | TEXT (ISO) | UTC timestamp |
| `error` | TEXT | Error message if failed |

---

## 13. Common Patterns

### Pattern: Research → Analyze → Report

```yaml
name: weekly-research
schedule: "0 10 * * 1"    # every Monday at 10:00 UTC
vars:
  topic: "LLM frameworks"

steps:
  - id: research
    agent: web-researcher
    prompt: "Research latest developments in: {{vars.topic}}"
    timeout: 300

  - id: analyze
    agent: default
    prompt: "Identify key trends from: {{steps.research.result}}"
    depends_on: [research]

  - id: report
    agent: default
    prompt: "Write a markdown report and save to reports/weekly.md: {{steps.analyze.result}}"
    depends_on: [analyze]
```

### Pattern: Parallel processing with join

```yaml
name: multi-source-analysis
vars:
  source_a: "https://example.com/feed-a"
  source_b: "https://example.com/feed-b"

steps:
  - id: fetch-a
    agent: web-researcher
    prompt: "Read and summarize: {{vars.source_a}}"
  - id: fetch-b
    agent: web-researcher
    prompt: "Read and summarize: {{vars.source_b}}"
  - id: combine
    agent: default
    prompt: |
      Compare these two sources:
      Source A: {{steps.fetch-a.result}}
      Source B: {{steps.fetch-b.result}}
    depends_on: [fetch-a, fetch-b]
```

### Pattern: Conditional error handling

```yaml
name: resilient-pipeline
steps:
  - id: primary
    agent: default
    prompt: "Try primary approach"
    on_failure: continue

  - id: fallback
    agent: default
    condition: "steps['primary']['status'] == 'failed'"
    prompt: "Primary failed, try fallback approach"
    depends_on: [primary]

  - id: final
    agent: default
    prompt: |
      Use whichever result is available:
      Primary: {{steps.primary.result}}
      Fallback: {{steps.fallback.result}}
    depends_on: [primary, fallback]
```

### Pattern: Code review pipeline

```yaml
name: code-review
vars:
  target_dir: "src/"

steps:
  - id: scan
    agent: default
    prompt: "List all Python files in {{vars.target_dir}} and read their contents"

  - id: security
    agent: default
    prompt: "Review this code for security vulnerabilities: {{steps.scan.result}}"
    depends_on: [scan]

  - id: quality
    agent: default
    prompt: "Review this code for quality and maintainability: {{steps.scan.result}}"
    depends_on: [scan]

  - id: summary
    agent: default
    prompt: |
      Compile a code review report:
      ## Security: {{steps.security.result}}
      ## Quality: {{steps.quality.result}}
    depends_on: [security, quality]
```

---

## 14. Validation Checklist

Before finalizing a workflow, verify:

1. **`name`** is set and unique across all workflow files
2. **Step `id`s** are unique within the workflow, no dots, kebab-case preferred
3. **`depends_on`** references only existing step IDs — misspellings cause load-time errors
4. **No cycles** in the dependency graph — `A → B → A` will fail validation
5. **`{{steps.X.result}}`** is only used when step X is in `depends_on` (directly or transitively)
6. **`agent`** references an existing preset file in `agents/` directory
7. **`schedule`** uses 5-field cron format — not 6-field (no seconds), not systemd-style words
8. **`timeout`** is enough for the agent to finish — web research steps may need 300-600s
9. **`condition`** expressions use dict-style access: `steps['id']['result']`, not `steps.id.result`
10. **`enabled: false`** on workflows not ready for scheduling (they can still be run manually)

---

## 15. Debugging

### Enable verbose logging

```bash
MCPAGENT_LOG_LEVEL=DEBUG mcpagent job run my-workflow
```

This shows:
- Each DAG level and which steps are in it
- Prompt rendering results
- Condition evaluation outcomes
- Retry attempts
- Step completion/failure with timing

### Inspect run results in SQLite

```bash
# Using sqlite3 CLI
sqlite3 .mcpagent/mcpagent.db

# All runs
SELECT id, workflow_name, status, trigger_type, started_at FROM runs ORDER BY id DESC LIMIT 10;

# Steps for a specific run
SELECT step_id, status, error, started_at, finished_at FROM step_runs WHERE run_id = 5;

# See the rendered prompt for a step
SELECT prompt_rendered FROM step_runs WHERE run_id = 5 AND step_id = 'analyze';

# See the agent's output
SELECT result_text FROM step_runs WHERE run_id = 5 AND step_id = 'analyze';
```
