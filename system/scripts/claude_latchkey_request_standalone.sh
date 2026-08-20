#!/usr/bin/env bash
# PreToolUse hook: HARD-BLOCK a latchkey permission request that is batched with
# another request, chained with another command, has its output redirected, or is
# filed by a backgrounded tool call.
#
# Why: a permission request is the one tool call the USER has to act on. The chat
# turns it into a card (see the permission-request handling in
# system/apps/system_interface/.../harnesses/tool_output.py) built from that
# single call -- the request is recognized from the command, and the button that
# opens the approval dialog comes from the request object the gateway echoes on
# stdout. Only the FIRST such object in the result is read, and only one card is
# rendered per call, so:
#   * two requests in one call -> the second one is never shown, and the user
#     cannot answer a request they cannot see (it just sits in the minds inbox);
#     the verdict messages that come back then line up against the wrong card.
#   * `> /tmp/out.json`, `-o /tmp/out.json`, `| jq .request_id` -> the echoed
#     object never reaches the transcript, so the card has nothing to open the
#     dialog with.
#   * `run_in_background: true` -> same thing through the tool's own flag rather
#     than the command: the result is a shell id, and the output the agent later
#     polls belongs to a different tool call than the card.
# Forbidding the batched/chained/redirected form makes that class of bug
# structurally impossible at the source. The chaining half of the rule is
# deliberately blunt: a chained command that happens to preserve the echo
# (`| tee out.json`) is blocked with the rest, because "the request is the whole
# tool call" is the property the card depends on, and it is the one an agent can
# check without knowing which commands pass stdout through.
#
# Scope: ONLY a POST to the reserved `latchkey-self.invalid/permission-requests`
# host -- the call that FILES a request. Reading the queue or any other latchkey
# curl is untouched, and may be piped or chained freely.
#
# Blocks via exit 2 with a stderr message the agent sees (mirrors
# claude_tk_standalone.sh). The command parsing lives in the sibling
# claude_latchkey_request_check.py -- it shell-tokenizes the command with `shlex`
# (so a rationale that mentions `&&` or `>` stays inside one quoted token and
# cannot trip the checks), which bash regex cannot do reliably.
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
checker="$script_dir/claude_latchkey_request_check.py"

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
