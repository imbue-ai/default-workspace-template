New private (non-mirrored) app: the `minds-admin` operator CLI, consolidating the operator/developer lifecycle tooling that used to be spread across `mngr imbue_cloud admin ...`, `minds env`, `minds pool`, `minds server`, and `minds paid` (issue #496 Phase 2).

Commands are env-aware in-process: with an activated env they resolve the tier's pool DSN, pool SSH key, connector URL, and admin API key from Vault / the env's local state (no more secret injection via subprocess env between our own layers); `--database-url`, `MINDS_HOST_POOL_DSN`, `POOL_SSH_PRIVATE_KEY`, and `MINDS_ADMIN_KEY` overrides remain for non-activated one-off use.

Subcommand names are kept verbatim: `minds-admin {env, pool, server, paid, account, workspaces, sweep, relays, repair-keys}`. `minds-admin env destroy` now reaps the env's unleased slices in-process. The deployment-tests orchestrator moved here too (`apps/minds_admin/scripts/test_deployments.py`).
