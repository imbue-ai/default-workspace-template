#!/usr/bin/env bash
# PreToolUse hook: HARD-BLOCK a latchkey permission request that is batched with
# another request, chained with another command, has its output redirected, or is
# filed by a backgrounded tool call.
#
# Every rule here is the shape one reader needs, not something latchkey requires;
# keep the two in step:
#
#   system/apps/system_interface/imbue/system_interface/harnesses/tool_output.py
#     is_permission_request_call()  -- recognizes the card from the call's INPUT
#                                      (whole raw text), one card per tool call
#     find_permission_request()     -- lifts the gateway's echoed object out of
#                                      that call's RESULT, first match only
#   system/apps/system_interface/frontend/src/views/permission-card.ts
#     parsePermissionRequest()      -- reads the request_id the card's
#                                      "Review & respond" button opens the dialog with
#
# So a second request in one call is never shown to the user, and anything that
# keeps the echo out of this call's result -- `> /tmp/out.json`, `-o /tmp/out.json`,
# `| jq .request_id`, or `run_in_background: true`, whose result is a shell id --
# leaves a card with no button. `| tee out.json` preserves the echo and is blocked
# anyway: "the request is the whole tool call" is the property the card depends on,
# and the one an agent can check without knowing which commands pass stdout through.
#
# Scope: ONLY a POST to the reserved `latchkey-self.invalid/permission-requests`
# host -- the call that FILES a request. Reading the queue or any other latchkey
# curl is untouched, and may be piped or chained freely.
#
# Blocks via exit 2 with a stderr message the agent sees. The command parsing lives
# in the sibling agent_latchkey_request_check.py, which shell-tokenizes with
# `shlex` so a rationale that mentions `&&` or `>` stays inside one quoted token.
set -euo pipefail

input=$(cat)

tool_name=$(echo "$input" | jq -r '.tool_name // empty')
[[ "$tool_name" == "Bash" ]] || exit 0

command=$(echo "$input" | jq -r '.tool_input.command // empty')
[[ -n "$command" ]] || exit 0

# Cheap guard: every filing mentions the host path, so the overwhelming majority
# of commands never pay for the tokenizer.
[[ "$command" == *"permission-requests"* ]] || exit 0

script_dir=$(cd "$(dirname "$0")" && pwd)
checker="$script_dir/agent_latchkey_request_check.py"

# Whether the call runs the command in the background is a property of the tool
# input, not of the command text, so it is read here and handed to the checker.
# A harness whose payload has no such field never sets it. (Two `exec` lines
# rather than an optional-flag array: `"${arr[@]}"` on an empty array is an
# unbound-variable error under `set -u` on bash 3.2.)
is_backgrounded=$(echo "$input" | jq -r '.tool_input.run_in_background // false')
if [[ "$is_backgrounded" == "true" ]]; then
    exec python3 "$checker" --backgrounded "$command"
fi
exec python3 "$checker" "$command"
