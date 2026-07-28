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
- The "New Codex Agent" launcher is gated behind a FEATURE_FLAG_ENABLE_CODEX
  feature flag (off by default), so codex can be dark-launched and enabled per
  host without a rebuild.
- Fixed: a codex agent interrupted mid-turn and resumed could stay stuck on
  "Thinking..." until the user sent another message. The restart-boundary check
  looked for claude's marker file on every agent, so it never fired for codex
  (which writes its own).
