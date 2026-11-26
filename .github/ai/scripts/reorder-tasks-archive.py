#!/usr/bin/env python3
# One-off: reorder .github/ai/archive/tasks-archive.md newest-first.
# Usage: python3 reorder-tasks-archive.py .github/ai/archive/tasks-archive.md
import os, re, sys, tempfile

if len(sys.argv) != 2:
    sys.exit("usage: reorder-tasks-archive.py <tasks-archive.md>")

path = sys.argv[1]
text = open(path, encoding="utf-8").read()

FM = "---\nrole: archive\n---\n"
i = text.find(FM)
if i < 0:
    sys.exit("front matter not found")
before, rest = text[:i], text[i:]

def split(t):
    return re.split(r"(?m)^(?=## )", t)

bp = split(before)
if bp[0].strip():
    sys.exit("unexpected text before first section in the misplaced block")
rp = split(rest)
header, secs = rp[0], rp[1:] + bp[1:]

policy, sections_to_sort = "", []
for s in secs:
    h = s.split("\n", 1)[0]
    if h.strip().lower() == "## archive policy":
        policy = s
        continue
    sections_to_sort.append(s)

def milestone_rank(heading):
    if heading.strip() == "## Archived Milestones":
        return 10.0
    range_match = re.search(r"\bM(\d+)-M(\d+)\b", heading)
    if range_match:
        return float(range_match.group(2))
    match = re.search(r"\bM(\d+(?:\.\d+)?)\b", heading)
    if match:
        return float(match.group(1))
    raise SystemExit(f"unmatched heading: {heading}")

body = [
    (milestone_rank(s.split("\n", 1)[0]), index, s)
    for index, s in enumerate(sections_to_sort)
]
body.sort(key=lambda item: (-item[0], item[1]))

def fix_heading(s):
    return re.sub(r"(?m)^## (?:Archived )?Milestone (M[\d.]+):", r"## Archived \1:", s, count=1)

sections = []
for _, _, s in body:
    s = fix_heading(s)
    s = s.replace("> M29 is the active milestone under ADR-050.",
                  "> M29 was the active milestone under ADR-050 (closed).")
    sections.append(s.strip("\n") + "\n\n")

header = header.replace("# Archived Tasks: M1-M10", "# Archived Tasks")
header = header.replace("> The active task file has no milestones after M13 closure.\n", "")
policy = policy.strip("\n") + (
    "\n\n- Order: newest milestone first (by closing time). Insert new sections "
    "immediately below the insert-point marker, never at the end.\n\n")
marker = ("<!-- ARCHIVE-INSERT-POINT: newest first. "
          "Insert new `## Archived Mxx:` sections immediately below this line. -->\n\n")

out = header.rstrip("\n") + "\n\n" + policy + marker + "".join(sections)
out = out.rstrip("\n") + "\n"

rows = lambda t: len(re.findall(r"(?m)^\| (?:TASK-|T\d)", t))
if rows(text) != rows(out):
    sys.exit(f"row count mismatch: {rows(text)} -> {rows(out)}; file not changed")

fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))
with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
    f.write(out); f.flush(); os.fsync(f.fileno())
os.replace(tmp, path)
print(f"ok: {len(sections)} sections reordered, {rows(out)} task rows preserved")
