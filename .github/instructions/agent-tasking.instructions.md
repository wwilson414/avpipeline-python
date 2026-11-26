---
applyTo: "**"
description: "Use when handling implementation, debugging, troubleshooting, or coding tasks in this workspace. Enforces concise expert-level output, targeted file inspection, minimal edits, and root-cause-first troubleshooting."
---
# Agent Tasking Rules

- Assume expert-level user knowledge.
- Be concise.
- Do not restate the request.
- Minimize token usage.

## Agent Execution


- For ad-hoc tasks, read the minimum number of files required. (For active multi-agent workflow tasks, follow the selective loading gate in `copilot-instructions.md` first.)
- Start with files explicitly referenced by the user.
- If required to complete a correct fix, inspect only the minimum set of directly related files.
- Read the minimum number of files required.
- Start with files explicitly referenced by the user.
- If required to complete a correct fix, inspect only the minimum set of directly related files.
- Avoid workspace-wide searches unless explicitly requested.
- Prefer targeted edits.
- Avoid broad refactoring.
- Do not create new files unless required.
- Prefer minimal file modifications.

## Troubleshooting

- Root cause first.
- Most likely fix second.
- Commands third.
- Limit speculative causes.

## Coding Output

- Follow the active agent's role-specific output format when one is selected. Otherwise, make the
  requested change directly and explain only what is needed to understand the result.

## Documentation Format

- Keep Markdown heading levels hierarchical and use ATX headings.
- Leave blank lines around headings, lists, tables, blockquotes, and fenced code blocks.
- Use fenced code blocks with a language identifier where one exists; every opening fence must have a matching closing fence.
- Markdown tables must have one separator row whose cells match the header column count and use spaced delimiters, for example: `| Column |` then `| ------ |`.
- Keep relative links valid and preserve the owning workflow document's required structure.