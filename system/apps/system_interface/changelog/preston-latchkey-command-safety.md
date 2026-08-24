The permission-request parsers now name the PreToolUse gate that exists to keep their input readable, and vice versa.

`harnesses/tool_output.py` and the chat card's `parsePermissionRequest` both assume a shape the agent has to hold up its end of: one filing per tool call, with the gateway's echoed object in that call's own result. A workspace hook (`system/scripts/agent_latchkey_request_standalone.sh`) is what holds the agent to it -- it blocks a request that is batched, chained, redirected, or backgrounded, and it copies `PERMISSION_REQUEST_HOST` from this package.

Nothing about that was written down on this side, so a future change to what counts as a request call, or to how many a result can carry, could silently leave the hook blocking a shape this package now handles (or waving through one it does not). Each side now points at the other by file and function, so both get updated together.

A tool call the harness refused no longer renders as a permission card.

The card is chosen from the call's input, so a command a PreToolUse guard blocked -- now the common case, since the guard exists to block exactly these -- still looked like a filing. No request had reached the gateway, so the card had nothing to read and said "Couldn't read this request -- see the Permissions tab", pointing at a tab with nothing in it.

A call renders as a card only when a request can actually be read out of it. That covers a guard refusing the command, and equally the gateway rejecting the body -- `curl` exits 0 on a 4xx, so a malformed filing does not even read as a failed call. Both now render as the ordinary tool calls they are, with the error they got. A call with no result yet still gets a card: that is the request in flight, which is when it most needs to be visible. The refused call also stops taking a place in the queue that later granted/denied messages are matched against.
