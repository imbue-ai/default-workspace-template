The analytics app's deploy integration now lives in this app (it landed while the env-deploy machinery still lived in `apps/minds`, and moved here with the operator-surface split):

- `minds-admin env deploy` gains a sticky `--with-analytics` / `--without-analytics` flag for dynamic dev envs, overriding the tier `deploy.toml`'s `[analytics]` block (the tier default, off everywhere until bringup).

- When analytics is enabled for the env, the deploy pushes the `analytics-<tier>-<deploy_id>` Modal Secret from the tier's Vault entry, applies `apps/analytics/migrations/` to the analytics ops database via the schema_migrations runner, and `modal deploy`s the cron-only `analytics-<env>` Modal app (and `env destroy` stops it).
