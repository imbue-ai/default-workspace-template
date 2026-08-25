The connector's structured log lines (`http_request` access records and the new `share_visit_authorized` records) carry a `minds_env` field with the deployed env's name, letting each dev env's analytics aggregation filter its own lines out of the shared per-tier log store; lines from containers predating the stamp simply omit the field.

The duplicate `MINDS_ENV_NAME` reader (`cloudflare.current_minds_env_name`) is gone -- the slice-box reconcile scoping now uses `modal_app_kit`'s shared `deployed_minds_env_name()` helper, the same one the log-line stamping uses.
