The codex model bar now reflects live model/effort/fast changes again, and moves optimistically on click.

The resolver reads the agent's effective model from the `minds_model_state.json` mirror the patched codex writes under `CODEX_HOME` (updated atomically on every model, effort, and service-tier change, including framework-initiated ones), instead of tailing the rollout for a `thread_settings_applied` event. The installed patched codex no longer emits that event, so the previous reader returned nothing and the bar showed only the launch model; reading the mirror restores live reads and also surfaces changes made before the first turn exists.

Codex switching is now `EAGER_THEN_RECONCILE`: the chip moves on click and reconciles from the mirror once the change lands (the mirror updates within ~100ms), matching claude's model bar.
