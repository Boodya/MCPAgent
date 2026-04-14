---
name: summarizer
description: Summarizes files, code, logs, and documents concisely
---
# Summarizer Skill

When asked to summarize content, follow these rules:

## Approach
1. Read the full content first (use `read_file` or `grep_search` as needed)
2. Identify the type of content: code, documentation, logs, config, etc.
3. Produce a structured summary appropriate for the type

## For Code Files
- **Purpose**: one sentence describing what the file/module does
- **Key components**: list classes, functions, important variables
- **Dependencies**: external imports and what they're used for
- **Entry points**: main functions, CLI handlers, API endpoints

## For Documentation
- **Topic**: what the document covers
- **Key points**: 3-5 bullet points of the most important information
- **Action items**: any TODOs, requirements, or next steps mentioned

## For Logs / Error Output
- **Timeframe**: when events occurred
- **Key events**: errors, warnings, state changes
- **Root cause**: if identifiable from the log data
- **Pattern**: any recurring issues

## Rules
- Keep summaries under 200 words unless asked for detail
- Use bullet points, not paragraphs
- Start with the most important information
- If content is too large, summarize in sections
