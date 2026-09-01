#!/usr/bin/env bash
#
# write_unused_plan.sh -- record a plan for a build-app request, for offline
# analysis only. Nothing in this workspace reads the result.
#
#   system/scripts/write_unused_plan.sh "<the user's request>"
#
# Returns immediately: the first invocation re-execs itself detached and exits 0,
# so the calling agent never waits and never sees a failure. All of the work
# happens in the detached child, whose only effect on the workspace is one file
# written under data/.imbue/plans/ (plus its own log line).
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
#                            file that appears is the one this script writes.
#   MNGR_* unset             A nested claude inherits the spawning agent's state dir
#                            and session id; mngr's readiness hooks key on those to
#                            drive the workspace's RUNNING/WAITING indicator.
#
# There is no mngr agent behind this: an `mngr create` agent would appear in the
# workspace's agent list (the UI hides only `is_primary=true`), would pay for
# provisioning on every build-app call, and would have to be destroyed afterwards.
# A plain claude process is invisible and exits on its own.

set -euo pipefail

readonly WORK_DIR="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
readonly PLANS_DIR="${WORK_DIR}/data/.imbue/plans"
readonly INSTRUCTIONS="${WORK_DIR}/system/scripts/unused_plan/SKILL.md"
readonly LOG_FILE="${PLANS_DIR}/.write_unused_plan.log"

# Ceiling on one plan run. A plan is a single read-and-write turn; anything past
# this is wedged and should not keep holding container memory.
readonly RUN_TIMEOUT_SECONDS=900

# oom_priority's AGENT_SUBPROCESS band (system/services/oom_priority/src/oom_priority/bands.py):
# any agent's subprocess is shed before the agents themselves. This process is
# more expendable than that, but the band is positive-only and 900 is the highest
# value not reserved for the browser fleet.
readonly OOM_SCORE_ADJ=900

log() {
    mkdir -p "$PLANS_DIR" || true
    printf '%s write_unused_plan: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG_FILE" || true
}

# ---- Parent: detach and return -----------------------------------------------
# The caller gets its shell back here, before any of the work below runs.
if [ "${WRITE_UNUSED_PLAN_DETACHED:-}" != "1" ]; then
    mkdir -p "$PLANS_DIR" || true
    # setsid is util-linux, so it is present in the workspace container but not on
    # a macOS checkout; nohup alone still detaches from the caller's stdio.
    if command -v setsid >/dev/null 2>&1; then
        WRITE_UNUSED_PLAN_DETACHED=1 setsid nohup "$0" "$@" </dev/null >>"$LOG_FILE" 2>&1 &
    else
        WRITE_UNUSED_PLAN_DETACHED=1 nohup "$0" "$@" </dev/null >>"$LOG_FILE" 2>&1 &
    fi
    disown || true
    exit 0
fi

# ---- Child: everything below runs detached ------------------------------------

request="${1:-}"
if [ -z "$request" ]; then
    log "no request argument; nothing to plan"
    exit 0
fi
if [ ! -f "$INSTRUCTIONS" ]; then
    log "instructions missing at ${INSTRUCTIONS}; skipping"
    exit 0
fi
if ! command -v claude >/dev/null 2>&1; then
    log "no claude on PATH; skipping"
    exit 0
fi

# Guarded rather than redirected: a failing redirection reports on the shell's own
# stderr, which a 2>/dev/null on the echo would not cover.
if [ -w /proc/self/oom_score_adj ]; then
    echo "$OOM_SCORE_ADJ" >/proc/self/oom_score_adj || true
fi

# The spawning agent's mngr identity must not follow us in: mngr's hooks would
# otherwise write this session's markers over that agent's state.
unset MNGR_AGENT_STATE_DIR MNGR_AGENT_ID MNGR_AGENT_NAME MAIN_CLAUDE_SESSION_ID
unset CLAUDE_PROJECT_DIR CLAUDE_CODE_OAUTH_TOKEN_FILE

output_file="${PLANS_DIR}/$(date -u +%Y%m%dT%H%M%SZ)-$$.md"
body_file="$(mktemp)"
prompt_file="$(mktemp)"
trap 'rm -f "$body_file" "$prompt_file"' EXIT

# The prompt goes in on stdin, not as an argument: the instructions open with YAML
# frontmatter, and claude reads a leading `---` as an option. Stdin also sidesteps
# the argv length limit, which a long request would otherwise reach.
{
    cat "$INSTRUCTIONS"
    printf '\n\n# The request\n\n%s\n' "$request"
} >"$prompt_file"

log "planning: ${request:0:120}"

cd "$WORK_DIR"
claude_status=0
nice -n 19 timeout "$RUN_TIMEOUT_SECONDS" claude -p \
    --model opus \
    --setting-sources user \
    --allowed-tools "Read,Grep,Glob" \
    <"$prompt_file" >"$body_file" 2>>"$LOG_FILE" || claude_status=$?

if [ "$claude_status" -ne 0 ]; then
    log "claude exited ${claude_status}; no plan written"
    exit 0
fi
if [ ! -s "$body_file" ]; then
    log "claude produced no output; no plan written"
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
    exit 0
fi
log "wrote ${output_file}"
