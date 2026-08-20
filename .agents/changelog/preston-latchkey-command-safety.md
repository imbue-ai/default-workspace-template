The latchkey skill now states the one-request-per-tool-call rule for permission requests.

Filing a permission request must be the only command in its tool call, with its output left alone -- no second request batched in, no `&&` chain, no `> file` or `| jq`. The skill explains that the chat builds the user's approval card out of that single call (the button comes from the request object the gateway echoes on stdout), notes that a PreToolUse hook blocks the other forms, and says to post one request, wait for its verdict, then post the next.

The "Ask for user permission" example now matches that rule: the two read-only lookups stay in one block, and the request itself sits in a block of its own, so an agent that runs the example as written is not blocked by the hook.

This replaces the narrower "never pipe the output through jq" note.
