The latchkey skill now states the one-request-per-tool-call rule for permission requests.

Filing a permission request must be the only command in its tool call, with its output left alone -- no second request batched in, no `&&` chain, no `> file` or `| jq`. The skill explains that the chat builds the user's approval card out of that single call (the button comes from the request object the gateway echoes on stdout), notes that a PreToolUse hook blocks the other forms, and says to post one request, wait for its verdict, then post the next.

The "Ask for user permission" example now matches that rule: the two read-only lookups stay in one block, and the request itself sits in a block of its own, so an agent that runs the example as written is not blocked by the hook.

This replaces the narrower "never pipe the output through jq" note.

Every other skill that documents a permission request now follows the same rule, so none of them is blocked by the hook: `github-sync` and `publish-template` no longer file GitHub's two scopes in one bash block (each request is its own tool call, the second after the first verdict); `file-sharing` no longer shares a block between the permissions lookup and the request; `minds-api` states the rule in place of the old jq note; and `use-template` says each activation request is its own call.
