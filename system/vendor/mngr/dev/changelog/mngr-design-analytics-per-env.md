Root-level pieces of the per-env analytics provisioning work:

- `scripts/delete_accounts.py` gains `--env <name>`: per-env (dev) targets resolve their credentials -- host_pool DSN, SuperTokens, and the per-env analytics stack -- from `~/.minds-<env>/secrets.toml` (the local state `minds-admin env deploy` writes) before falling back to Vault.

- `.minds/template/analytics.sh` declares the optional `ANALYTICS_LOGS_ENV_FILTER` key (blank on shared tiers).

- `specs/minds-analytics/spec.md`: the deployment section now describes the split -- dev envs auto-provision isolated per-env stacks; shared tiers keep the operator bringup + Vault entry.
