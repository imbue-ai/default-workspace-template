#!/usr/bin/env bash
# Enable one or more workspace feature flags for the system-interface app.
#
#   system/scripts/enable_workspace_feature_flag.sh FEATURE_FLAG_ENABLE_CODEX [...]
#
# A feature flag is an env var the system-interface process reads at startup, so
# turning one on means editing its supervisord `environment=` line and restarting
# it -- the flag is read once, not per request.
#
# Flags already enabled are reported and skipped; if every flag named is already
# on, the script exits without touching the config or bouncing the service (a
# restart drops every connected browser's websocket, so it is not free). Existing
# entries on the `environment=` line are preserved -- enabling a second flag never
# clobbers the first.
#
# Idempotent, and safe to run from any directory.
set -euo pipefail

readonly SUPERVISORD_CONF="/home/user/workspace/system/supervisord.conf"
readonly PROGRAM="system_interface"
readonly API_BASE="http://127.0.0.1:8000"
# How long to wait for the restarted service to answer HTTP again: attempts x
# interval, so 30 x 0.5s = 15s.
readonly SERVE_WAIT_ATTEMPTS=30
readonly SERVE_WAIT_INTERVAL_SECONDS=0.5
# Grace for browsers to re-establish their websockets after the restart, before
# the one-shot reload broadcast goes out.
readonly CLIENT_RECONNECT_SETTLE_SECONDS=2
# Flags are env vars, so accept only names that can BE env vars.
readonly FLAG_NAME_PATTERN='^[A-Za-z_][A-Za-z0-9_]*$'

usage() {
    cat >&2 <<'EOF'
usage: enable_workspace_feature_flag.sh FLAG [FLAG ...]

Enable workspace feature flags for the system-interface app and restart it.

  FLAG    an env-var name, e.g. FEATURE_FLAG_ENABLE_CODEX

examples:
  enable_workspace_feature_flag.sh FEATURE_FLAG_ENABLE_CODEX
  enable_workspace_feature_flag.sh FEATURE_FLAG_ENABLE_CODEX FEATURE_FLAG_ENABLE_FOO
EOF
    exit 2
}

[ "$#" -ge 1 ] || usage
for arg in "$@"; do
    case "$arg" in
        -h | --help) usage ;;
    esac
    if ! printf '%s' "$arg" | grep -qE "$FLAG_NAME_PATTERN"; then
        echo "error: ${arg@Q} is not a valid env-var name" >&2
        exit 2
    fi
done

if [ ! -f "$SUPERVISORD_CONF" ]; then
    echo "error: $SUPERVISORD_CONF not found -- run this inside the workspace" >&2
    exit 1
fi
if ! command -v supervisorctl >/dev/null 2>&1; then
    echo "error: supervisorctl not found -- run this inside the workspace" >&2
    exit 1
fi

# The program's current `environment=` value (empty if the line is absent). Read
# only from within the [program:<PROGRAM>] section so another program's
# environment line can never be picked up.
current_environment() {
    awk -v program="[program:${PROGRAM}]" '
        $0 == program { in_section = 1; next }
        /^\[/         { in_section = 0 }
        in_section && /^environment[[:space:]]*=/ {
            sub(/^environment[[:space:]]*=[[:space:]]*/, "")
            print
            exit
        }
    ' "$SUPERVISORD_CONF"
}

existing="$(current_environment)"

# --- Step 0: report what is already on, and work out what is left to do -------
to_add=()
for flag in "$@"; do
    # Match KEY="1" / KEY=1 at an entry boundary (start of line or after a comma).
    if printf '%s' "$existing" | grep -qE "(^|,)[[:space:]]*${flag}[[:space:]]*=[[:space:]]*\"?1\"?([[:space:]]*,|[[:space:]]*$)"; then
        echo "already enabled: $flag"
    else
        to_add+=("$flag")
    fi
done

if [ "${#to_add[@]}" -eq 0 ]; then
    echo
    echo "Nothing to do -- every flag named is already enabled. Not restarting $PROGRAM."
    exit 0
fi

echo
echo "enabling: ${to_add[*]}"

# --- Step 1: merge the flags into [program:system_interface] -----------------
merged="$existing"
for flag in "${to_add[@]}"; do
    # A flag present with some other value is replaced rather than duplicated;
    # supervisord would otherwise honour whichever entry it parsed last.
    merged="$(printf '%s' "$merged" | sed -E "s/(^|,)[[:space:]]*${flag}[[:space:]]*=[^,]*//g")"
    merged="$(printf '%s' "$merged" | sed -E 's/^,+//; s/,+$//; s/,,+/,/g')"
    if [ -n "$merged" ]; then
        merged="${merged},${flag}=\"1\""
    else
        merged="${flag}=\"1\""
    fi
done

backup="${SUPERVISORD_CONF}.bak.$(date +%Y%m%d%H%M%S)"
cp "$SUPERVISORD_CONF" "$backup"
echo "backed up config to $backup"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
# Replace the section's existing environment line in place, or -- when it has
# none -- insert one directly under the section header. Appending it at the end
# of the section would also parse, but a line sitting after the section's blank
# separator reads as if it belongs to the NEXT program.
awk -v program="[program:${PROGRAM}]" -v value="$merged" -v has_line="$([ -n "$existing" ] && echo 1 || echo 0)" '
    $0 == program {
        print
        if (has_line == 0) { print "environment=" value }
        in_section = 1
        next
    }
    /^\[/ { in_section = 0 }
    in_section && /^environment[[:space:]]*=/ {
        print "environment=" value
        next
    }
    { print }
' "$SUPERVISORD_CONF" >"$tmp"

if ! grep -q "^environment=" "$tmp"; then
    echo "error: failed to write the environment line -- config left unchanged" >&2
    exit 1
fi
cat "$tmp" >"$SUPERVISORD_CONF"
echo "updated $SUPERVISORD_CONF"

# --- Step 2: reload supervisord and bounce the service -----------------------
supervisorctl reread
supervisorctl update
supervisorctl restart "$PROGRAM"

# --- Step 3: wait for the service to serve again ------------------------------
# `supervisorctl restart` returns once the process is SPAWNED, not once it is
# listening, so the broadcast below would otherwise fire at a socket that is not
# up yet and silently reach nobody.
echo
printf 'waiting for %s to serve...' "$PROGRAM"
is_serving=0
for _ in $(seq 1 "$SERVE_WAIT_ATTEMPTS"); do
    if curl -fsS "${API_BASE}/" >/dev/null 2>&1; then
        is_serving=1
        break
    fi
    sleep "$SERVE_WAIT_INTERVAL_SECONDS"
done
if [ "$is_serving" -eq 1 ]; then
    echo " up"
else
    echo " timed out"
    echo "warning: $PROGRAM is not answering at $API_BASE -- check 'supervisorctl status'" >&2
fi

# --- Step 4: verify the flags reached the running process --------------------
# The process environment is the authoritative check: it is what the app actually
# read. (The served HTML exposes some flags as meta tags, but the env-var ->
# meta-tag mapping is per-flag and known only to the backend, so it cannot be
# checked generically here.)
echo
pid="$(supervisorctl pid "$PROGRAM" 2>/dev/null || true)"
if [ -z "$pid" ] || [ "$pid" = "0" ]; then
    echo "warning: could not resolve a pid for $PROGRAM -- check 'supervisorctl status'" >&2
    exit 1
fi

is_verified=1
for flag in "$@"; do
    if tr '\0' '\n' <"/proc/${pid}/environ" 2>/dev/null | grep -qx "${flag}=1"; then
        echo "verified: $flag is set in the running $PROGRAM"
    else
        echo "warning: $flag is NOT set in the running $PROGRAM" >&2
        is_verified=0
    fi
done

# --- Step 5: reload every connected browser so the new flag takes effect -----
# A flag is served to the browser as a meta tag in the HTML, so picking it up
# needs a full page load -- a websocket reconnect alone will not do it.
#
# The restart dropped every browser's websocket, and the broadcast is one-shot:
# a browser that reconnects after it is sent simply never hears it and sits on
# stale HTML. So give the clients a moment to re-establish first. This is a
# heuristic -- the server exposes no "who is connected" signal to wait on -- and
# it is why the manual-refresh fallback below matters.
sleep "$CLIENT_RECONNECT_SETTLE_SECONDS"
curl -fsS -X POST "${API_BASE}/api/layout/broadcast" \
    -H "Content-Type: application/json" \
    -d '{"op":"reload_system_interface","args":{},"agent_id":""}' >/dev/null &&
    echo "reloaded connected browsers" ||
    echo "warning: could not broadcast a browser reload -- refresh the tab manually" >&2

[ "$is_verified" -eq 1 ] || exit 1
