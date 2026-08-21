The permission-request parsers now name the PreToolUse gate that exists to keep their input readable, and vice versa.

`harnesses/tool_output.py` and the chat card's `parsePermissionRequest` both assume a shape the agent has to hold up its end of: one filing per tool call, with the gateway's echoed object in that call's own result. A workspace hook (`system/scripts/agent_latchkey_request_standalone.sh`) is what holds the agent to it -- it blocks a request that is batched, chained, redirected, or backgrounded, and it copies `PERMISSION_REQUEST_HOST` from this package.

Nothing about that was written down on this side, so a future change to what counts as a request call, or to how many a result can carry, could silently leave the hook blocking a shape this package now handles (or waving through one it does not). Each side now points at the other by file and function, so both get updated together.
