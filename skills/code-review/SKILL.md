---
name: code-review
description: Reviews code for bugs, style issues, and security vulnerabilities
triggers:
  - review code
  - code review
  - review this
  - review file
  - review the
  - check code
  - find bugs
  - security review
---
# Code Review Skill

When performing a code review, follow this structured approach:

## 1. Security Check
- Look for hardcoded secrets, API keys, passwords
- Check for SQL injection, XSS, path traversal vulnerabilities
- Verify input validation at system boundaries
- Check for proper authentication/authorization

## 2. Bug Detection
- Look for off-by-one errors, null/None dereferences
- Check error handling: are exceptions caught and handled properly?
- Verify edge cases: empty inputs, large inputs, concurrent access
- Check resource leaks (unclosed files, connections, etc.)

## 3. Code Quality
- Check naming: are variables/functions clearly named?
- Look for code duplication that should be refactored
- Verify functions are focused (single responsibility)
- Check for overly complex logic that could be simplified

## 4. Output Format
Present findings as a structured list:
- **Critical** — security issues, data loss risks
- **Bug** — logic errors, unhandled edge cases
- **Warning** — potential issues, code smells
- **Suggestion** — style improvements, readability

Always read the file(s) first using `read_file` before reviewing.
