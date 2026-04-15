---
name: skill-authoring
description: "Creates and edits MCPAgent skill modules — SKILL.md files with YAML frontmatter, structured instructions, and tool-referencing procedures."
---
# Skill Authoring Skill

This skill covers creating, editing, and debugging MCPAgent skill modules — reusable knowledge packs that agents load on demand via `load_skill`.

---

## 1. What Is a Skill?

A skill is a **SKILL.md** file containing domain-specific instructions that get injected into the agent's context when loaded. Skills are:
- **Lazy** — loaded only when the agent calls `load_skill`
- **Reusable** — any agent with the skill in its `skills:` list can use it
- **Self-describing** — the `description` field in frontmatter is used for discovery and matching

---

## 2. File Location and Naming

- Skills live in subdirectories under `skills_dir` (default: `skills/`)
- Each skill is a directory containing exactly one `SKILL.md`
- Directory name = skill name (lowercase, hyphenated)

```
skills/
├── code-review/
│   └── SKILL.md
├── summarizer/
│   └── SKILL.md
└── workflow-authoring/
    └── SKILL.md
```

---

## 3. SKILL.md Schema

### YAML Frontmatter

```yaml
---
name: my-skill                      # REQUIRED. Must match directory name
description: "Short description"    # REQUIRED. Used for skill discovery and LLM matching
---
```

The `description` should be a clear, keyword-rich sentence so the LLM can decide when to load the skill. Include:
- What the skill covers
- When to use it (trigger phrases / scenarios)

### Markdown Body

Everything after the closing `---` is the skill content — instructions injected into the agent's context.

---

## 4. Skill Content Structure

Follow this proven template:

```markdown
# <Skill Name> Skill

One-sentence overview of what this skill provides.

---

## 1. <Concept / Context Section>
Background knowledge the agent needs.

## 2. <Schema / Reference Section>
Formats, schemas, field descriptions, valid values.

## 3. <Procedure / How-To Section>
Step-by-step instructions for common tasks.

## 4. <Examples Section>
Concrete examples with code/YAML blocks.

## 5. <Common Patterns / Archetypes>
Quick-reference table of typical configurations.

## 6. <Pitfalls / Anti-Patterns>
What to avoid and why.
```

---

## 5. Writing Guidelines

### Content
- **Be imperative** — "Do X", "Always Y", "Never Z"
- **Be specific** — reference exact tool names (`read_file`, `write_file`, `grep_search`)
- **Be structured** — use numbered sections, tables, code blocks
- **Include examples** — always show at least one minimal and one full example
- **Reference tools** — tell the agent which tools to use for each step

### Size
- Aim for 100–300 lines; skills are injected into context so brevity matters
- Use tables for reference data instead of verbose prose
- Don't repeat information that's already in tool schemas

### Language
- Write all skill content in **English**
- Use Markdown formatting consistently (headers, code fences, tables, bullet lists)

### Description field
- Must be a single, keyword-rich sentence
- Include the domain, key actions, and when to trigger
- Examples:
  - ✅ `"Creates and edits MCPAgent workflow YAML files — DAG-based agent orchestration with scheduling."`
  - ✅ `"Reviews code for bugs, style issues, and security vulnerabilities."`
  - ❌ `"A skill for doing stuff"` (too vague)
  - ❌ Three sentences of description (too long)

---

## 6. Step-by-Step: Creating a New Skill

1. **Define the domain** — what narrow area does this skill cover?
2. **Choose a name** — lowercase, hyphenated, descriptive (e.g. `api-design`, `data-pipeline`)
3. **Write the description** — one keyword-rich sentence for discovery
4. **Draft the content** — follow the template in §4
5. **Include examples** — at least one minimal and one complete example
6. **Create the directory** — `skills/<name>/`
7. **Write the file** — `write_file` to `skills/<name>/SKILL.md`
8. **Verify** — `read_file` to confirm content and frontmatter are correct
9. **Test** — from any agent, run `load_skill` with the skill name and verify it loads

---

## 7. Modifying an Existing Skill

1. Read the current file with `read_file` at `skills/<name>/SKILL.md`
2. Identify what needs to change (frontmatter, structure, content, examples)
3. Edit the file
4. Read back to verify
5. Test by loading the skill in an agent session

---

## 8. Common Skill Categories

| Category | Focus | Example names |
|---|---|---|
| Review / Analysis | Evaluate code, configs, designs | `code-review`, `security-audit` |
| Authoring / Scaffolding | Create new files from patterns | `agent-authoring`, `workflow-authoring` |
| Transformation | Convert between formats | `summarizer`, `translator` |
| Domain Knowledge | Inject specialized knowledge | `kubernetes-ops`, `sql-tuning` |
| Procedure | Step-by-step operational guides | `incident-response`, `deployment` |

---

## 9. Anti-Patterns

- ❌ **Skill too broad** — "general coding assistant" is not a skill, it's an agent
- ❌ **No examples** — agents perform much better with concrete examples
- ❌ **Duplicating tool docs** — don't explain tool parameters; the agent already has schemas
- ❌ **Vague description** — if the description doesn't contain keywords, the skill won't be discovered
- ❌ **Huge skill** — if content exceeds ~400 lines, split into multiple skills
- ❌ **Missing frontmatter** — both `name` and `description` are required
