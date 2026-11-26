#!/usr/bin/env bash
# Atomic lock for .github/ai workflow records (mkdir-based).
# Location: <repo>/.github/ai/scripts/ai-lock.sh
#
# Usage:
#   ai-lock.sh acquire <owner>
#   ai-lock.sh release <owner>
#   ai-lock.sh release --force
#   ai-lock.sh status
#
# Env:
#   AI_LOCK_TTL  seconds before a lock is considered stale (default 900)
#
# Exit codes: 0 ok | 1 locked or refused | 2 usage error
#
# Conventions:
#   - Hold the lock only while writing records:
#       acquire -> write files -> ai-validate*.sh -> release
#   - Do NOT hold the lock while a sub-agent is running (would exceed TTL).
#   - Always release with the same <owner> used to acquire (use "orchestrator").
#   - The lock is per worktree: each worktree has its own .github/ai/.lock.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
cd "$ROOT"
LOCK="$ROOT/.github/ai/.lock"
INFO="$LOCK/info"
TTL="${AI_LOCK_TTL:-900}"

usage() {
  echo "usage: $0 acquire <owner> | release <owner> | release --force | status" >&2
  exit 2
}

cmd="${1:-status}"
owner="${2:-}"

case "$TTL" in
  ''|*[!0-9]*) echo "AI_LOCK_TTL must be a non-negative integer" >&2; exit 2 ;;
esac

case "$cmd" in
  acquire|release) [ -n "$owner" ] || usage ;;
  status) ;;
  *) usage ;;
esac

if [ -n "$owner" ] && [ "$owner" != "--force" ]; then
  case "$owner" in
    *[!A-Za-z0-9_.-]*) echo "owner must match [A-Za-z0-9_.-]+" >&2; exit 2 ;;
  esac
fi
if [ "$owner" = "--force" ] && [ "$cmd" = "acquire" ]; then
  echo "--force is only valid with release" >&2; exit 2
fi

now="$(date +%s)"

# mtime of a path, portable across GNU and BSD stat; falls back to "now" (age 0)
mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo "$now"; }

info_line() { head -n1 "$INFO" 2>/dev/null || true; }

lock_age() {
  local ts=""
  if [ -f "$INFO" ]; then
    ts="$(info_line | awk '{print $1}')"
  fi
  case "$ts" in ''|*[!0-9]*) ts="$(mtime "$LOCK")" ;; esac
  echo $(( now - ts ))
}

try_acquire() {
  mkdir -p "$(dirname "$LOCK")"
  if mkdir "$LOCK" 2>/dev/null; then
    # write info to a temp file, then move into place (no half-written info)
    local tmp="$LOCK/.info.$$"
    echo "$now $owner pid=$$ $(date '+%Y-%m-%d %H:%M')" > "$tmp"
    mv "$tmp" "$INFO"
    return 0
  fi
  return 1
}

# Move the stale lock away atomically, then verify it was the one we judged
# stale. If a fresh lock was moved by mistake, restore it only if the slot is
# still free (mv onto an existing directory would nest instead of failing).
break_stale() {
  local seen="$1"
  local grave="$LOCK.stale.$$"
  rm -rf "$grave"
  mv "$LOCK" "$grave" 2>/dev/null || return 1
  local moved
  moved="$(head -n1 "$grave/info" 2>/dev/null || true)"
  if [ "$moved" != "$seen" ]; then
    if [ ! -e "$LOCK" ] && mv "$grave" "$LOCK" 2>/dev/null; then
      return 1
    fi
    rm -rf "$grave"
    return 1
  fi
  rm -rf "$grave"
  return 0
}

case "$cmd" in
  acquire)
    if try_acquire; then echo "lock acquired by $owner"; exit 0; fi
    age="$(lock_age)"
    if [ "$age" -gt "$TTL" ]; then
      seen="$(info_line)"
      echo "stale lock (${age}s > ${TTL}s), breaking it" >&2
      break_stale "$seen" || true
      if try_acquire; then echo "lock acquired by $owner"; exit 0; fi
    fi
    echo "LOCKED: held for ${age}s -> $(cat "$INFO" 2>/dev/null || echo unknown). Abort; do not overwrite." >&2
    exit 1
    ;;

  release)
    if [ ! -e "$LOCK" ]; then
      echo "WARN: lock was not held (expired or already released)" >&2
      exit 0
    fi
    if [ "$owner" != "--force" ]; then
      if [ ! -f "$INFO" ]; then
        echo "lock has no info (incomplete); use 'release --force' or wait for TTL" >&2
        exit 1
      fi
      holder="$(info_line | awk '{print $2}')"
      if [ "$holder" != "$owner" ]; then
        echo "lock is held by '$holder', not '$owner'. Use: release <owner> or release --force" >&2
        exit 1
      fi
      # re-check right before deleting to narrow the check-then-delete window
      if [ "$(info_line | awk '{print $2}')" != "$owner" ]; then
        echo "lock changed hands during release; not removing" >&2
        exit 1
      fi
    fi
    rm -rf "$LOCK"
    echo "lock released"
    ;;

  status)
    if [ -e "$LOCK" ]; then
      echo "locked (TTL ${TTL}s, age $(lock_age)s): $(cat "$INFO" 2>/dev/null || echo unknown)"
      exit 1
    fi
    echo "free"
    ;;
esac