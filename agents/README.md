# Agent Presets Directory

Place agent preset files here (Phase 2 feature).

Each agent is a `.md` file with YAML frontmatter:

```markdown
---
name: planner
description: Decomposes tasks into actionable steps
model: default
tools: [read_file, write_file, grep_search]
skills: []
---
# System Prompt

You are a planning agent. Break down complex tasks...
```

Switch agents in the CLI with `/agent <name>`.
