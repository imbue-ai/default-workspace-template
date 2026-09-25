#!/usr/bin/env bash
#
# run_planner.sh -- write the build plan for one build-app-parallel run.
#
#   .agents/skills/build-app-parallel/scripts/run_planner.sh <run-dir>
#
# Reads <run-dir>/brief.md, runs a headless planner on this skill's own prompt
# (../references/planner-prompt.md), and writes the plan to <run-dir>/plan.md.
# Also writes <run-dir>/meta.json (ids and timings, for tying a plan back to the
# transcript it came from) and <run-dir>/log (the planner's stderr, and its
# stdout when it fails). Runs in the foreground: exit 0 means plan.md is there,
# exit 1 means no plan was written and the reason is in the log, exit 2 is a bad
# call (no run folder, no brief, a plan already present, no claude).
#
# This is the build's critical path: the plan it writes is the plan the whole
# build runs. It is deliberately a separate script from the offline plan recorder
# (system/scripts/imbue_plan_extra/write_plan.sh), which writes plans nothing
# reads, so a change made for that recorder cannot reach this.
#
# The headless planner is set up the way that recorder sets its up, for the same
# reasons -- see its header for the full rationale:
#
#   --setting-sources user   Does NOT load this repo's .claude/settings.json, so
#                            none of the project hooks fire -- in particular the
#                            SessionStart `uv sync --all-packages`, which would
#                            rebuild the venv underneath the agent that spawned
#                            us. Project skills are not discovered either, which
#                            is why the instructions are fed in as the prompt.
#   --allowed-tools          Read-only. The plan comes back on stdout; the
#                            planner has no tool that can write to the workspace.
#   --max-budget-usd         Runaway guard. Almost all of the cost is input: the
#                            plan is short, and reading build-app plus the
#                            workspace is what fills the context.
#   MNGR_*/CLAUDE_* unset    A nested claude inherits the spawning agent's state
#                            dir and session id; mngr's readiness hooks key on
#                            those to drive the workspace's RUNNING/WAITING
#                            indicator. Captured into meta.json first.
#
# There is no mngr agent behind this: one would appear in the workspace's agent
# list, pay for provisioning on every build, and have to be destroyed afterwards.
# A plain claude process is invisible and exits on its own.

set -euo pipefail

readonly SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly INSTRUCTIONS="${SELF_DIR}/../references/planner-prompt.md"

# Ceiling on one planner run. A plan is a single read-and-write turn; anything
# past this is wedged and should not keep holding container memory.
readonly RUN_TIMEOUT_SECONDS=900
# Hard ceiling on what one plan may spend. A run against a bare template checkout
# measures at about $0.62, and a lived-in workspace reads more -- its apps,
# memories and tickets -- so treat that as the floor rather than the typical.
readonly MAX_BUDGET_USD=5.00
# oom_priority's AGENT_SUBPROCESS band (system/services/oom_priority/src/oom_priority/bands.py):
# any agent's subprocess is shed before the agents themselves. This process is
# more expendable than that, but the band is positive-only and 900 is the highest
# value not reserved for the browser fleet.
readonly OOM_SCORE_ADJ=900

if [ "$#" -ne 1 ]; then
    echo "usage: $0 <run-dir>" >&2
    exit 2
fi
if [ ! -d "$1" ]; then
    echo "run_planner: no run folder at $1" >&2
    exit 2
fi
# Absolute, because the planner runs from the orchestrator's checkout below.
readonly RUN_DIR="$(cd "$1" && pwd)"
readonly BRIEF_FILE="${RUN_DIR}/brief.md"
readonly PLAN_FILE="${RUN_DIR}/plan.md"
readonly LOG_FILE="${RUN_DIR}/log"
readonly STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if [ ! -f "$INSTRUCTIONS" ]; then
    echo "run_planner: no planner prompt at ${INSTRUCTIONS}" >&2
    exit 2
fi
if [ ! -s "$BRIEF_FILE" ]; then
    echo "run_planner: no brief at ${BRIEF_FILE}" >&2
    exit 2
fi
if [ -e "$PLAN_FILE" ]; then
    echo "run_planner: a plan already exists at ${PLAN_FILE}; move it aside to plan again" >&2
    exit 2
fi
if ! command -v claude >/dev/null 2>&1; then
    echo "run_planner: claude is not on PATH" >&2
    exit 2
fi

# Snapshot the caller's identity before it is unset below. These are the keys
# that tie a plan to the transcript it came from.
readonly CALLER_AGENT_NAME="${MNGR_AGENT_NAME:-}"
readonly CALLER_AGENT_ID="${MNGR_AGENT_ID:-}"
readonly CALLER_SESSION_ID="${MAIN_CLAUDE_SESSION_ID:-}"
readonly WORK_DIR="${MNGR_AGENT_WORK_DIR:-$(pwd)}"

# Written on every exit path, so a run that produced no plan still says why.
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

prompt_file="$(mktemp)"
body_file="$(mktemp)"
trap 'rm -f "$prompt_file" "$body_file"' EXIT

# The prompt goes to claude on stdin rather than as an argument: it is long, and
# stdin keeps it clear of both the argv limit and shell quoting.
{
    cat "$INSTRUCTIONS"
    printf '\n\n# The brief\n\n'
    cat "$BRIEF_FILE"
} >"$prompt_file"

# Guarded rather than redirected: a failing redirection reports on the shell's
# own stderr, which a 2>/dev/null on the echo would not cover.
if [ -w /proc/self/oom_score_adj ]; then
    echo "$OOM_SCORE_ADJ" >/proc/self/oom_score_adj || true
fi

# The planner reads the repo from the orchestrator's checkout, which is where
# build-app and the other references it reads live.
cd "$WORK_DIR"

planner_status=0
# The model alias, not an exact id: it tracks whatever the workspace's pinned
# Claude Code calls the current Opus, which is what the agents here run on.
env -u MNGR_AGENT_STATE_DIR -u MNGR_AGENT_ID -u MNGR_AGENT_NAME -u MAIN_CLAUDE_SESSION_ID \
    -u CLAUDE_PROJECT_DIR -u CLAUDE_CODE_OAUTH_TOKEN_FILE \
    nice -n 19 timeout "$RUN_TIMEOUT_SECONDS" claude -p \
    --model opus \
    --setting-sources user \
    --allowed-tools "Read,Grep,Glob" \
    --max-budget-usd "$MAX_BUDGET_USD" \
    <"$prompt_file" >"$body_file" 2>>"$LOG_FILE" || planner_status=$?

if [ "$planner_status" -ne 0 ]; then
    # claude -p reports some failures (budget, auth, API errors) on stdout.
    {
        printf '\n--- planner stdout (exit %s) ---\n' "$planner_status"
        cat "$body_file"
    } >>"$LOG_FILE"
    write_meta "planner_failed"
    echo "run_planner: the planner exited ${planner_status}; see ${LOG_FILE}" >&2
    exit 1
fi
if [ ! -s "$body_file" ]; then
    write_meta "empty_output"
    echo "run_planner: the planner produced no output; see ${LOG_FILE}" >&2
    exit 1
fi

cp "$body_file" "$PLAN_FILE"
write_meta "ok"
echo "run_planner: wrote ${PLAN_FILE}"
