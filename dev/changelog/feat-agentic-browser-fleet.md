Added the `agentic-browser-fleet` skill, which teaches the agent to drive the
per-workspace browser fleet via the `agentic-browser-fleet` CLI: listing
browsers, starting them, and running browser-use tasks on a specific browser
while the trace streams to the agent's own output. It documents the ownership
rules (agents never preempt each other and wait in a FIFO queue; a human "Take
control" ends the task and the agent resumes only when told to), the exit codes,
and how to anchor a sub-agent's browser pane next to the parent's chat
(`BROWSER_FLEET_ANCHOR`).

The `scripts/layout.py` agent helper can now address a specific browser session
as a pane ref (`service:browser?session=<id>`).
