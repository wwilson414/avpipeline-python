---
name: reconcile-worktree
description: Reconcile .github/ai state after a worktree branch is merged back.
disable-model-invocation: true
---
# Worktree Merge Reconciliation

Run under the lock: `.github/ai/scripts/ai-lock.sh acquire orchestrator`.

1. If `task-state.json` contains conflict markers (`<<<<<<<`), do not resolve blindly. Re-derive the authoritative state from the git history of both branches (`git log`, `git show <branch>:.github/ai/task-state.json`).
2. Compare the single `current_task` object from both branches. Never silently drop a task
   or choose between conflicting active-task records.
3. Append both branches' completed task entries to `.github/ai/archive/tasks-archive.md`, ordered by timestamp, not by branch.
4. If the same `task_id` has different `retry_count` values, do not pick one. List the mismatches in `handoff.md` Blockers and ask the human.
5. Merge `progress.md` events from both branches newest-first, then run `.github/ai/scripts/ai-rotate-progress.sh`.
6. Run `.github/ai/scripts/ai-validate.sh`, then `.github/ai/scripts/ai-lock.sh release`.
