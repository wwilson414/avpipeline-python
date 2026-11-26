---
applyTo: ".github/ai/**/*.md"
---

# AI Workflow Document Rules

## Markdown

- Use hierarchical ATX headings. Put a blank line around headings, lists, tables, blockquotes, and fenced code blocks.
- Every opening fence has a matching closing fence. Tag fences with a language when known.
- Tables have exactly one separator row, the same cell count as the header, and spaced delimiters:

```markdown
| Column | Other |
| ------ | ----- |
```

## Structure

- `handoff.md`: starts with exactly one `# Current Handoff` heading. Sections: Goal, Execution State, Blockers, Next Step. Replace, never append.
- `progress.md`: each event starts with `## YYYY-MM-DD HH:MM - <title>`. Newest first. At most 20 events. A retrospective event records both the occurrence and recording timestamps.
- `plan.md` and `tasks.md`: current state only. No change history, no session logs, no `Change History` or `Session Log` headings.
- `archive/*.md`: append only. Archives that hold events stay newest-first overall, so displaced (older) events are appended at the end.

## Language

English only. Timestamps use `YYYY-MM-DD HH:MM`.
