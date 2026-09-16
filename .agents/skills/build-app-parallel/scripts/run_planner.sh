#!/usr/bin/env bash
#
# run_planner.sh -- write the build plan for one build-app-parallel run.
#
#   .agents/skills/build-app-parallel/scripts/run_planner.sh <run-dir>
#
# Reads <run-dir>/brief.md, runs a headless planner on it, and writes the plan to
# <run-dir>/plan.md. Runs in the foreground and exits non-zero when no plan was
# written; the orchestrator runs it as a background task and is notified when it
# exits. Planner stderr goes to <run-dir>/planner.log.
#
# The planner runs the way the offline plan recorder
# (system/scripts/imbue_plan_extra/write_plan.sh) runs its planner, for the same
# reasons -- see that script's header for the full rationale:
#
#   --setting-sources user   Skips this repo's project hooks, in particular the
#                            SessionStart `uv sync --all-packages`. Project skills
#                            are not discovered either, so the instructions go in
#                            as the prompt.
#   --allowed-tools          Read-only. The plan comes back on stdout.
#   MNGR_* unset             Keeps the planner from writing its session markers
#                            over the orchestrating agent's mngr state.

set -euo pipefail

readonly SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly INSTRUCTIONS="${SELF_DIR}/../references/planner-prompt.md"

# Ceiling on one planner run; past this it is wedged.
readonly RUN_TIMEOUT_SECONDS=900
# Runaway guard on spend. Almost all of the cost is reading build-app and the workspace.
readonly MAX_BUDGET_USD=5.00

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
readonly LOG_FILE="${RUN_DIR}/planner.log"

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

prompt_file="$(mktemp)"
body_file="$(mktemp)"
trap 'rm -f "$prompt_file" "$body_file"' EXIT

{
    cat "$INSTRUCTIONS"
    printf '\n\n# The brief\n\n'
    cat "$BRIEF_FILE"
} >"$prompt_file"

# The planner reads the repo from the orchestrator's checkout, which is where
# build-app and the other skills it reads live.
cd "${MNGR_AGENT_WORK_DIR:-$(pwd)}"

planner_status=0
env -u MNGR_AGENT_STATE_DIR -u MNGR_AGENT_ID -u MNGR_AGENT_NAME -u MAIN_CLAUDE_SESSION_ID \
    -u CLAUDE_PROJECT_DIR -u CLAUDE_CODE_OAUTH_TOKEN_FILE \
    timeout "$RUN_TIMEOUT_SECONDS" claude -p \
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
    echo "run_planner: the planner exited ${planner_status}; see ${LOG_FILE}" >&2
    exit 1
fi
if [ ! -s "$body_file" ]; then
    echo "run_planner: the planner produced no output; see ${LOG_FILE}" >&2
    exit 1
fi

mv "$body_file" "$PLAN_FILE"
echo "run_planner: wrote ${PLAN_FILE}"
