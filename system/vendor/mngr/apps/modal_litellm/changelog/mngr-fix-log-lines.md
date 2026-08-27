The deployed LiteLLM proxy's log lines are now level-queryable JSON in the tier's OpenObserve `modal_logs` stream (mngr-internal#656):

- LiteLLM's native JSON logging is enabled (`litellm_settings.json_logs` in the deployed config, plus `JSON_LOGS=1` exported before LiteLLM is imported, in both `litellm_app` and `migrate_db`), so its own lines carry `level` / `timestamp` / `message`; `LITELLM_LOG` is set to INFO so LiteLLM's own INFO lines are emitted, matching our `imbue.*` packages (set it to DEBUG in a dev env's `litellm` secret for LiteLLM's debug output).

- Our own lines (`migrate_db`, the access-log middleware) go through the shared `configure_logging()` JSON bootstrap; `migrate_db`'s ad-hoc `logging.basicConfig` is gone.

The local-dev `litellm_proxy/config.yaml` is unchanged (human-readable text).
