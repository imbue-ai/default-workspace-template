The opencode lifecycle plugin now records the live model and variant (opencode's effort
axis) to `$MNGR_AGENT_STATE_DIR/opencode_model_state.json` (`{provider, model, variant}`,
where variant `"default"` is the base profile). It writes on each assistant
`message.updated`, last-write-wins, so a mid-turn model or variant switch is reflected
promptly. This gives the chat model bar a low-latency source for opencode's current
selection. A user message.updated carries no model and is a no-op; before the first
assistant message the system-interface resolver falls back to `opencode.json` for the
pre-turn-1 model.
