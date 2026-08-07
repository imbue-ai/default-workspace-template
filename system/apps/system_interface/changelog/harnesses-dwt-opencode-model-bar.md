Wired opencode into the chat UI's model bar (first cut: model bar only, no transcript
yet), mirroring the pi wiring.

- opencode's model catalog is parsed from opencode's own models.dev cache
  (`~/.cache/opencode/models.json`, ~6200 models), so it is a searchable picker like
  pi's. Each model's effort options mirror opencode's OWN variant synthesis, not the raw
  models.dev effort values: an `effort` reasoning axis contributes its values, a
  `budget_tokens` axis (all Anthropic Claude) contributes `high`/`max`, and a `toggle`
  axis contributes no effort options. This matches the live `variant` we read back, so a
  Claude model's effort chip works instead of blanking.

- The live model/variant comes from `opencode_model_state.json` (written by the
  opencode lifecycle plugin), and `variant` "" / "default" read as no effort. The
  pre-turn-1 model comes from probing the live opencode server (`GET /config` +
  `/config/providers`), cached once -- so the bar shows a model before the first turn,
  which pi cannot do.

- The picker offers only the authenticated models (`opencode models`, run per-agent with
  the agent's `OPENCODE_CONFIG_DIR` + `XDG_DATA_HOME`), recomputed each time the picker
  opens, so a fresh login shows up without a reload.

- Switching model/effort from the bar is a live, session-level call to the opencode
  server (`POST /api/session/{id}/model`, which sets model and variant together);
  ON_CHANGE, so the chip reconciles from the state file on the next turn.

- Adds a "New Opencode Agent" launcher. NOTE: launching a chat agent on opencode also
  requires `mngr_opencode` to support the `output_style` setting the shared `chat`
  template sets (as `mngr_pi_coding` does); until that lands, agent creation fails at the
  mngr layer.
