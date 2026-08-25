# minds_admin

Private operator CLI (`minds-admin`) for the minds stack. It consolidates the operator/developer lifecycle tooling that used to be spread across `mngr imbue_cloud admin ...`, `minds env`, `minds pool`, `minds server`, and `minds paid`.

This app is **private**: it is deliberately absent from the public-mirror allowlist (`mirror/copy.bara.sky`). Private code may depend on the public packages (`imbue-minds`, `imbue-mngr-imbue-cloud`, ...); public code must never import `imbue.minds_admin`.

## Commands

All commands are env-aware: with an activated env (`eval "$(uv run minds-admin env activate <name>)"`) they resolve the tier's pool DSN, pool SSH key, connector URL, and admin API key from Vault / the env's local state, so nothing needs to be hand-exported. Explicit flags and env-var overrides (`--database-url`, `MINDS_HOST_POOL_DSN`, `POOL_SSH_PRIVATE_KEY`, `MINDS_ADMIN_KEY`, `OVH_*`) remain for non-activated one-off use.

- `minds-admin env {activate, deactivate, list, deploy, destroy, recover}` -- minds environment lifecycle (dev / staging / production tiers).
- `minds-admin pool {create, list, destroy, teardown-slices, backfill-host-keys}` -- bare-metal slice pool provisioning (bakes leasable pool hosts onto registered boxes).
- `minds-admin server {pricing, order, await-delivery, setup, prep, list, register, set-status}` -- bare-metal box fleet management.
- `minds-admin paid {domain, email} {add, remove, list}` -- the connector's paid lists (ally-plan eligibility).
- `minds-admin account {show, set-plan, set-quota, suspend, unsuspend, revoke-sessions}` -- per-account entitlements and reversible suspension.
- `minds-admin workspaces {stop, abandon}` -- workspace-lifecycle escape hatches (operator force-stop; mark-crashed).
- `minds-admin sweep r2` -- on-demand connector sweeps.
- `minds-admin relays {list, add, remove}` -- the sharing relay fleet inventory.
- `minds-admin repair-keys` -- fleet sweep for the historical slice authorized_keys wipe.

Run any command with `--help` for details; the deployment runbooks live in `apps/minds/docs/deploy/` (private).

## Layout

- `imbue/minds_admin/cli/` -- the click command groups (entry assembled in `cli/root.py`, invoked via `main.py`).
- `imbue/minds_admin/envs/` -- env provisioning/deploy/destroy machinery (Modal, Neon, SuperTokens, Vault-driven).
- `imbue/minds_admin/bake/` -- the provider-generic pool-host bake (default-workspace-template content onto a provisioned host).
- `imbue/minds_admin/slices/` -- operator-only bare-metal slice modules (ordering, pricing, prep, DB access, fleet repairs).
- `scripts/test_deployments.py` -- the deployment-tests orchestrator (stands up ci envs and drives the `apps/minds/deployment_tests/` suites).
