# Project Tasks

> **Rules**:
> - **Task IDs**: Use sequential IDs (`TASK-001`, `TASK-002`, ...) matching `.github/ai/task-state.json`.
> - **Status Sync**: Keep task status synchronized with `task-state.json` (`todo` | `in-progress` | `in-review` | `blocked` | `done`).
> - **Decomposition**: `Decomposer` / `Planner` defines tasks and acceptance criteria; `Orchestrator` dispatches and updates execution status.

---

## Milestone M1: <Milestone Name>

### [ ] TASK-001: <Short Task Title>
- **Status**: `todo`
- **Priority**: `high` <!-- high | medium | low -->
- **Depends On**: `NONE` <!-- e.g., TASK-000 | NONE -->
- **Assigned Role**: `Coder` <!-- Coder | Tester | Reviewer | Security Reviewer | Robustness Expert -->
- **Scope / Files**:
  - `<path/to/file1>`
- **Acceptance Criteria**:
  - [ ] <Specific, verifiable outcome 1>
  - [ ] <Specific, verifiable outcome 2>
- **Verification**:
  - `<build/test command from project-stack.instructions.md>`

### [ ] TASK-002: <Short Task Title>
- **Status**: `todo`
- **Priority**: `medium`
- **Depends On**: `TASK-001`
- **Assigned Role**: `Tester`
- **Scope / Files**:
  - `<path/to/test_file>`
- **Acceptance Criteria**:
  - [ ] <Specific, verifiable outcome>
- **Verification**:
  - `<test command>`
