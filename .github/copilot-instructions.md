# Copilot Instructions — AI Agent Harness (Template Repository)

This repository is the **upstream generic template** for a VS Code / GitHub Copilot multi-agent workflow. It is synced into downstream projects' `.github/` directories.

## 1. Repository Purpose & Structure

Maintain strict separation of responsibilities across files:

- `agents/*.agent.md`: Role definitions. Preserve least-privilege tool boundaries (e.g., Reviewer, Tester, Security Reviewer, Robustness Expert must NOT have `edit`; Planner and Decomposer must NOT have `execute`).
- `skills/*/SKILL.md`: Step-by-step deterministic transaction procedures (e.g., `archive-milestone`, `reconcile-worktree`).
- `instructions/*.instructions.md`: Auto-injected rules. Keep `applyTo` globs as narrow as possible to avoid context bloat.
- `orchestrator-protocol.md`: Orchestrator-only scheduling, worktree governance, and transaction triggers. **Do NOT rename to `*.instructions.md`** (must not be auto-injected into non-Orchestrator agents).
- `ai/scripts/`: Deterministic Bash/Python scripts for locking (`ai-lock.sh`), progress rotation (`ai-rotate-progress.sh`), and state validation.
- `ai/schema/`: JSON schemas for `task-state.json` and related state files.

## 2. Core Design Invariants (Do Not Break)

When editing any file in this repository, preserve these architectural rules:

1. **Genericity**: Never hardcode project-specific tech stacks, build commands, or business paths into generic files. Project-specific commands belong in downstream `project-stack.instructions.md`.
2. **No Procedure Duplication**: `orchestrator-protocol.md` defines *when* to trigger a transaction; `SKILL.md` defines *how* to execute it. Do not duplicate Skill steps inside protocols or instructions.
3. **Single-Writer & Retry Caps**: Keep single-writer ownership for `handoff.md`, `progress.md`, and `task-state.json`, and preserve `retry_count` / `max_retries` enforcement.
4. **Deterministic State Mutations**: State rotation (`progress.md` 20-entry cap) and milestone archiving must be performed by scripts, never by manual LLM editing.
5. **Conditional Gating**: Downstream `copilot-instructions.md` must only load `.github/ai/` state files during formal multi-agent workflows or when `TASK-*` / `.github/ai/` is explicitly referenced.

## 3. Script & Schema Editing Rules

- **Atomicity**: Any script modifying state files (`task-state.json`, `progress.md`, `progress-archive.md`) must use atomic writes (write to temp file + `fsync` + `os.replace` or `mv`) and respect `ai-lock.sh`.
- **Idempotency & Strict Validation**: Preserve idempotent deduplication and fail-fast validation on malformed headers or schemas.
- **Portability**: Scripts must work in standard Linux/WSL/macOS Bash environments (used via VS Code Remote / local terminals) and resolve paths relative to the downstream project root (`$PWD/.github/ai/`) or script location (`SCRIPT_DIR`).

## 4. Verification Before Finishing Changes

- For shell scripts (`.sh`): run `bash -n <script>` (and `shellcheck <script>` if available).
- For Python helpers / inline Python: verify syntax and edge cases (e.g., empty files, missing archive files, lock contention).
- For JSON schemas or templates: ensure valid JSON formatting.
