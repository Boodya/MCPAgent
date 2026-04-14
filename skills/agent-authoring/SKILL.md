---
name: agent-authoring
description: "Creates and edits MCPAgent agent presets — YAML frontmatter configuration and system prompts in agents/*.md files."
---
# Agent Authoring Skill

This skill covers creating, editing, and debugging MCPAgent agent preset files — the configuration that defines an agent's identity, capabilities, and behaviour.

---

## 1. File Location and Naming

- Agent presets live in the directory specified by `agents_dir` in `config/config.yaml` (default: `agents/`)
- Each file is `<agent-name>.md` — lowercase, hyphen-separated
- The `name` field in YAML frontmatter **must match** the filename (without `.md`)
- `default.md` is special — it's the fallback agent

```
agents/
├── default.md
├── architect.md
├── web-researcher.md
└── code-reviewer.md
```

---

## 2. Agent Preset Schema

### YAML Frontmatter (between `---` fences)

```yaml
---
name: my-agent                     # REQUIRED. Must match filename
description: "What this agent does" # REQUIRED. Shown in agent listings and call_agent
model: default                     # Optional (default: "default"). Model key from config.yaml
tools: all                         # Tool access — see §3
mcp_servers: all                   # MCP server access — see §4
skills: []                         # Skill access — see §5
subagents:                         # Agents this agent can call via call_agent
  - default
  - web-researcher
---
```

### System Prompt (Markdown body after frontmatter)

Everything after the closing `---` is the system prompt injected into the LLM context.

---

## 3. Tool Filtering (`tools:` field)

| Value | Meaning |
|---|---|
| `all` | Access to every registered tool |
| `[]` (empty list) | Only `load_skill` and `call_agent` (always available) |
| `[read_file, write_file, grep_search]` | Explicit allowlist |
| `[read_*, searxng__*]` | Wildcard patterns (glob-style) |

**Rules:**
- `load_skill` and `call_agent` are **always** available regardless of `tools:` setting
- Use minimal tool sets — don't give `all` unless the agent genuinely needs it
- Wildcards like `searxng__*` grant all tools from a specific MCP server

---

## 4. MCP Server Selection (`mcp_servers:` field)

| Value | Meaning |
|---|---|
| `all` | Start all configured MCP servers |
| `[]` (empty list) | No MCP servers — agent uses only built-in tools |
| `[searxng, github]` | Start only these servers |

**Rules:**
- MCP servers are **expensive** — each is a subprocess. Only enable what the agent needs
- When switching agents via `call_agent`, MCPAgent automatically starts/stops servers as needed
- Server names must match keys from `config/mcp.json`
- Before referencing server names, inspect `config/mcp.json` to see what's available

---

## 5. Skills (`skills:` field)

| Value | Meaning |
|---|---|
| `all` | Agent can load any skill via `load_skill` |
| `[]` (empty list) | No skills available |
| `[code-review, summarizer]` | Only these skills can be loaded |

---

## 6. Subagents (`subagents:` field)

- Lists agent names this agent can invoke via `call_agent`
- Sub-agents get their own tool/MCP context but share the same conversation scope
- Keep subagent chains shallow (avoid deep nesting)

---

## 7. System Prompt Best Practices

### Structure
1. **Role definition** — one sentence: "You are **Name** — <purpose>."
2. **Rules / constraints** — non-negotiable behaviours (bullet list)
3. **Operating procedure** — numbered steps for common workflows
4. **Output preferences** — format, length, language

### Writing guidelines
- Be imperative and direct: "Do X", "Never Y", "Always Z"
- Keep the prompt under ~500 words; the LLM has limited attention
- Use Markdown headers (`#`, `##`) for section separation
- Put the most critical rules at the top (primacy bias)
- Write prompts in **English** — even if the user speaks another language
- Don't repeat tool documentation; the agent already sees tool schemas
- Reference skills the agent should use: "Load `skill-name` for X tasks"

### Anti-patterns to avoid
- ❌ Long paragraphs of context the LLM won't retain
- ❌ Duplicating tool parameter docs in the prompt
- ❌ Giving the agent `tools: all` + `mcp_servers: all` "just in case"
- ❌ Omitting the `description` field (it's used for agent discovery)

---

## 8. Step-by-Step: Creating a New Agent

1. **Clarify purpose** — what does this agent do? What's its specialty?
2. **Choose a name** — lowercase, hyphenated, descriptive (e.g. `data-analyst`)
3. **Identify tools** — what built-in tools does it need? List explicitly
4. **Identify MCP servers** — does it need web search? GitHub? DB? List explicitly
5. **Identify skills** — which skills should it be able to load?
6. **Identify subagents** — can it delegate to other agents?
7. **Write the system prompt** — follow §7 structure
8. **Write the file** — `write_file` to `agents/<name>.md`
9. **Verify** — `read_file` to confirm content is correct
10. **Test** — switch to the agent and run a sample interaction

---

## 9. Modifying an Existing Agent

1. Read the current file with `read_file`
2. Identify what needs to change (frontmatter vs. prompt vs. both)
3. Edit using `write_file` (full replace) or targeted line edits
4. Read back to verify
5. Test the modified agent

---

## 10. Common Agent Archetypes

| Archetype | tools | mcp_servers | skills | subagents |
|---|---|---|---|---|
| Researcher | `[read_file, write_file, list_dir, grep_search]` | `[searxng]` | `[summarizer]` | `[]` |
| Coder | `all` | `[]` | `[code-review]` | `[]` |
| Orchestrator | `[read_file, write_file, list_dir]` | `[]` | `all` | `[default, web-researcher]` |
| Analyst | `[read_file, grep_search, run_command]` | `[]` | `[summarizer]` | `[]` |
| Tester | `all` | `all` | `[]` | `[default]` |
