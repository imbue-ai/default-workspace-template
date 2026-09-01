#!/usr/bin/env bash
#
# write_plan.sh -- record a routing plan for a build-app request, for offline
# analysis only. Nothing in this workspace reads the result.
#
#   system/scripts/imbue_plan_extra/write_plan.sh <<'EOF'
#   <the brief>
#   EOF
#
# Returns immediately: the first invocation prints the run directory it created,
# re-execs itself detached, and exits 0. The calling agent never waits and never
# sees a failure. Printing the run directory is what makes a call in an agent's
# transcript map to its output: the path is right there in the tool result.
#
# Each call gets its own directory, data/.imbue/plans/<utc-timestamp>-<agent>/:
#
#   brief.md     the brief as passed in, verbatim
#   plan.md      the plan, under a fixed "do not use" header
#   meta.json    ids and timings for correlating this run with the agent's transcript
#   log          this run's own log
#
# Strict mode is on, so every failure this script tolerates is written out as an
# explicit `|| true` or a checked branch at the point it can happen. Nothing here
# may propagate a non-zero exit to the agent that called it.
#
# The child runs a separate headless claude:
#
#   --setting-sources user   Does NOT load this repo's .claude/settings.json, so
#                            none of the workspace's project hooks fire for it --
#                            in particular the SessionStart `uv sync --all-packages`,
#                            which would rebuild the venv underneath the agent that
#                            spawned us. This is the same flag the project-scoped
#                            plugin install in claude_update_plugin.sh is written to
#                            protect. It also means project skills are NOT discovered,
#                            which is why the instructions are fed in as the prompt
#                            rather than invoked as a slash command.
#   --allowed-tools          Read-only. The plan comes back on stdout; the child has
#                            no tool that can write to the workspace, so the only
#                            files that appear are the ones this script writes.
#   MNGR_* unset             A nested claude inherits the spawning agent's state dir
#                            and session id; mngr's readiness hooks key on those to
#                            drive the workspace's RUNNING/WAITING indicator. They are
#                            captured into meta.json first, then cleared.
#
# There is no mngr agent behind this: an `mngr create` agent would appear in the
# workspace's agent list (the UI hides only `is_primary=true`), would pay for
# provisioning on every build-app call, and would have to be destroyed afterwards.
# A plain claude process is invisible and exits on its own.

set -euo pipefail

readonly SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly WORK_DIR="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
readonly PLANS_DIR="${WORK_DIR}/data/.imbue/plans"
readonly INSTRUCTIONS="${SELF_DIR}/prompt.md"

# Ceiling on one plan run. A plan is a single read-and-write turn; anything past
# this is wedged and should not keep holding container memory.
readonly RUN_TIMEOUT_SECONDS=900

# oom_priority's AGENT_SUBPROCESS band (system/services/oom_priority/src/oom_priority/bands.py):
# any agent's subprocess is shed before the agents themselves. This process is
# more expendable than that, but the band is positive-only and 900 is the highest
# value not reserved for the browser fleet.
readonly OOM_SCORE_ADJ=900

# Whichever of these the spawning agent has, most human-readable first. Recorded
# in meta.json in full; a sanitized copy names the run directory.
readonly CALLER_REF="${MNGR_AGENT_NAME:-${MNGR_AGENT_ID:-pid$$}}"

# ---- Parent: create the run directory, announce it, detach --------------------
# The caller gets its shell back here, before any of the work below runs.
if [ -z "${IMBUE_PLAN_EXTRA_RUN_DIR:-}" ]; then
    run_slug="$(date -u +%Y%m%dT%H%M%SZ)-$(printf '%s' "$CALLER_REF" | tr -c 'A-Za-z0-9._-' '-')"
    run_dir="${PLANS_DIR}/${run_slug}"
    # Two calls from one agent inside the same second would collide; the pid
    # disambiguates them without making the common name noisier.
    if [ -e "$run_dir" ]; then
        run_dir="${run_dir}-$$"
    fi
    mkdir -p "$run_dir" || exit 0
    # The brief arrives on stdin rather than as an argument, so it can carry
    # quotes, backticks and newlines without the caller escaping them into a
    # shell word. The timeout bounds the one misuse that could hang the caller:
    # invoked with a stdin that nothing ever closes.
    [ -t 0 ] || timeout 5 cat >"${run_dir}/brief.md" || true
    # The one line the agent sees. It is told to ignore this, but the path in the
    # transcript is what ties the call to its output when the transcript is read later.
    printf '%s\n' "${run_dir#"${WORK_DIR}/"}"
    # setsid is util-linux, so it is present in the workspace container but not on
    # a macOS checkout; nohup alone still detaches from the caller's stdio.
    if command -v setsid >/dev/null 2>&1; then
        IMBUE_PLAN_EXTRA_RUN_DIR="$run_dir" setsid nohup "$0" </dev/null >>"${run_dir}/log" 2>&1 &
    else
        IMBUE_PLAN_EXTRA_RUN_DIR="$run_dir" nohup "$0" </dev/null >>"${run_dir}/log" 2>&1 &
    fi
    disown || true
    exit 0
fi

# ---- Child: everything below runs detached ------------------------------------

readonly RUN_DIR="$IMBUE_PLAN_EXTRA_RUN_DIR"
readonly LOG_FILE="${RUN_DIR}/log"
readonly STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Snapshot the caller's identity now. These are the keys that tie a run to the
# transcript it came from, and they are unset further down before claude starts.
readonly CALLER_AGENT_NAME="${MNGR_AGENT_NAME:-}"
readonly CALLER_AGENT_ID="${MNGR_AGENT_ID:-}"
readonly CALLER_SESSION_ID="${MAIN_CLAUDE_SESSION_ID:-}"

log() {
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG_FILE" || true
}

# Written on every exit path so a run that produced no plan still says why.
write_meta() {
    cat >"${RUN_DIR}/meta.json" <<META || true
{
  "started_at": "${STARTED_AT}",
  "finished_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "status": "$1",
  "caller_agent_name": "${CALLER_AGENT_NAME}",
  "caller_agent_id": "${CALLER_AGENT_ID}",
  "caller_session_id": "${CALLER_SESSION_ID}",
  "work_dir": "${WORK_DIR}"
}
META
}

readonly BRIEF_FILE="${RUN_DIR}/brief.md"
if [ ! -s "$BRIEF_FILE" ]; then
    log "no brief on stdin; nothing to plan"
    write_meta "no_brief"
    exit 0
fi

if [ ! -f "$INSTRUCTIONS" ]; then
    log "instructions missing at ${INSTRUCTIONS}; skipping"
    write_meta "no_instructions"
    exit 0
fi
if ! command -v claude >/dev/null 2>&1; then
    log "no claude on PATH; skipping"
    write_meta "no_claude"
    exit 0
fi

# Guarded rather than redirected: a failing redirection reports on the shell's own
# stderr, which a 2>/dev/null on the echo would not cover.
if [ -w /proc/self/oom_score_adj ]; then
    echo "$OOM_SCORE_ADJ" >/proc/self/oom_score_adj || true
fi

output_file="${RUN_DIR}/plan.md"
body_file="$(mktemp)"
prompt_file="$(mktemp)"
trap 'rm -f "$body_file" "$prompt_file"' EXIT

# The prompt goes to claude on stdin rather than as an argument: it is long,
# and stdin keeps it clear of both the argv limit and shell quoting.
{
    cat "$INSTRUCTIONS"
    printf '\n\n# The brief\n\n'
    cat "$BRIEF_FILE"
} >"$prompt_file"

log "planning: $(head -c 120 "$BRIEF_FILE" | tr '\n' ' ')"

# The spawning agent's mngr identity must not follow us in: mngr's hooks would
# otherwise write this session's markers over that agent's state. Snapshotted
# into CALLER_* at the top of the child, so meta.json still records it.
unset MNGR_AGENT_STATE_DIR MNGR_AGENT_ID MNGR_AGENT_NAME MAIN_CLAUDE_SESSION_ID
unset CLAUDE_PROJECT_DIR CLAUDE_CODE_OAUTH_TOKEN_FILE

cd "$WORK_DIR"
claude_status=0
# The alias, not an exact id: it tracks whatever the workspace's pinned Claude Code
# calls the current Opus, which is what the agents here run on. An exact id would
# hold the recorder on one model while the workspace moved past it.
nice -n 19 timeout "$RUN_TIMEOUT_SECONDS" claude -p \
    --model opus \
    --setting-sources user \
    --allowed-tools "Read,Grep,Glob" \
    <"$prompt_file" >"$body_file" 2>>"$LOG_FILE" || claude_status=$?

if [ "$claude_status" -ne 0 ]; then
    log "claude exited ${claude_status}; no plan written"
    write_meta "claude_failed"
    exit 0
fi
if [ ! -s "$body_file" ]; then
    log "claude produced no output; no plan written"
    write_meta "empty_output"
    exit 0
fi

# The header is written here rather than asked of the model so that it is on
# every plan regardless of what the model did with its instructions.
write_status=0
{
    cat <<'HEADER'
> DO NOT USE THIS PLAN. It was written by a separate process that is not part
> of building anything, it was never reviewed, and the work it describes was
> done by someone else. It is recorded for offline analysis only. Do not read
> it, act on it, cite it, or offer it to the user.

HEADER
    cat "$body_file"
} >"$output_file" || write_status=$?

if [ "$write_status" -ne 0 ]; then
    log "could not write ${output_file} (exit ${write_status})"
    write_meta "write_failed"
    exit 0
fi
log "wrote ${output_file}"
write_meta "ok"
