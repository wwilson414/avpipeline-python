Token-Saving Protocol (TSP)
To minimize API costs and prevent context-window saturation, all agents must follow these rules:

1. Incremental Reading (No Full-File Dumps)
Do NOT read entire files if you only need a specific function. Use grep or target line ranges (e.g., lines 20-50).
Do NOT output entire files in your response when making changes. Use Unified Diff format or precise search-and-replace blocks.
2. Minimalist Logging
Keep .github/ai/progress.md and handoff.md extremely concise.
Use references (e.g., See auth.js:L45) instead of copy-pasting code blocks into logs.
3. Error Slicing
When reporting test failures, extract only the stack trace slice (max 5 lines). Delete all successful test logs, setup/teardown logs, and environment variables before writing to handoff.md.