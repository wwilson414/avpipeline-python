#!/usr/bin/env bash
# Validate .github/ai/tasks.md: table structure, dependency graph, and
# consistency with task-state.json (current_task is authoritative for status).
#
# Section kinds are decided by the heading prefix:
#   "Deferred ..."    -> inactive, rows must have status 'deferred'
#   "Superseded ..."  -> inactive, rows must have status 'superseded'
#   anything else     -> active, full validation
# Inactive rows are not schedulable: they need no Priority column and are
# excluded from the dependency graph, but their IDs and executors are checked.
#
# Executors come from .github/agents/*.agent.md (file-name slug), minus
# NON_EXECUTORS, and the schema agent enum must match those files.
#
# Run inside the orchestrator lock. Exit 1 on any error.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
cd "$ROOT"

PY=""
for c in python3 python py; do
  command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
done
[ -n "$PY" ] || { echo "ERROR: python not found (need python3)" >&2; exit 2; }

"$PY" - <<'PY'
import glob, json, os, re, subprocess, sys

PATH = ".github/ai/tasks.md"
STATE_PATH = ".github/ai/task-state.json"
SCHEMA_PATH = ".github/ai/schema/task-state.schema.json"
AGENT_DIR = ".github/agents"
STATUS = {"pending", "in-progress", "review", "done", "blocked"}
ACTIVE = {"in-progress", "review"}
PRIO = {"P0": 0, "P1": 1, "P2": 2}
# Roles that never own a task row in tasks.md.
NON_EXECUTORS = {"orchestrator", "planner", "decomposer"}
ACTIVE_HEADER = ("ID", "Task", "Executor", "Priority", "Status", "Depends On", "Acceptance Gate")
INACTIVE_HEADER = ("ID", "Task", "Executor", "Status", "Depends On", "Acceptance Gate")
TASK_ID = re.compile(r"^(?:T[0-9]+(?:\.[0-9]+)*|TASK-[A-Za-z0-9][A-Za-z0-9_-]*-[0-9]+)$")

# Inactive section kinds: heading prefix -> the only status allowed in rows.
INACTIVE = {"deferred": "deferred", "superseded": "superseded"}

errors, warns = [], []
err = errors.append

def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()

# ---- agents / executors
SUFFIX = ".agent.md"
AGENTS = {
    os.path.basename(f)[: -len(SUFFIX)].lower()
    for f in glob.glob(f"{AGENT_DIR}/*{SUFFIX}")
}
EXEC = AGENTS - NON_EXECUTORS
if not AGENTS:
    err(f"no agent definitions found under {AGENT_DIR}/")
try:
    schema = json.loads(read(SCHEMA_PATH))
    schema_agents = set(schema["$defs"]["agent"]["enum"])
    if AGENTS and schema_agents != AGENTS:
        err(f"{SCHEMA_PATH}: agent enum {sorted(schema_agents)} differs from "
            f"{AGENT_DIR}/*{SUFFIX} {sorted(AGENTS)}")
except FileNotFoundError:
    warns.append(f"{SCHEMA_PATH} not found; agent enum check skipped")
except (KeyError, TypeError, ValueError) as e:
    err(f"{SCHEMA_PATH}: cannot read $defs.agent.enum: {e}")

try:
    text = read(PATH)
except FileNotFoundError:
    print(f"ERROR: {PATH} not found")
    sys.exit(1)

def split_row(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]

def section_kind(heading):
    """'deferred', 'superseded' or 'active', decided by the heading prefix."""
    h = heading.strip().lower()
    for kind in INACTIVE:
        if h.startswith(kind):
            return kind
    return "active"

def parse_tables(src):
    """Return [(section_heading, header, rows)] for each pipe table."""
    heading, lines, out, i = "", src.splitlines(), [], 0
    while i < len(lines):
        l = lines[i]
        if l.startswith("#"):
            heading = l.lstrip("# ").strip()
        if l.lstrip().startswith("|") and i + 1 < len(lines) \
           and re.match(r"^\s*\|[\s:\-|]+\|\s*$", lines[i + 1]):
            header = split_row(l)
            j, rows = i + 2, []
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                rows.append((j + 1, split_row(lines[j])))
                j += 1
            out.append((heading, header, rows))
            i = j
            continue
        i += 1
    return out

def active_rows(src):
    res = {}
    for heading, header, rows in parse_tables(src):
        if section_kind(heading) != "active" or "Status" not in header:
            continue
        for ln, cells in rows:
            if len(cells) == len(header):
                row = dict(zip(header, cells))
                res[row.get("ID", cells[0])] = row
    return res

# ---- parse tasks
tasks = {}      # active, schedulable rows
inactive = {}   # id -> (kind, line)  deferred / superseded rows
for heading, header, rows in parse_tables(text):
    kind = section_kind(heading)
    is_inactive = kind != "active"
    if "Status" not in header and not is_inactive:
        continue
    if len(set(header)) != len(header):
        err(f"{PATH}: table '{heading}' has duplicate column names")
        continue
    expected_header = INACTIVE_HEADER if is_inactive else ACTIVE_HEADER
    if tuple(header) != expected_header:
        err(f"{PATH}: {kind} table '{heading}' must use columns "
            f"{list(expected_header)} in this order (found {header})")
        continue
    for ln, cells in rows:
        if len(cells) != len(header):
            err(f"{PATH}:{ln}: {len(cells)} cells, header has {len(header)}")
            continue
        row = dict(zip(header, cells))
        tid = row["ID"]
        if not TASK_ID.match(tid):
            err(f"{PATH}:{ln}: invalid task ID '{tid}'")
        if not row["Task"]:
            err(f"{PATH}:{ln}: {tid}: task title must not be empty")
        if not row["Acceptance Gate"]:
            err(f"{PATH}:{ln}: {tid}: Acceptance Gate must not be empty")
        if tid in tasks or tid in inactive:
            err(f"{PATH}:{ln}: duplicate task ID {tid}")
        if row["Executor"] not in EXEC:
            err(f"{PATH}:{ln}: {tid}: invalid executor '{row['Executor']}' "
                f"(allowed: {sorted(EXEC)})")
        if is_inactive:
            want = INACTIVE[kind]
            if row["Status"] != want:
                err(f"{PATH}:{ln}: {kind} row {tid} must have status '{want}' "
                    f"(found '{row['Status']}'); move it to the matching section")
            inactive[tid] = (kind, ln)
            continue
        if row["Status"] not in STATUS:
            err(f"{PATH}:{ln}: {tid}: invalid status '{row['Status']}'")
        if row["Priority"] not in PRIO:
            err(f"{PATH}:{ln}: {tid}: invalid priority '{row['Priority']}'")
        tasks[tid] = row

# ---- dependencies
def deps(row):
    d = row.get("Depends On", "None").strip()
    if d.lower() in ("none", "", "-", "n/a"):
        return []
    return [x.strip() for x in re.split(r"[,\s]+", d) if x.strip()]

graph = {}
for tid, row in tasks.items():
    graph[tid] = deps(row)
    for d in graph[tid]:
        if d == tid:
            err(f"{tid}: depends on itself")
        elif d in inactive:
            err(f"{tid}: depends on {inactive[d][0]} task '{d}' "
                f"(not schedulable; remove or replace the dependency)")
        elif d not in tasks:
            err(f"{tid}: depends on unknown task '{d}'")

# cycle detection (iterative, no recursion limit)
WHITE, GREY, BLACK = 0, 1, 2
color = {n: WHITE for n in graph}
for start in graph:
    if color[start] != WHITE:
        continue
    stack = [(start, iter(graph[start]))]
    path = [start]
    color[start] = GREY
    while stack:
        node, it = stack[-1]
        for m in it:
            if m not in graph:
                continue
            if color[m] == GREY:
                err("dependency cycle: " + " -> ".join(path[path.index(m):] + [m]))
            elif color[m] == WHITE:
                color[m] = GREY
                stack.append((m, iter(graph[m])))
                path.append(m)
                break
        else:
            color[node] = BLACK
            stack.pop()
            path.pop()

# ---- priority ordering
for tid, row in tasks.items():
    p = PRIO.get(row.get("Priority"))
    if p is None:
        continue
    for d in graph[tid]:
        dp = PRIO.get(tasks.get(d, {}).get("Priority"))
        if dp is not None and dp > p and "Blocker:" not in row.get("Acceptance Gate", ""):
            err(f"{tid} ({row['Priority']}) depends on lower-priority {d} "
                f"({tasks[d]['Priority']}) without 'Blocker:' in its Acceptance Gate")

# ---- a started/finished task must not have unfinished dependencies
for tid, row in tasks.items():
    if row.get("Status") in ACTIVE or row.get("Status") == "done":
        for d in graph[tid]:
            if d in tasks and tasks[d]["Status"] != "done":
                err(f"{tid} is '{row['Status']}' but dependency {d} is '{tasks[d]['Status']}'")

# ---- only one active task in tasks.md
active_ids = [t for t, r in tasks.items() if r.get("Status") in ACTIVE]
if len(active_ids) > 1:
    err(f"more than one in-progress/review task in tasks.md: {active_ids}")

# ---- consistency with task-state.json
state = None
try:
    state = json.loads(read(STATE_PATH))
except FileNotFoundError:
    warns.append(f"{STATE_PATH} not found; consistency check skipped")
except Exception as e:
    err(f"{STATE_PATH}: invalid JSON: {e}")

if isinstance(state, dict):
    cur = state.get("current_task")
    if cur is None:
        if active_ids:
            err(f"current_task is null but tasks.md has active task(s): {active_ids}")
    elif isinstance(cur, dict):
        cid = cur.get("id")
        if cid not in tasks:
            err(f"current_task {cid} not found in tasks.md active tables")
        else:
            row = tasks[cid]
            if row["Status"] != cur.get("status"):
                err(f"{cid}: tasks.md status '{row['Status']}' != task-state.json "
                    f"'{cur.get('status')}' (task-state.json is authoritative)")
            if sorted(graph[cid]) != sorted(cur.get("depends_on") or []):
                err(f"{cid}: depends_on differs: tasks.md {sorted(graph[cid])} vs "
                    f"task-state.json {sorted(cur.get('depends_on') or [])}")
            others = [t for t in active_ids if t != cid]
            if others:
                err(f"tasks.md has other in-progress/review tasks besides {cid}: {others}")

# ---- no illegal status regressions vs git HEAD
old = None
try:
    old = subprocess.run(["git", "show", f"HEAD:{PATH}"], capture_output=True,
                         text=True, check=True).stdout
except Exception:
    pass  # new file or no git history
if old is not None:
    handoff = read(".github/ai/handoff.md")
    approved_reopens = set(re.findall(
        r"(?m)^\s*-\s*\*\*Human-Approved Reopen\*\*:\s*"
        r"(TASK-[A-Za-z0-9][A-Za-z0-9_-]*-[0-9]+)\s*(?:—|$)",
        handoff,
    ))
    current = state.get("current_task") if isinstance(state, dict) else None
    current_id = current.get("id") if isinstance(current, dict) else None
    for tid, orow in active_rows(old).items():
        if tid not in tasks:
            continue  # removed, archived or moved to an inactive section
        before, after = orow.get("Status"), tasks[tid]["Status"]
        if before == "done" and after != "done":
            if tid == current_id and tid in approved_reopens:
                warns.append(f"{tid}: reopening authorized in current handoff")
                continue
            err(f"{tid}: reopened from 'done' to '{after}' (needs human approval)")
        elif before == "blocked" and after == "pending":
            err(f"{tid}: 'blocked' -> 'pending' is not allowed; unblock to 'in-progress'")

for w in warns:
    print("WARN:", w)
if errors:
    for e in errors:
        print("ERROR:", e)
    sys.exit(1)
print(f"tasks.md OK ({len(tasks)} active, {len(inactive)} inactive tasks)")
PY