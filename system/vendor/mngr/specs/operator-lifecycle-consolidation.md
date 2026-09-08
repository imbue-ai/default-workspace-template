# Operator lifecycle consolidation (issue #496)

Consolidate the bare-metal/pool operator lifecycle behind a private, tier-aware surface.
Reference: https://github.com/imbue-ai/mngr-internal/issues/496

## Overview

- One root problem, three symptoms: the operator lifecycle (bake slices, manage boxes, manage envs, connector admin) is smeared across three layers -- the **public** `mngr_imbue_cloud` plugin, the **public** `apps/minds` CLI, and private `private.just` shell recipes -- and it shares the minds desktop app's mngr data root.
- Decision 1 (Part A): slice bakes become a pure function of (bake source, box, tier credentials). Every bake invocation runs its inner `mngr` subprocesses in a **fresh, ephemeral mngr namespace** (throwaway `MNGR_HOST_DIR` + inert `MNGR_PREFIX`), so bake-time hosts/agents/discovery-events never enter any user-facing data root. This is implemented inside `mngr_imbue_cloud` itself (protects every caller, not just minds-wrapped ones).
- Decision 2 (surface move): all operator/developer tooling consolidates into a new **private, non-mirrored** app `apps/minds_admin` with its own console script `minds-admin` (not an mngr plugin). This includes the whole `mngr imbue_cloud admin` group, the `minds pool` / `minds server` / `minds paid` wrappers, and the entire `minds env` group plus its operator-only machinery. The two CLI layers collapse: `minds-admin` commands are env-aware and call the implementation in-process (no subprocess hop, no secret-injection-via-env between our own layers).
- Decision 3 (Part B): `server setup` and `server prep` converge on one "converge box to desired state" appliance that includes the observability collector, failing closed when the tier has observability configured; the env-aware CLI gains `order` / `await-delivery` / `setup`; the runbook collapses to one command per box.
- Dependency rule that makes this shape possible: the public mirror constraint is one-directional. Private code may depend on public packages (`imbue-minds`, `imbue-mngr-imbue-cloud`, `imbue-observability`); public code must never import `apps/minds_admin`.
- Sequencing: three phases, each landing as its own working PR: (1) Part A in-place, (2) the surface move, (3) Part B on the new surface.

## Expected behavior

### Part A: bake namespace isolation

- Starting the minds desktop app on a machine that recently ran bakes no longer shows a burst of `slice-*` "workspaces": bakes write zero events, host records, or profile state into any `~/.minds*/mngr` root (or `~/.mngr`).
- Each `admin pool create` invocation (later `minds-admin pool create`) creates one throwaway host dir under `~/.cache/mngr-bake/`, threads it (plus an inert `MNGR_PREFIX` such as `mngr-bake-`, deliberately not an extension of any app prefix) into every inner `mngr` subprocess, and deletes it when the whole invocation succeeds.
- On any failure (an exception, or a report with `failed > 0`) the namespace dir is retained and its path printed; there is no success-path retention flag. Each bake invocation first sweeps retained dirs older than 7 days.
- Remote/baked state is byte-identical to today: same labels (`user_created=true`, `is_primary=true`), same pool rows, same adopted-workspace behavior. Only the operator-local side effects change.
- Existing pollution self-heals: the app's clean-snapshot prune already removes remembered `slice-*` hosts on the next app start, and with no new bake events they stay gone. No destroy-event emission, no migration.
- The bake no longer depends on (or is breakable by) the operator's desktop-app mngr profile; the stale-agent-types failure class is gone by construction. (The now-redundant `seed_activated_env_agent_types()` call in `minds pool create` is removed in phase 2 together with the rest of `minds pool`.)

### Surface move

- `mngr imbue_cloud admin ...` no longer exists -- deleted outright from the public plugin, no stub. Public `mngr imbue_cloud` keeps only the user-facing surface (auth, account, hosts, keys, bucket, shares, sync).
- `minds env`, `minds pool`, `minds server`, `minds paid` no longer exist -- deleted outright, no tombstones. `minds` keeps only the app surface (`minds run`).
- The new `minds-admin` CLI carries the consolidated surface with subcommand names kept verbatim: `minds-admin {env, pool, server, paid, account, workspaces, sweep, relays, repair-keys}`.
- Developer/operator activation ceremony becomes `eval "$(uv run minds-admin env activate <name>)"`; behavior (exports, deploy mode, generation auto-wipe, `--create`) is unchanged -- pure relocation.
- Regular users are unaffected: the packaged Electron app sets `MINDS_ROOT_NAME` and passes `--config-file` itself and never used `minds env`.
- `minds-admin` commands are env-aware in-process: Vault/tier resolution happens inside the command; `--database-url` and env-var overrides (`MINDS_HOST_POOL_DSN`, `POOL_SSH_PRIVATE_KEY`, `MINDS_ADMIN_KEY`, OVH vars) remain for non-activated one-off use.
- `minds-admin env destroy` reaps the env's unleased slices in-process (the old public-minds -> operator-code dependency disappears because env destroy itself moved).
- The public mirror never sees `apps/minds_admin`; the public minds package still builds, collects, and passes CI with the operator modules gone.
- justfile recipes keep their names (`bake-slice-prod`, `prep-server`, `list-pool-hosts`, `audit-boxes`, `add-paid-email`, ...) but route to `uv run minds-admin ...`.

### Part B: server lifecycle convergence

- `server setup` converges a box to the same state as `server prep`: it keeps its destructive-initial steps (OS reinstall with injected host key, SSH wait, status machine) and then runs the **same composed prep** (base script + collector install + verification) before marking the box `ready`.
- When the tier has observability configured, a failed collector install (or a collector that is not active post-install) **fails the prep**, and `setup` refuses to mark the box `ready`. When the tier has no observability credential, prep skips the collector cleanly, exactly like today.
- Post-install verification: the composed prep checks the `otelcol-contrib` systemd unit is active on the box and fails loudly if not.
- The collector assembly moves from `private.just` + `scripts/provision_observability_config.py` into `minds-admin server prep`/`setup` (in-process import of the observability renderer); `--extra-prep-script` survives as an ad-hoc escape hatch. Relay provisioning keeps using the script (out of scope).
- New env-aware commands `minds-admin server order / await-delivery / setup` resolve OVH credentials (`secrets/minds/<tier>/ovh`), the pool DSN, and the pool SSH key from the activated tier's Vault entries -- the runbook's Step 0 export block dies.
- New just recipes: `just order-server`, `just await-delivery`, `just setup-server` (thin aliases); the production runbook collapses to one command per box per step.

## Implementation plan

### Phase 1 (Part A) -- all in `libs/mngr_imbue_cloud`

- `bake/pool_bake.py`:
  - New `ephemeral_bake_namespace()` context manager: creates `~/.cache/mngr-bake/<unique>/host_dir`, yields the extra-env mapping (`MNGR_HOST_DIR`, `MNGR_PREFIX=mngr-bake-`), deletes the dir on clean exit, retains + logs the path on error/failure signal (context manager takes a "was the run successful" signal, or is split into create/cleanup helpers so `allocate_slices` can decide from the report).
  - New `sweep_stale_bake_namespaces()` helper: removes retained dirs older than 7 days (by mtime) under the parent; called at invocation start; logs what it removed.
  - `run_mngr_command` / `bake_pool_host` already accept `extra_env` / `extra_create_env` -- the namespace mapping merges into them.
- `cli/server.py`:
  - `allocate_slices`: sweep stale namespaces, create the ephemeral namespace for the whole invocation, merge its mapping into `bake_worker_kwargs`' create env (`_bake_one_slice` -> `bake_pool_host(extra_create_env=...)`), delete on full success, retain + print on any failure. The direct-SSH container transport (`_slice_run_in_container`) and the orphan reap are unaffected (no local mngr state involved).
- Unit tests: namespace lifecycle (success deletes, failure retains, sweep removes only >7d dirs), env threading (fake runner asserts the inner argv env carries the ephemeral `MNGR_HOST_DIR`/`MNGR_PREFIX`), existing `pool_bake_test.py` / `server_test.py` updated.
- Changelog entry: `libs/mngr_imbue_cloud/changelog/mngr-cleanup-bakes.md`.

### Phase 2 (surface move) -- create `apps/minds_admin`, delete the public operator surface

- New `apps/minds_admin`:
  - `pyproject.toml` with console script `minds-admin` (`imbue.minds_admin.cli_entry:main` or similar); workspace member; depends on `imbue-minds`, `imbue-mngr-imbue-cloud`, `imbue-observability` (phase 3), plus direct deps the moved code already uses (`psycopg2`, `click`, `tabulate`, `psutil`, ...).
  - Standard project scaffolding: `README.md`, `test_ratchets.py`, `changelog/`, import-linter layers contract.
  - NOT added to `mirror/copy.bara.sky` -- private by default.
- Moves **from `libs/mngr_imbue_cloud`** into `apps/minds_admin` (imports flipped to the new package; public lib keeps no re-exports):
  - `cli/admin.py`, `cli/server.py`, `cli/paid.py`, `cli/accounts_admin.py`, `cli/workspaces_admin.py`, `cli/sweep_admin.py`, `cli/relays_admin.py`, `cli/repair_keys_admin.py` (reworked from `mngr imbue_cloud admin <x>` groups into env-aware `minds-admin <x>` groups).
  - `bake/` (pool_bake, bake_source) and the operator-only slices modules: `bare_metal_db.py`, `bare_metal_prep.py`, `ordering.py`, `pricing.py`, `key_repair.py`, `autostart_backfill.py` (the last has since been deleted outright, after its one-time rollout completed at minds-v0.3.17).
  - The operator-relevant parts of `cli/_common.py` (`resolve_pool_database_url`, `emit_json`, `fail_with_json`) -- move or split, whichever leaves the public user CLI self-contained.
  - Stays public (used by the slice provider backend or the user surface): `providers/**`, `plugin/**` (minus the deleted admin CLI registration), `slices/bare_metal.py` (sizing/naming), `slices/lima_slice.py`, `slices/lima_slice_client.py`, `slices/box_image_cache.py`, `slices/lima_box_image_cache.py`, `connector/**`, `wire*`, user CLI modules, `config.py`, `data_types.py`, `primitives.py`.
  - `cli/root.py`: drop the `admin` group and all admin imports; minor version bump; changelog entry noting the removal.
- Moves **from `apps/minds`** into `apps/minds_admin`:
  - CLI: `cli/env.py`, `cli/pool.py`, `cli/server.py`, `cli/paid.py`, `cli/_activated_env.py` (the deploy-mode gate and activation checks move with their only consumers).
  - Operator-only envs machinery: `envs/provisioning.py`, `envs/per_env_deploy.py`, `envs/providers/{modal_env,neon_db,supertokens_app,workspace_storage}.py`, `envs/secret_lifecycle.py`, `envs/recover.py`, `envs/migrations.py`, `envs/generation.py`, `envs/local_store.py`, `envs/health_check.py`, `envs/mngr_agent_cleanup.py`, `envs/r2_cleanup.py` (final list governed by the split rule below), with their tests and any `testing.py`/`conftest.py` fixtures only they use.
  - Split rule: **anything imported by minds app-runtime code or the public test/conftest surface stays in minds; anything imported only by the operator CLI moves.** Known stays: `envs/docker_cleanup.py` (imported by `cli/run.py` + desktop_client), `envs/primitives.py`, `envs/paths.py`, `envs/vault_reader.py` (imported by the public `imbue/minds/deployment_tests/helpers.py`, which the app conftest imports), `bootstrap.py`, `config/loader.py`, the per-tier `client.toml` files (Electron bundle). The per-tier `deploy.toml` files stay in minds too (the config loader and its tests live there); `minds_admin` reads them via the minds config loader.
  - `cli_entry.py`: remove `env`/`pool`/`server`/`paid` registrations (keep `run`); remove the `seed_activated_env_agent_types()` bake-path call (dies with `cli/pool.py`; the seed function itself stays -- desktop startup still uses it).
  - `minds run`'s activation-refusal messages and any error text referencing `minds env activate` updated to `minds-admin env activate`.
- Repo-wide updates:
  - `justfile` / `private.just`: recipes keep their names, bodies route to `uv run minds-admin ...`; recipe comments updated.
  - `apps/minds/deployment_tests/**` (private): update imports of moved envs modules to `imbue.minds_admin.*`.
  - Docs/skills sweep via grep checklist: `grep -rn "minds env activate\|minds env deploy\|minds pool \|minds server \|minds paid \|imbue_cloud admin"` across `docs/`, `apps/minds/docs/`, `.claude/skills/`, `CLAUDE.md` files, `README`s. Representative known touchpoints: `environments.md`, `host-pool-setup.md`, `production-release-deployment.md`, `staging-bringup.md`, `vault-setup.md`, `observability-bringup.md`, `dev-setup.md`, `release.md`, the `minds-justfile` / `minds-dev-workflow` skills, `apps/minds/README.md`, `libs/mngr_imbue_cloud/README.md`.
  - `scripts/provision_observability_config.py` / `provision_dev_relay_config.py` keep working unchanged (they import `imbue.minds.envs.vault_reader`, which stays).
- Public-mirror invariants checklist (part of the phase-2 PR):
  - `apps/minds_admin` absent from `copy.bara.sky`'s allowlist.
  - The allowlist's exclude comments about `imbue/minds/envs` / `config/envs` updated to describe the new (smaller) public surface.
  - No public file imports a moved module (verified by the public-CI equivalent: build the mirror file set and run the import check / full tests on it, or rely on the mirror repo's CI on the first sync -- prefer catching it pre-merge with a local check).
  - `imbue-minds` still builds and its public tests still collect with the moved modules gone.
- Ratchets: `apps/minds_admin/test_ratchets.py` created via the sync script; tighten counts in `mngr_imbue_cloud`/`minds` where violations moved away.
- Changelog entries: `apps/minds_admin`, `apps/minds`, `libs/mngr_imbue_cloud`, `dev` (justfile/scripts).

### Phase 3 (Part B) -- all in `apps/minds_admin`

- `server` group:
  - New `order` / `await-delivery` / `setup` commands: same click surfaces as the old admin ones, plus tier-aware resolution -- OVH creds from `secrets/minds/<tier>/ovh`, pool DSN, pool SSH key -- with env-var/flag overrides preserved.
  - Composed-prep function shared by `prep` and `setup`: base `build_box_prep_script` + collector install (when configured) + `--extra-prep-script` (escape hatch) + post-install verification (`systemctl is-active otelcol-contrib` over the same pinned-host-key SSH session, fail loudly).
  - Collector assembly in-process: resolve the tier's boxes ingest credential from Vault (ported from `provision_observability_config.py collector-env`), render via `imbue.observability.collector_install.render_collector_install_script`. Missing/empty credential = clean skip (today's exit-3 semantics); present credential + failed install or inactive unit = prep failure; `setup` then refuses to flip the box to `ready`.
- `justfile`: new `order-server`, `await-delivery`, `setup-server` recipes (thin aliases); `prep-server` reduced to a thin alias (collector logic now inside `minds-admin`); `_derive_observability_tier` shell block retired from the prep path (relay recipes keep their copy).
- `scripts/provision_observability_config.py`: `collector-env` mode kept only for the relay recipes; the boxes-sender path is noted as served by `minds-admin` (or the sender arg restricted, whichever is smaller).
- Docs: `production-release-deployment.md` rewritten around the new commands (Step 0 export block deleted; Steps 2-6 become dry-run order -> order -> await -> setup -> bake, one command per box); `host-pool-setup.md` Step 5 updated; runbook's open TODO about lifecycle recipes resolved.
- Changelog entries: `apps/minds_admin`, `dev`.

## Implementation phases

- **Phase 1 -- bake namespace isolation (Part A).** Lands in `libs/mngr_imbue_cloud` on this branch (`mngr/cleanup-bakes`). After it: bakes are namespace-isolated for every caller, the desktop-app symptom is fixed, nothing else changes. System fully working.
- **Phase 2 -- the surface move.** Creates `apps/minds_admin`; relocates the imbue_cloud admin group, the minds operator CLIs, and the env machinery; deletes the public surfaces; updates recipes/docs/mirror comments. Pure relocation -- no behavior changes beyond command spellings. System fully working (operators switch to `minds-admin`).
- **Phase 3 -- server lifecycle convergence (Part B).** Builds the composed prep + env-aware order/await-delivery/setup on the new surface; fail-closed collector; recipes + runbook rewrite. System fully working with the new operator UX.

## Testing strategy

- Phase 1 (unit-level only, per decision):
  - Namespace lifecycle: success deletes the dir; failure retains it; the sweep removes only dirs older than 7 days and never the active one.
  - Env threading: a fake subprocess runner asserts every inner `mngr create` env carries the ephemeral `MNGR_HOST_DIR`/`MNGR_PREFIX` and that they override inherited values.
  - Existing `pool_bake_test.py` / `server_test.py` suites updated and green.
  - Manual verification (not CI): one `just bake-slice-dev` against a dev box from an activated dev env; confirm the env's `~/.minds-dev-*/mngr` gains no new discovery events/host records, the app shows no `slice-*` rows, and the pool row + lease still work.
- Phase 2:
  - Moved tests move with their modules and stay green; `just test-offload` for the whole tree.
  - Import-linter contracts for `minds_admin`; meta-ratchets (`test_meta_ratchets.py`) satisfied by the new project's `test_ratchets.py`.
  - Mirror invariants: verify no public file references moved modules (grep + building/collecting the public subset); confirm `copy.bara.sky` untouched by the new app.
  - Manual smoke: `minds-admin env activate/list/deactivate`, `minds-admin pool list`, `minds-admin server list` against a dev env; `just minds-start` still launches with the new activation ceremony.
- Phase 3:
  - Unit tests: composed-prep assembly (base + collector + extra script ordering), fail-closed vs clean-skip branching on the Vault credential, argv construction for `order`/`await-delivery`/`setup`, verification-command rendering.
  - Manual verification: `just prep-server` on a staging box -> collector unit active; `setup` path exercised on the next real box addition (order --dry-run in CI-less manual check); runbook walked once end-to-end at the next deployment.
- Edge cases to cover across phases: bake invocation killed mid-run (retained namespace, orphan reap still works); two concurrent bake invocations (distinct namespaces, no interference); tier without observability (clean skip preserved); non-activated invocation with explicit overrides (still works on the new surface).

## Open questions

- Exact stays/moves boundary for a few `envs` modules (`health_check`, `mngr_agent_cleanup`, `r2_cleanup`, `migrations`): governed by the import-direction rule at implementation time; if one turns out to be imported by app-runtime code, it stays and `minds_admin` imports it from minds.
- Whether the phase-2 deletion of `minds env` needs any grace for in-flight developer shells/scripts beyond same-PR doc updates (decision so far: no tombstones; a bare "No such command" is acceptable).
- Whether `minds-admin` should eventually absorb the remaining operator shell surface -- share-relay provisioning, observability bring-up, `scripts/delete_accounts.py` -- explicitly out of scope here; future consolidation.
- Whether removing the boxes-sender path from `provision_observability_config.py` in phase 3 breaks any other recipe (audit at implementation; relay recipes keep the script).
- Post-phase-2, whether `imbue-mngr-imbue-cloud`'s PyPI release notes should call out the admin removal beyond the changelog (decision so far: minor bump + changelog entries suffice).
