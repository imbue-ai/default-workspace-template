# minds_admin

Private operator CLI (`minds-admin`) for the minds stack. It consolidates the operator/developer lifecycle tooling that used to be spread across `mngr imbue_cloud admin ...`, `minds env`, `minds pool`, `minds server`, and `minds paid`.

This app is **private**: it is deliberately absent from the public-mirror allowlist (`mirror/copy.bara.sky`). Private code may depend on the public packages (`imbue-minds`, `imbue-mngr-imbue-cloud`, ...); public code must never import `imbue.minds_admin`.

## Commands

All commands are env-aware: with an activated env (`eval "$(uv run minds-admin env activate <name>)"`) they resolve the tier's pool DSN, box management credentials, connector URL, and admin API key from Vault / the env's local state, so nothing needs to be hand-exported. Box management credentials are per box generation: a gen-2 box is reached with a short-lived SSH certificate the tier's Vault SSH CA signs on demand for the operator key at `~/.mindsadmin/<tier>/ssh_id` (the per-tier operator identity directory, which also holds the WireGuard key; `MINDS_ADMIN_IDENTITY_DIR` relocates the root), a gen-1 box with the tier's static pool key from Vault. Explicit flags and env-var overrides (`--database-url`, `MINDS_HOST_POOL_DSN`, `POOL_SSH_PRIVATE_KEY`, `MINDS_ADMIN_KEY`, `OVH_*`) remain for non-activated one-off use.

- `minds-admin env {activate, deactivate, list, deploy, destroy, recover}` -- minds environment lifecycle (dev / staging / production tiers).
- `minds-admin pool {create, list, destroy, teardown-slices, backfill-host-keys}` -- bare-metal slice pool provisioning (bakes leasable pool hosts onto registered boxes).
- `minds-admin server {pricing, order, await-delivery, setup, prep, ssh, unlock, list, register, set-status, drain}` -- bare-metal box fleet management (``prep`` / ``setup`` dispatch on the box's recorded slice-fleet generation; ``ssh`` opens a management session over the same automatically resolved dial as every other box command; ``unlock`` opens a gen-2 box's locked LUKS storage volume with its Vault recovery passphrase and brings its slices back).
- `minds-admin wireguard {config, sync-peers, install-onetun}` -- the gen-2 management WireGuard overlay (operator client configs; fleet peer sync from the `[management_plane]` table of the tier's committed `deploy.toml`; the pinned onetun install from the artifact mirror).
- `minds-admin artifacts {list, upload, verify}` -- the pinned upstream artifacts the fleet downloads from imbue's mirror (`slices/mirror_artifacts.py` is the manifest; `upload` fetches, digest-verifies, and stores each one; see `apps/apt_mirror/README.md`, "Artifacts").
- `minds-admin paid {domain, email} {add, remove, list}` -- the connector's paid lists (ally-plan eligibility).
- `minds-admin account {show, set-plan, set-quota, suspend, unsuspend, revoke-sessions}` -- per-account entitlements and reversible suspension.
- `minds-admin workspaces {stop, start, abandon, release}` -- workspace-lifecycle escape hatches (operator force-stop; operator start of a stopped workspace; mark-crashed; release a confirmed-abandoned lease through the connector's own destroy chain, any lifecycle status).
- `minds-admin sweep {r2, lease-records}` -- on-demand connector sweeps (`lease-records --dry-run` is the audit view of pool-lease vs workspace-record drift).
- `minds-admin relays {list, add, remove}` -- the sharing relay fleet inventory.
- `minds-admin repair-keys` -- fleet sweep for the historical slice authorized_keys wipe.
- `minds-admin cutover {preflight, drain, repave, restore}` -- the one-time gen-1 -> gen-2 slice-fleet cutover (runbook: `apps/minds/docs/deploy/gen2-cutover.md`; deleted after the last tier is cut over).
- `minds-admin repair-home-layout` -- probe (`--all-leased` for the whole pool), migrate, or roll back the home-tree layout of slice workspaces the slow path rebuilt on the legacy volume layout (gen-1 boxes only: a slice on a gen-2 box is skipped, since the repair runs through the lima client).

Run any command with `--help` for details; the deployment runbooks live in `apps/minds/docs/deploy/` (private).

## Layout

- `imbue/minds_admin/cli/` -- the click command groups (entry assembled in `cli/root.py`, invoked via `main.py`).
- `imbue/minds_admin/envs/` -- env provisioning/deploy/destroy machinery (Modal, Neon, SuperTokens, Vault-driven).
- `imbue/minds_admin/bake/` -- the provider-generic pool-host bake (default-workspace-template content onto a provisioned host).
- `imbue/minds_admin/slices/` -- operator-only bare-metal slice modules (ordering, pricing, prep, DB access, fleet repairs).
- `scripts/test_deployments.py` -- the deployment-tests orchestrator (stands up ci envs and drives the `apps/minds/deployment_tests/` suites).
