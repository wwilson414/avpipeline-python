# Orchestrator Execution & Governance Protocol

This document defines the detailed scheduling, archiving, and worktree governance rules for the **Orchestrator** role.

## 1. Task Scheduling & Dispatching
- Schedule one active task at a time.
- Dispatch **Planner** when `.github/ai/plan.md` is missing or incomplete.
- Dispatch **Decomposer** when the task breakdown in `.github/ai/tasks.md` is missing.
- Dispatch the active execution agent (**Coder**), followed by the required validation/review roles (**Tester**, **Reviewer**, **Robustness Expert**, **Security Reviewer**).
- Stop for human direction when:
  - `retry_count` reaches `max_retries` (default: 3),
  - A task is `blocked`, or
  - High-risk work (e.g., destructive changes, dependency installation, architectural conflicts) requires approval.

## 2. Worktree & Parallel Session Governance (Agents Window Mode)
- Record active branch/worktree metadata in `.github/ai/task-state.json` when running in autonomous **Agents window** mode.
- When merging a worktree branch back or resolving `.github/ai` state conflicts, execute the **`reconcile-worktree`** skill (`.github/skills/reconcile-worktree/SKILL.md`).

## 3. Progress Log Rotation (Automated)
- `.github/ai/progress.md` maintains a strict **20-event active window** in reverse chronological order (`YYYY-MM-DD HH:MM`).
- **Do not manually count or move lines** in `progress.md`. After appending a new event (or at session end), run the rotation script to enforce the 20-event window and archive older entries automatically:
  ```bash
  bash .github/ai/scripts/ai-rotate-progress.sh
  ```

## 4. Milestone Closure Protocol
Once every task in the current milestone is `done`, execute the **`archive-milestone`** skill (`.github/skills/archive-milestone/SKILL.md`) before scheduling the next milestone.

