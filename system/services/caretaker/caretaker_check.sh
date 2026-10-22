#!/usr/bin/env bash
#
# caretaker_check.sh -- the Caretaker's deterministic weekly check. The
# Caretaker is the routine `caretaker` (system/routines/caretaker.toml), off by
# default until the user turns it on; the routines runner runs this script
# when the weekly check is due, and retries it if a check failed or was killed
# before completing. It wakes the Caretaker agent
# only when it finds something worth telling the user about -- handing the
# findings over via data/.state/caretaker/findings.md -- or when the agent has
# never introduced itself (no data/.state/caretaker/permissions.md yet).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
CARETAKER_DIR="$ROOT/data/.state/caretaker"

# `--only routines` looks at the other routines and nothing else: the shape a daily, lighter check takes.
ONLY=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --only)
            case "${2:-}" in
                routines) ONLY="$2"; shift 2 ;;
                *) echo "caretaker_check: --only takes 'routines', not '${2:-}'" >&2; exit 2 ;;
            esac
            ;;
        *) echo "caretaker_check: unknown argument: $1" >&2; exit 2 ;;
    esac
done

# Wake the Caretaker's chat with /caretaker through the routines library, which creates the chat on the
# first run, clears it on later ones, and (inside a run) records the chat on this run. The routine whose
# chat and run these are is the one running the check (the runner names it in the environment; outside a
# run the skill's name stands in), so a second definition over this script gets a chat and runs of its own.
wake_agent() {
    exec uv run --no-sync routines wake caretaker
}

log() { printf '%s caretaker_check: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# The findings file the agent reads, headed by what kind of check found them.
write_findings() {
    {
        printf '# %s (%s)\n\n' "$1" "$(date +'%Y-%m-%d %H:%M %Z')"
        printf '%s' "$FINDINGS"
    } > "$CARETAKER_DIR/findings.md"
}

mkdir -p "$CARETAKER_DIR"
MARKER="$CARETAKER_DIR/last_check"

# One-time introduction: the agent has never met the user (no permissions file
# yet), so wake it regardless of findings.
if [ ! -f "$CARETAKER_DIR/permissions.md" ]; then
    log "first run: waking the agent to introduce itself"
    touch "$MARKER"
    wake_agent
fi

FINDINGS=""
add_finding() { FINDINGS="${FINDINGS}- $1"$'\n'; }

# 5. Routines that are failing, overdue, broken, or waiting on the user (the routines library's own
# verdicts). The Caretaker leaves itself out: a check that follows a failed run of its own would
# otherwise wake the agent about the very routine it is.
if command -v uv >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then
    if routines_listing=$(uv run --no-sync routines list --format json 2>&1); then
        bad_routines=$(printf '%s' "$routines_listing" \
            | jq -r '.routines[] | select(.routine_id != "caretaker") | select(.health == "FAILING" or .health == "OVERDUE" or .health == "BROKEN" or .health == "NEEDS_USER") | "\(.title) (\(.health_text))"' 2>/dev/null \
            | tr '\n' ';' || true)
        [ -n "$bad_routines" ] && add_finding "routines needing attention: $bad_routines"
    else
        log "could not read the routines: $(printf '%s' "$routines_listing" | tail -n 1)"
    fi
fi

# The marker is the full check's baseline for findings 2 and 4 (what is new since the last weekly check), so
# the routines-only pass leaves it alone.
if [ "$ONLY" = "routines" ]; then
    if [ -z "$FINDINGS" ]; then
        log "all routines are fine; nothing to report"
        exit 0
    fi
    write_findings "What the routines check found"
    log "routine findings; waking the agent"
    wake_agent
fi

# 1. Services in a bad state (FATAL: crashed and gave up; BACKOFF: crash-looping).
if command -v supervisorctl >/dev/null 2>&1; then
    bad_services=$(supervisorctl status 2>/dev/null | awk '$2 == "FATAL" || $2 == "BACKOFF" {print $1 " (" $2 ")"}' || true)
    [ -n "$bad_services" ] && add_finding "services in a bad state: $(echo "$bad_services" | tr '\n' ' ')"
fi

# 2. Service logs with fresh error output since the last check (falls back to
# the last 7 days on the first post-introduction check, before a marker exists).
if [ -d /var/log/supervisor ]; then
    if [ -f "$MARKER" ]; then newer=(-newer "$MARKER"); else newer=(-mtime -7); fi
    error_logs=$(find /var/log/supervisor -name '*stderr*' "${newer[@]}" -size +0c 2>/dev/null \
        | xargs -r grep -liE 'error|traceback|exception' 2>/dev/null || true)
    [ -n "$error_logs" ] && add_finding "fresh error output in: $(echo "$error_logs" | tr '\n' ' ')"
fi

# 3. Disk nearly full. The env override exists for tests.
disk_threshold="${MINDS_CARETAKER_DISK_THRESHOLD:-85}"
disk_used=$(df -P / | awk 'NR==2 {gsub("%",""); print $5}')
[ "$disk_used" -ge "$disk_threshold" ] && add_finding "disk is ${disk_used}% full"

# 4. The OOM guard shed processes since the last check.
SHED="$ROOT/data/.state/oom_priority/events/shed.jsonl"
if [ -f "$SHED" ] && [ -s "$SHED" ] && { [ ! -f "$MARKER" ] || [ "$SHED" -nt "$MARKER" ]; }; then
    add_finding "the memory guard had to stop processes (see data/.state/oom_priority/events/shed.jsonl)"
fi

touch "$MARKER"
if [ -z "$FINDINGS" ]; then
    log "all clear; nothing to report until the next check"
    exit 0
fi

log "findings; waking the agent"
write_findings "What the weekly check found"
wake_agent
