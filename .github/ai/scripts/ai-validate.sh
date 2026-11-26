#!/usr/bin/env bash
# Validate .github/ai workflow records. Exit 1 on any error.
# Run inside the orchestrator lock, after writing records and before release.
# Matches task-state.schema.json 1.1.0.
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
import json, os, re, sys, glob

AI = ".github/ai"
MAX_PROGRESS_EVENTS = 20   # keep in sync with ai-rotate-progress.sh default
MAX_SESSION_HISTORY = 5    # keep in sync with task-state.schema.json
SCHEMA_VERSION = "1.1.0"

errors, warns = [], []
err = errors.append

def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()

def fences_balanced(text):
    open_f = None  # (char, length)
    for line in text.splitlines():
        m = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if not m:
            continue
        run, rest = m.group(1), m.group(2)
        ch, n = run[0], len(run)
        if open_f is None:
            if ch == "`" and "`" in rest:
                continue  # inline code, not a fence
            open_f = (ch, n)
        elif ch == open_f[0] and n >= open_f[1] and rest.strip() == "":
            open_f = None
    return open_f is None

# ---- task-state.json
ts_path = f"{AI}/task-state.json"
state = None
try:
    state = json.loads(read(ts_path))
except FileNotFoundError:
    err(f"{ts_path}: file not found")
except Exception as e:
    err(f"{ts_path}: invalid JSON: {e}")

if isinstance(state, dict):
    # 1) JSON Schema (optional dependency)
    schema_path = f"{AI}/schema/task-state.schema.json"
    if os.path.exists(schema_path):
        try:
            import jsonschema
            jsonschema.validate(state, json.loads(read(schema_path)))
        except ImportError:
            warns.append("jsonschema not installed; schema check skipped (pip install jsonschema)")
        except Exception as e:
            err(f"{ts_path}: schema violation: {getattr(e, 'message', e)}")
    else:
        warns.append(f"{schema_path} not found; schema check skipped")

    # 2) Checks that work even without jsonschema
    if state.get("schema_version") != SCHEMA_VERSION:
        err(f"{ts_path}: schema_version must be {SCHEMA_VERSION}, got {state.get('schema_version')!r} (migrate state file)")

    cur = state.get("current_task")
    gstatus = state.get("status")

    if cur is not None and not isinstance(cur, dict):
        err(f"{ts_path}: current_task must be an object or null")
    elif isinstance(cur, dict):
        rc, mr = cur.get("retry_count", 0), cur.get("max_retries", 3)
        if isinstance(rc, int) and isinstance(mr, int) and rc > mr:
            err(f"{ts_path}: current_task {cur.get('id')} retry_count {rc} > max_retries {mr}")
        if cur.get("id") in (cur.get("depends_on") or []):
            err(f"{ts_path}: current_task {cur.get('id')} depends on itself")
        if gstatus == "blocked" and cur.get("status") not in ("blocked", "in-progress", "review"):
            warns.append("global status is blocked but current_task is not in a blocked/active state")

    if gstatus == "complete" and cur is not None:
        err(f"{ts_path}: status is 'complete' but current_task is not null")

    # 3) session_history: newest first, bounded
    hist = state.get("session_history") or []
    if isinstance(hist, list):
        if len(hist) > MAX_SESSION_HISTORY:
            err(f"{ts_path}: session_history has {len(hist)} entries (> {MAX_SESSION_HISTORY}); drop the oldest")
        stamps = [h.get("timestamp", "") for h in hist if isinstance(h, dict)]
        if any(a < b for a, b in zip(stamps, stamps[1:])):
            err(f"{ts_path}: session_history is not newest-first")

    # 4) session metadata
    sess = state.get("session") or {}
    if not (isinstance(sess, dict) and sess.get("task_id_prefix")):
        warns.append("session.task_id_prefix is not set; Decomposer cannot create task IDs")

    # 5) archive pointers must exist and use the canonical paths
    arch = state.get("archive") or {}
    if isinstance(arch, dict):
        expected_archives = {
            "progress_archive": f"{AI}/archive/progress-archive.md",
            "tasks_archive": f"{AI}/archive/tasks-archive.md",
        }
        for k, expected in expected_archives.items():
            v = arch.get(k)
            if isinstance(v, str) and v and not os.path.exists(v):
                err(f"archive pointer '{k}' -> {v} does not exist")
            if v != expected:
                err(f"archive.{k} must be {expected}, got {v!r}")

# ---- progress.md: bounded, newest first
event_re = re.compile(r"^## \d{4}-\d{2}-\d{2} \d{2}:\d{2} - \S.*$")
stamp_re = re.compile(r"(?m)^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) - ")
legacy_event_re = re.compile(r"^[-*] \*\*\d{4}-\d{2}-\d{2} \d{2}:\d{2}\*\*:")
prog = f"{AI}/progress.md"
if os.path.exists(prog):
    progress_text = read(prog)
    progress_lines = progress_text.splitlines()
    invalid_headings = [
        line for line in progress_lines
        if line.startswith("## ") and not event_re.match(line)
    ]
    if invalid_headings:
        err(f"{prog}: non-event level-2 heading '{invalid_headings[0]}'")
    if any(legacy_event_re.match(line) for line in progress_lines):
        err(f"{prog}: legacy dated bullet events found; use '## YYYY-MM-DD HH:MM - title'")
    stamps = [
        match.group(1)
        for line in progress_lines
        if (match := stamp_re.match(line))
    ]
    if len(stamps) > MAX_PROGRESS_EVENTS:
        err(f"{prog}: {len(stamps)} events (> {MAX_PROGRESS_EVENTS}); run .github/ai/scripts/ai-rotate-progress.sh")
    if any(a < b for a, b in zip(stamps, stamps[1:])):
        err(f"{prog}: events are not in reverse chronological order")

# ---- progress archive: canonical event format and newest first
progress_archive = f"{AI}/archive/progress-archive.md"
if os.path.exists(progress_archive):
    archive_text = read(progress_archive)
    if legacy_event_re.search(archive_text):
        err(f"{progress_archive}: legacy dated bullet events found")
    archive_stamps = [
        match.group(1)
        for match in stamp_re.finditer(archive_text)
    ]
    if any(a < b for a, b in zip(archive_stamps, archive_stamps[1:])):
        err(f"{progress_archive}: events are not in reverse chronological order")

# ---- handoff.md: one canonical current handoff
ho = f"{AI}/handoff.md"
if os.path.exists(ho):
    handoff_text = read(ho)
    handoff_lines = handoff_text.splitlines()
    if not handoff_lines or handoff_lines[0].strip() != "# Current Handoff":
        err(f"{ho}: first line must be '# Current Handoff' (no front matter or preamble)")
    n = len(re.findall(r"(?m)^# Current Handoff\s*$", handoff_text))
    if n != 1:
        err(f"{ho}: expected exactly one '# Current Handoff' heading, found {n}")
    sections = re.findall(r"(?m)^## (.+?)\s*$", handoff_text)
    required_sections = ["Goal", "Execution State", "Blockers", "Next Step"]
    for section in required_sections:
        if sections.count(section) != 1:
            err(f"{ho}: expected exactly one '## {section}' section")
    unexpected_sections = [
        section for section in sections if section not in required_sections
    ]
    if unexpected_sections:
        err(f"{ho}: unexpected section(s): {', '.join(unexpected_sections)}")

# ---- plan.md / tasks.md: no history or session logs
for name in ("plan.md", "tasks.md"):
    p = f"{AI}/{name}"
    if os.path.exists(p) and re.search(
            r"(?im)^#{1,6}\s*(change history|session log|changelog)\b", read(p)):
        err(f"{p}: contains change-history/session-log section")

# ---- workflow md (archive excluded): balanced fences, no conflict markers
for p in glob.glob(f"{AI}/**/*.md", recursive=True):
    if p.replace("\\", "/").startswith(f"{AI}/archive/"):
        continue
    text = read(p)
    if not fences_balanced(text):
        err(f"{p}: unbalanced code fences")
    if re.search(r"(?m)^(<{7}|>{7}) ", text):
        err(f"{p}: merge conflict markers present")

for w in warns:
    print(f"WARN: {w}")
if errors:
    for e in errors:
        print(f"ERROR: {e}")
    sys.exit(1)
print("ai records OK")
PY
rc=$?

# Whitespace check limited to workflow records, not business code
if ! git diff --check -- .github/ai >/dev/null 2>&1; then
  echo "ERROR: git diff --check reports whitespace problems in .github/ai" >&2
  git diff --check -- .github/ai >&2 || true
  rc=1
fi

exit $rc