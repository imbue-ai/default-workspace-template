The pi lifecycle extension now records the live model and thinking level (pi's effort
axis) to `$MNGR_AGENT_STATE_DIR/pi_model_state.json` (`{provider, model, thinking_level}`).
It writes on `session_start` -- which fires at TUI startup, before the first prompt, so
the pre-turn-1 selection is available immediately -- and refreshes on `model_select` /
`thinking_level_select` as the user switches. This gives the chat model bar a low-latency,
on-disk source for pi's current model and effort, which pi otherwise exposes only through
its extension API.

Messages sent to a running pi agent are now delivered as `steer` rather than `followUp`.
pi's agent loop re-polls its steering queue after every tool-call round and injects steered
messages before the next model response, so a message sent mid-run reaches the agent greedily
at the next tool boundary instead of waiting for the whole turn to end. Delivery to an idle
agent is unchanged (it starts a turn either way).

The lifecycle extension's model-state mirror moves to the harness-uniform contract: it now
writes `$MNGR_AGENT_STATE_DIR/minds_model_state.json` with the shared `{model: "provider/id",
effort, fast}` schema (previously `pi_model_state.json` with pi-specific keys), atomically via
tmp + rename. The system interface reads the same file name and schema for every harness.
