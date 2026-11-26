#!/usr/bin/env bash
# Keep the newest N events in progress.md; move displaced events to the
# single running archive. Idempotent: re-running after a crash does not
# duplicate archived events (compared by full event text, not by title).
# Location: <repo>/.github/ai/scripts/ai-rotate-progress.sh
# Usage: ai-rotate-progress.sh [keep=20]   (run inside the orchestrator lock)
#
# Archive order: newest first, same as progress.md. Displaced events are
# inserted right after the archive header, above older archived events.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
cd "$ROOT"
PROGRESS="$ROOT/.github/ai/progress.md"
ARCHIVE="$ROOT/.github/ai/archive/progress-archive.md"
KEEP="${1:-20}"

case "$KEEP" in ''|*[!0-9]*) echo "keep must be a positive integer" >&2; exit 2 ;; esac
[ "$KEEP" -ge 1 ] || { echo "keep must be >= 1" >&2; exit 2; }
[ -f "$PROGRESS" ] || { echo "progress.md not found: $PROGRESS" >&2; exit 2; }

PY=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "python not found (need python3)" >&2; exit 2; }

mkdir -p "$(dirname "$ARCHIVE")"

"$PY" - "$PROGRESS" "$ARCHIVE" "$KEEP" <<'PY'
import os, re, sys, tempfile

progress, archive, keep = sys.argv[1], sys.argv[2], int(sys.argv[3])

def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()

def atomic_write(path, content):
    mode = os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o644
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

# An event starts with: ## YYYY-MM-DD HH:MM - title
pat = re.compile(r"(?m)^## (?=\d{4}-\d{2}-\d{2} \d{2}:\d{2} - \S)")
legacy_pat = re.compile(r"(?m)^[-*] \*\*\d{4}-\d{2}-\d{2} \d{2}:\d{2}\*\*:")
stamp_pat = re.compile(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) - ")

def split_events(text):
    """Return (head, [event_text, ...], footer); each event ends with a blank line."""
    starts = [m.start() for m in pat.finditer(text)]
    if not starts:
        return text, [], ""
    first = starts[0]
    footer_at = text.find("\n<!-- End of active progress window. -->", first)
    end = footer_at if footer_at >= 0 else len(text)
    starts = [m.start() for m in pat.finditer(text[:end])]
    bounds = starts + [end]
    evs = [text[a:b].strip("\n") + "\n\n" for a, b in zip(bounds, bounds[1:])]
    footer = text[footer_at:].lstrip("\n") if footer_at >= 0 else ""
    return text[:first], evs, footer

text = read(progress)
if legacy_pat.search(text):
    raise SystemExit(
        "progress.md contains legacy dated bullet events; "
        "migrate them to '## YYYY-MM-DD HH:MM - title' first"
    )
invalid = [
    line for line in text.splitlines()
    if line.startswith("## ")
    and not pat.match(line)
]
if invalid:
    raise SystemExit(f"progress.md contains non-event level-2 heading: {invalid[0]}")

head, events, footer = split_events(text)

if len(events) <= keep:
    print(f"progress.md has {len(events)} events (<= {keep}); nothing to rotate")
    sys.exit(0)

kept, displaced = events[:keep], events[keep:]

# 1) archive first. Dedupe by full event text so that two different events
#    in the same minute are never confused with each other.
arch_text = read(archive) if os.path.exists(archive) else ""
arch_head, arch_events, _ = split_events(arch_text)
if not arch_events:
    raise SystemExit(
        "progress-archive.md has no canonical progress events; "
        "migrate the archive before rotating"
    )
seen = {e.strip() for e in arch_events}
new = [e for e in displaced if e.strip() not in seen]
if new:
    merged = new + arch_events
    merged.sort(key=lambda event: stamp_pat.match(event).group(1), reverse=True)
    atomic_write(archive, arch_head + "".join(merged))

# 2) atomically rewrite progress.md (only after the archive is safely written)
atomic_write(
    progress,
    head + "".join(kept).rstrip("\n") + "\n\n" + footer
)

print(f"rotated: kept {len(kept)}, archived {len(new)} "
      f"(skipped {len(displaced) - len(new)} duplicates) -> {archive}")
PY