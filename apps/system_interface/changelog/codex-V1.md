Added codex as a peer harness in the workspace chat UI, alongside claude.

- Codex transcript tool blocks and the live activity caption now describe what
  the code-mode call actually did -- "Running <cmd>", "Editing <file>",
  "Searching the web" -- instead of an opaque "Tool: exec", from a single shared
  label (so header and caption never disagree).
- The bottom activity indicator is driven by codex's own turn markers
  (task_started / task_complete), so "Thinking..." brackets the real turn; codex
  tool captions are briefly debounced so fast code-mode calls don't flicker.
- Sending to a cold codex agent no longer hangs the request past the proxy
  timeout ("Failed to send: null"): the send is bounded and, if the agent is
  still starting, accepted for background delivery.
