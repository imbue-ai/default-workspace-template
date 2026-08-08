The pi lifecycle extension now records the live model and thinking level (pi's effort
axis) to `$MNGR_AGENT_STATE_DIR/pi_model_state.json` (`{provider, model, thinking_level}`).
It writes on `session_start` -- which fires at TUI startup, before the first prompt, so
the pre-turn-1 selection is available immediately -- and refreshes on `model_select` /
`thinking_level_select` as the user switches. This gives the chat model bar a low-latency,
on-disk source for pi's current model and effort, which pi otherwise exposes only through
its extension API.
