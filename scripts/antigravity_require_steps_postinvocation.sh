#!/usr/bin/env bash
# PostInvocation hook (antigravity, half 2 of 2): if the PostToolUse half
# (scripts/antigravity_flag_missing_step_posttooluse.sh) left a flag,
# deliver the step-tracking reminder via injectSteps/ephemeralMessage and
# clear the flag. See that script's header comment for why this is split
# across two hooks.
set -euo pipefail

cat > /dev/null # PostInvocation input isn't needed for this check

repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
tickets_dir="${TICKETS_DIR:-${repo_root}/.tickets}"
flag_file="${tickets_dir}/.antigravity_missing_step_flag"

if [[ ! -f "$flag_file" ]]; then
    echo '{}'
    exit 0
fi

reason=$(cat "$flag_file")
rm -f "$flag_file"

if [[ "$reason" == "no_in_progress" ]]; then
    msg="[Step tracking reminder]

You have declared step records but none is currently in_progress. Call \`tk start <id>\` on your next step before doing more work. Steps must be serial -- only one in_progress at a time."
else
    msg="[Step tracking reminder]

You did substantive work without declaring any step records. The chat progress view requires steps to render your work as a structured timeline.

Before continuing, declare your plan as step records (each prints \`Created <id>: <title>\`):
  tk create --step \"Description of first step\"
  tk create --step \"Description of second step\"
  ...
Then start the first step with its literal id: tk start <id>

See AGENTS.md > Task management for the full protocol."
fi

jq -n --arg msg "$msg" '{injectSteps: [{ephemeralMessage: $msg}]}'
