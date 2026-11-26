---
name: archive-milestone
description: "Close the current milestone - archive its tasks, rotate progress, update task-state."
disable-model-invocation: true
---
# Milestone Archive Transaction

Run only when every task in the current milestone is `done`. Do all steps under the lock.
Both archives are **newest first**: new content goes at the top, never at the end.

Scope: only the `### Active M<N>: <name>` section is archived. Never move or edit
`## Superseded Historical Work` or `## Deferred Future Work`; they stay in `tasks.md`.

1. Acquire the lock. Abort if it fails.

   ```bash
   .github/ai/scripts/ai-lock.sh acquire orchestrator
   ```

2. Verify every task of the milestone is `done`. If any is `pending`, `in-progress`, `review`,
   or `blocked`, release the lock and stop.

3. **Crash check.** Search `.github/ai/archive/tasks-archive.md` for `## Archived M<N>:`.
   - Found: a previous run already archived it. Compare it with the section in `tasks.md`;
     if complete, skip step 4 and go to step 5. If incomplete, stop and tell the user.
   - Not found: continue.

4. Insert the milestone into `.github/ai/archive/tasks-archive.md`:
   - Place it **immediately below** the line `<!-- ARCHIVE-INSERT-POINT ... -->`, above the
     previously newest `## Archived ...` section. Do not append at the end of the file.
   - Rewrite the heading from `### Active M<N>: <name>` to `## Archived M<N>: <name>`.
   - Keep every task row complete (ID, Task, Executor, Priority, Status, Depends On,
     Acceptance Gate) and
     the milestone evidence or acceptance criteria. Use `### M<N> Acceptance Evidence` for evidence.
   - Rewrite intro text that says the milestone is active, planning-only, or pending into past
     tense, or drop it. The archive must not claim a closed milestone is active.
   - Use minimal targeted edits; do not rewrite the whole archive file.

5. Remove only the `### Active M<N>: ...` section from `.github/ai/tasks.md`.
   - Keep the `## Active Tasks` heading, the Superseded and Deferred sections, and the trailing
     `<!-- Current-state task list ends here. -->` / `End of tasks.` markers.
   - If no active milestone remains, put the single line `No active tasks.` under `## Active Tasks`.

6. Insert a progress event for the milestone close (newest first), then rotate:

   ```bash
   .github/ai/scripts/ai-rotate-progress.sh
   ```

7. Update `task-state.json` (schema 1.1.0):
   - `current_task`: set to `null`.
   - If the next milestone is already scheduled: `current_milestone` = its ID,
     `current_milestone_status` = `pending`, `status` = `active`.
   - If none is scheduled: keep `current_milestone` as the closed ID,
     `current_milestone_status` = `done`, and `status` = `complete` only if no schedulable task remains.
   - Insert one `session_history` entry at the top. Keep at most 5 entries by dropping the oldest.
   - Leave the `archive.*` pointers unchanged; they are fixed by the schema.
   - Do not write the deprecated top-level `retry_count`.

8. Validate, then release:

   ```bash
   .github/ai/scripts/ai-validate.sh && .github/ai/scripts/ai-validate-tasks.sh && .github/ai/scripts/ai-lock.sh release orchestrator
   ```

   If validation fails, fix the problem and re-run it. The lock is not released while validation
   fails. Do not release it on a failing state without telling the user. If the lock times out
   (TTL 900 s), re-acquire it before continuing.