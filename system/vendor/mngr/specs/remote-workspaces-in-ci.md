# Remote workspaces in CI (release tests against real bare-metal slices)

## Overview

- Remote workspaces (imbue_cloud pool hosts, i.e. lima/QEMU slices carved on bare-metal boxes) are the primary way users consume minds, but nothing in CI has ever exercised them: the per-run `ci-*` envs have empty pools, so the one test that leases a real slice (`apps/minds/deployment_tests/test_workspace_stop_start.py`) always skips, and the bake/lease/user-isolation test was explicitly deferred by [specs/minds-deployment-tests.md](./minds-deployment-tests.md).
- This spec adds standing CI-tier bare-metal capacity plus a per-run slice pre-allocation stage to the opt-in minds release tier (`run_minds_release_tests` dispatch in `ci.yml`), so release tests lease pre-baked slices exactly the way real users do.
- The design deliberately reuses existing machinery: the `minds-admin server` / `pool` provisioning flow, the per-box image cache with its seed/fill phases, the per-run ci env orchestration, and the existing sweeps.
  The only substantive new pieces are a content-addressed image-cache key (so CI bakes get the "first slice builds, the rest load" behavior despite unpinned content), a small box-import step, a CI slice sweep, and a DB-free cache pre-warm verb.
- Release runs are serialized with a GitHub Actions concurrency group; the on-box slot guard and atomic lease claims remain the hard backstop against overlap.
- Because every fresh release run bakes new content (the cache key is a content hash and the code changes between runs), every run pays one cold image build per box used.
  The pre-warm stage exists to run that build in parallel with env deploy instead of after it; same-content re-runs (flaky retries, single-test re-runs, local iteration) are warm.

## Current state (phase 0 -- already done, operational)

These resources exist and are the substrate the rest of this spec builds on:

- **Two CI-tier bare-metal boxes**, ordered 2026-08-20, same type as production (`24sys032-us`, 128 GB RAM, `softraid-2x960nvme`, 8c/16t, options `bandwidth-1000-24sys-us` + `vrack-bandwidth-500-24sys-us`, ~$100/mo each):
  - `ee903147-9021-437a-9fb9-2ca202c4ca66` in `vin` (US-EAST-VA), OVH order 8588400.
  - `7e76f6be-9c5d-4c7c-b4df-961bd18de1d5` in `hil` (US-WEST-OR), OVH order 8588402.
  - Each yields 14 slices of 8 GB at production slice sizing.
- **A standing CI-tier "infra" Neon database** (project `minds-ci-infra`, `host_pool` DB with all connector migrations applied).
  Its pooled DSN lives at the previously-templated-but-empty Vault leaf `secrets/minds/ci/neon/DATABASE_URL`.
  This DB is the canonical registry of CI bare-metal boxes: the `bare_metal_servers` rows (status, public address, pinned sshd host key, slice sizing) live here, and the `minds-admin server` order/await/setup/prep/list commands operate against it.
- The CI tier's `ovh` and `pool-ssh` Vault entries were already populated; the `neon` leaf was populated as part of this work.
- Both boxes are provisioned to `ready` (OS reinstalled, prepped, host keys recorded).
- `secrets/minds/ci/storage/*` is populated (mirrored from the dev tier's entry, sharing its bucket) with `WORKSPACE_STOP_RETENTION_SECONDS=60`, so per-run ci envs deploy with workspace stop/start enabled and a CI-sized retention window.
- A read-only deploy key on the template repo lives at `secrets/minds/ci/dwt/DWT_READ_KEY_B64` (deliberately not the read-write vendor-sync key).

**Note:** the `minds-admin` commands resolve the pool DSN for `ci-*` envs from the per-env `secrets.toml`, not from tier Vault, so operating against the infra DB means exporting `MINDS_HOST_POOL_DSN` from the Vault leaf (see the runbook section below).

## Expected behavior

### The per-run release flow

All of the following is gated on the existing `workflow_dispatch` + `run_minds_release_tests=true` opt-in (plus the TMR minds schedule, which dispatches the same flow).
Normal pushes and PRs never touch the boxes, never bake, and never run these steps.

1. **Warm the image cache (phase 2, designed here, may ship after phase 1).**
   A job started at workflow begin runs `minds-admin pool warm-cache` against the selected box(es): it carves a throwaway slice (no DB row), builds the run's workspace image via the existing seed machinery, publishes the box tar under the run's content-addressed tag, and destroys the throwaway slice.
   Runs in parallel with `build-minds-ci-env`, so the cold image build overlaps env deploy instead of following it.
2. **Deploy the per-run ci env** (existing `build-minds-ci-env` job, unchanged in essence).
3. **Import the CI boxes into the per-run env** (new step in `build-minds-ci-env`, after the env deploy): a new `minds-admin server import-boxes` command copies the `ready` `bare_metal_servers` rows (same UUIDs, same pinned host keys) from the infra DB into the per-run env's `host_pool` DB, so the env's connector can SSH the boxes at lease/release time.
4. **Sweep stale CI slices** (new step, same job): destroy any `ci-*`-owned lima instance on the boxes whose env no longer exists or that is older than a staleness threshold (see "Sweeps" below).
   Running the sweep *before* baking guarantees a wedged prior run cannot eat slots and cause spurious capacity failures.
5. **Bake the run's slices** (new step, same job): one `minds-admin pool create` invocation with `--workspace-dir` pointing at the run's default-workspace-template checkout, `--mngr-source` pointing at the run's mngr checkout, `--content-addressed-cache`, and `--count` = the number of remote-workspace tests selected for the run plus two spares.
   The existing seed/fill fan-out applies: with the tar already published by the warm job (or by this invocation's own seed phase, in the phase 1 world), every slice is carve + `docker load` + finalize.
   The rows are stamped with `repo_branch_or_tag` = the run's resolved template SHA (see "What content gets baked" below); that value plus the canonical `repo_url` (fast-path matching requires the full identity pair) are written into `deployment_envs.json` so tests lease fast-path matches.
6. **Run the tests** (existing `test-minds-snapshot` `minds_services` step and `test-minds-release`): remote-workspace tests lease from the pool like real users and release (which destroys the slice and frees the slot -- leases are consumed, never returned to `available`).
7. **Tear down** (existing `destroy-minds-ci-env` job, extended): `minds-admin env destroy` already destroys the env's unleased slices; the job additionally re-runs the CI slice sweep as the crash backstop.

### What content gets baked

- The default-workspace-template content is selected by a new `template_ref` dispatch input on `ci.yml`, defaulting to `main`, frozen to a SHA at run start (mirroring `minds-launch-to-msg.yml`).
- The runner clones the template repo read-only using a dedicated read-only deploy key stored in Vault (`secrets/minds/ci/dwt/DWT_READ_KEY_B64`; deliberately not the read-write `minds/release/DWT_VENDOR_SYNC_KEY_B64`).
- The vendored mngr inside the baked image is the run's own mngr checkout, threaded via `--mngr-source` (the bake's existing vendor-sync + reset-on-finish behavior applies).
- The lease-attribute `repo_branch_or_tag` stamped on the rows is the resolved template SHA, so a fast-path lease from this run can only adopt this run's bake.

### Content-addressed image cache

Today the per-box image cache is only used by production `--from-tag` bakes, keyed `default-workspace-template:<tag>`; `--workspace-dir` bakes always set `default_workspace_template_cache_tag = None` because a branch label is mutable content and therefore an unsafe key.

- A new opt-in flag on `minds-admin pool create` (and `warm-cache`), `--content-addressed-cache`, derives the tag from the *content* instead: `default-workspace-template:content-<git-tree-hash>`, where the hash covers the exact bake inputs -- the workspace dir's rsync-visible file set (same gitignore filter + excludes the vendor sync uses) *after* `--mngr-source` has been synced in.
  Concretely: a temporary git index is built over the workspace dir (`git add -A` through a throwaway `GIT_INDEX_FILE`, so tracked changes and untracked non-ignored files are all staged) and hashed with `git write-tree` -- stable across runs with identical content and blind to timestamps.
- Everything downstream is unchanged: the seed phase, the build lock with dead-seeder handoff, the fill-phase `docker load`s, and the playwright-derived-image build all operate on the tag string.
- Eviction is already handled: `save_image_from_slice` prunes every other tag's tar when publishing, so a box holds exactly one tar -- the most recent content.
  Consequence: a re-run stays warm only until a different-content run bakes on the same box.
  With release runs serialized this is exactly the desired behavior.
- The flag stays opt-in so operator dev bakes keep their current no-cache behavior (a single-slice dev bake would pay the tar save for no benefit).

**Note:** the docker build is not a pure function of the tree (base-image pulls, apt), so a cache hit can serve a slightly staler build than a from-scratch one would produce.
This is the same tradeoff production `--from-tag` caching already accepts.

### The cache pre-warm verb (phase 2)

`minds-admin pool warm-cache --server-id <id> --workspace-dir <dir> [--mngr-source <dir>] --content-addressed-cache`:

- Requires no database: slot/port reservation is purely on-box, and no `pool_hosts` row is written.
- Behavior: if the box already has the tar for the derived tag, exit 0 immediately (cheap no-op).
  Otherwise carve one throwaway slice, run the existing seed path (build base image, build the playwright layer, `docker save` to the box tar), then destroy the slice VM and disk unconditionally (`finally`).
- Warm slices have no owning env (the verb is DB-free), so their lima instance names carry the reserved pseudo-env label `ci-warm`, which the CI slice sweep treats like any other `ci-*` owner.
- Failure semantics: a failed warm leaves no tar and no lock (the existing stale-lock TTL covers a killed process); the bake stage's own seed phase then builds, so the warm job is advisory -- its failure should be surfaced but must not fail the workflow.
- The CI job wrapping it starts at workflow begin, parallel with `build-minds-ci-env`, and targets the same box the bake stage will select (see "Box selection" below); the selection logic must therefore be deterministic from run inputs plus infra-DB state, and is shared code between the two steps (`find_first_ready_server_in_datacenter`, applied by the orchestrator's `warm-pool-cache` and `bake-pool` commands over the same id-preserved `bare_metal_servers` rows).
- **Seed-phase handoff (what makes the overlap actually pay):** with the phase-1 structure the bake stage would still serialize its own seed slice behind the in-flight warm build (its seed slice just waits on the warm's tar), erasing the savings. The bake therefore skips its seed-first phase not only when the tar is already present but also when a *fresh build lock* is held on the tag (the warm job mid-build): the fan-out slices each block on the in-flight seed's tar inside their own `mngr create` (the provider's existing block-then-load with dead-seeder handoff), and if the warm dies one of them takes over the build. When neither tar nor lock exists (warm never started / crashed early), the bake seeds exactly as in phase 1.
- The two jobs resolve `template_ref` to a SHA independently; if the ref advances between the two clones the warm builds different content and is simply wasted (the bake seeds itself) -- accepted, since release runs pin content in practice.

### Box selection and capacity accounting

- Per run, all slices are packed onto **one** box (cold seeds on additional boxes multiply bandwidth/compute, and one 14-slot box comfortably fits the roster plus spares).
- The bake step selects the `ready` box serving the requested lease region's datacenter (default region `US-EAST-VA`, i.e. the `vin` box); the second box is capacity headroom, hardware redundancy, and the home for future region-spanning tests.
  The bake's own on-box occupancy check is what actually guards capacity.
- Slice count comes from a `minds_pool_slice_count` dispatch input whose default is a constant maintained next to the test roster (remote-workspace test count + 2 spares, for flaky retries and post-run debugging); a count past the box's free slots fails the bake's capacity check loudly rather than being clamped.
  Deriving the count from pytest collection would be more precise but is not worth the coupling; a filtered re-run passes a smaller count explicitly.
- Leases are consumed: `POST /hosts/{id}/release` destroys the slice VM and deletes the row, so every test run of a lease-consuming test burns one slice.
  Re-running a test therefore needs a fresh slice; with a warm cache a top-up bake is carve + load, minutes.

### Sweeps (leak safety)

A new `minds-admin server sweep-ci-slices` command (run against the infra DB's box rows, with the CI pool key):

- Enumerates the lima instances + disks on each `ready` CI box, parses the owning env from the slice resource name (the connector's `slice_name_env_owner` logic).
- Destroys any `ci-*`-owned slice whose on-box age exceeds a staleness threshold (default 4 hours, matching the ci env sweep), including warm-verb throwaway slices.
  Phase 1 deliberately implements only the age criterion (no Modal-env-existence check): a slice whose env died young is torn down by the normal `minds-admin env destroy` path, and anything that survives it ages into this sweep; release runs are serialized, so nothing younger than the threshold can be another run's live slice.
- Never touches slices owned by non-`ci` envs (there should be none on a CI-tier box; if found they are reported loudly as tier contamination, mirroring `audit-boxes`).
- Invoked in the bake-stage prologue and in `destroy-minds-ci-env`; existing nets (the ci Modal-env sweep, `minds-admin env destroy`'s unleased-slice teardown, the connector's box reconcile) are unchanged.

### Concurrency

- Release runs are serialized at the *workflow* level with a conditional `concurrency` group on `ci.yml`: release dispatches (`run_minds_release_tests=true`) share the group `minds-remote-release` (`cancel-in-progress: false`); every other trigger gets a unique per-run group so normal CI is never serialized.
- A job-level group deliberately does NOT work here: the warm job and `build-minds-ci-env` must run in parallel *within* one run, and same-group jobs serialize against each other even inside a single run.
- The TMR minds schedule is unaffected: it deliberately excludes the capability suites (`minds_deployment` / `minds_services`), so it never touches the boxes.
  If a scheduled remote-workspace run is added later, it must trigger via `gh workflow run ci.yml -f run_minds_release_tests=true` so it inherits the same group.
- Known GitHub semantics: only the newest pending run is kept in a group's queue; an older pending run is cancelled.
  Acceptable at this run frequency.
- The concurrency group does not govern humans; policy is that operators never bake onto CI-tier boxes by hand (tier exclusivity already prevents dev-env tooling from reaching them).
- Belt and braces: the on-box cross-env slot guard and the atomic DB lease claims mean an accidental overlap fails loudly (box-full / 503), never corruptly.

### Stop/start integration

- `secrets/minds/ci/storage` is populated by mirroring the dev tier's entry (bucket, S3 creds, KEK -- the ci tier already mirrors dev credentials by design), with one difference: `WORKSPACE_STOP_RETENTION_SECONDS=60`, so the stop path does not wait out the default one-hour local-retention window.
- The env deploy already stamps a per-env `WORKSPACE_STORAGE_KEY_PREFIX` (`<env>/`) for per-env-Modal-env tiers and `minds-admin env destroy` already reclaims the prefix, so no code changes are needed -- populating the Vault entry lights the whole path up.
- `test_workspace_stop_start` then stops skipping.
  **Measured (phase 1):** the full cycle against the standing vin box took ~2.6 hours -- the ~13 GB artifact upload ran at ~1.4 MB/s effective, far below the 6-25 MB/s the stop/start docs assume -- which no CI job budget fits.
  The test is therefore gated behind an explicit `MINDS_STOP_START_RELEASE_TEST=1` opt-in (the `MNGR_AWS_RELEASE_TESTS` pattern) until one of the open-questions follow-ups lands; the CI services step does not set it.

### Empty-pool semantics

- Once CI guarantees capacity, "pool empty" must be a failure, not a skip: a broken bake stage silently turning the suite green is the worst outcome.
- Default: remote-workspace tests **fail** when the lease returns 503-no-capacity.
- Opt-out: `MINDS_ALLOW_EMPTY_POOL=1` restores today's skip behavior, for runs against envs that legitimately have no pool.
  The `just minds-test-services-against` recipe sets it by default (arbitrary dev envs may have no baked box); the CI jobs never set it.

### Re-running individual tests (dev loop)

- **In CI:** a new `minds_release_test_filter` dispatch input (mirroring `minds_snapshot_test_filter`) scopes the run to matching tests; the bake step sizes `--count` from the selection, so a one-test re-run costs env-deploy + one bake + the test.
- **Locally:** `just minds-test-deployment-up default` stands up the shared env once; a new `just bake-ci-slices <count>` recipe (thin wrapper over `import-boxes` + `pool create` against the up'd env) tops up slices, and the printed pytest command re-runs at will.
  `just minds-test-services-against dev-<you> ...` remains the fully-local variant against the operator's own dev box.

## Test inventory

### Phase 1 (this work)

- `test_lease_isolation_and_release` (`minds_services`; the deferred test from minds-deployment-tests.md, slice-era): lease a pre-baked slice as verified user A, assert user B cannot see the host via the user-facing API (404, not 403), release, assert the slot is freed and the row gone.
- `test_fast_path_create_and_destroy` (`minds_services`): configure an imbue_cloud provider instance against the per-run env, `mngr create` with `-b fast_mode=require` and the run's `(repo_url, repo_branch_or_tag)` pair (fast-path matching requires both), assert the pre-baked agent is adopted and its services boot (the workspace's `system_interface` answers), then `mngr destroy` the workspace and drive the explicit `hosts release` path (destroy itself defers lease release to GC's grace period), asserting the lease disappears from the connector.
  This is the layer where adoption, key rotation, and workspace boot are actually exercised.
- `test_workspace_stop_start` (existing): capacity + storage config are now provided, but the measured ~2.6h cycle keeps it behind the `MINDS_STOP_START_RELEASE_TEST=1` opt-in for now (see "Stop/start integration").

### Later (enabled by this work, not in scope)

- A slow-path (`fast_mode=prevent`) rebuild test (uncommon path; lower priority).
- Desktop-client / web-driven remote-workspace creation tests (the stated end goal; adds an Electron-on-runner and real-login dimension).
- Region-spanning tests (both boxes; pays a second seed).

## Changes

### mngr_imbue_cloud (`libs/mngr_imbue_cloud`)

- `MAX_SLICE_ENV_NAME_LENGTH` + `assert_env_name_fits_slice_names`: limactl caps identifiers at 76 chars and its ssh control-socket path must fit `UNIX_PATH_MAX`, so env names past the derived cap fail fast at bake time instead of dying in `limactl` mid-carve.
  Found by this work's first real bake: with the full 32-hex host id the orchestrator's `ci-<timestamp>-<8-hex>` names blew both limits, so the host-id hex embedded in slice lima names is truncated to 16 chars (`SLICE_HOST_ID_HEX_LENGTH`; the owner parse accepts both lengths) and the orchestrator names keep their 8-hex suffix.

**Note:** the content-addressed cache-tag derivation ended up in `minds_admin`'s bake package (it is a pure function over a workspace dir, and only the bake CLI computes it); the provider's existing `default_workspace_template_cache_tag` threading is reused unchanged.

### minds_admin (`apps/minds_admin`)

- `minds-admin pool create --content-addressed-cache` flag (derives the tag after the `--mngr-source` vendor sync; mutually exclusive with `--from-tag`'s tag-derived cache).
- `minds-admin pool warm-cache` (phase 2): DB-free seed-only verb, per above.
- `minds-admin server import-boxes --source-database-url <dsn>`: copy `ready` `bare_metal_servers` rows into the target env's DB (idempotent; same UUIDs).
- `minds-admin server sweep-ci-slices`: the CI slice sweep, per above.
  The slice-name-to-owning-env parse (`slice_name_env_owner`) already lives in `mngr_imbue_cloud`'s slices module and is imported directly; only the connector carries its own copy.
- Box-selection helper shared by the warm and bake steps.
- `scripts/test_deployments.py`: optional flags so `up` can import boxes + bake (used by the local iterate mode and by CI's `build-minds-ci-env`).

### minds (`apps/minds`)

- `deployment_tests`: the two new tests; `deployment_envs.json` gains the run's `repo_branch_or_tag` (and pool box info as needed); the 503-lease handling flips from skip to fail unless `MINDS_ALLOW_EMPTY_POOL=1`.

### dev (workflows, justfile)

- `ci.yml`: `template_ref` + `minds_release_test_filter` + `minds_pool_slice_count` dispatch inputs; bake/import/sweep steps in `build-minds-ci-env`; the warm job (phase 2); sweep in `destroy-minds-ci-env`; the conditional workflow-level `minds-remote-release` concurrency group.
- `private.just`: `bake-ci-slices` recipe; `minds-test-services-against` sets `MINDS_ALLOW_EMPTY_POOL=1`.

### Vault / ops (no code)

- `secrets/minds/ci/storage/*` mirrored from dev + `WORKSPACE_STOP_RETENTION_SECONDS=60`.
- `secrets/minds/ci/dwt/DWT_READ_KEY_B64`: read-only deploy key on the template repo.
- Both CI boxes taken through `await-delivery` + `setup-server` to `ready` (in flight as of this spec).
- Vault role updates if `minds_ci_env_gh` lacks read on any of the paths above (verify during implementation).

## Wall-time model

| Stage | Cold (content changed) | Warm (same content) |
|---|---|---|
| env deploy | 10-15 min | 10-15 min |
| seed build (one per box used) | 15-25 min | skipped (tar present) |
| per-slice carve + load + finalize | ~5-10 min, parallel | same |

- Phase 1 (no warm job): setup is env-deploy then bake, ~30-40 min cold.
- Phase 2 (warm job at workflow start): setup is ~max(env-deploy, seed) + fill, ~25-30 min cold.
- The `minds_deployment` group and `build-minds-snapshot` run in parallel with all of it, unchanged.

**Measured (phase 1, dispatch 32522869366, count=5, cold content):** the table above was
pessimistic. On the real runners, env deploy took 4.1 min and the whole bake step 11.5 min
(sweep 6s, import-boxes 11s, template clone 11s, vendor sync + content tag 5s, seed slice
6.7 min -- of which the image build + `docker save` was ~5.4 min -- then the 4-slice fill
4.3 min), for ~16.4 min of `build-minds-ci-env` wall time including ~2.5 min of runner
setup. The phase-2 overlap therefore targets the ~5.5 min from bake-step start to
tar-published: with the warm job seeding from workflow start (tar lands ~8 min in, roughly
when the deploy-side job reaches its bake step) plus the seed-phase handoff and the fill
fan-out sized to the whole roster (`--max-concurrency` = count, capped at 8), the expected setup wall
time is ~12 min. Per-dispatch measurements live in the PR discussion.

## Operator runbook pointers

Operating the standing CI boxes uses the existing `minds-admin server` flow with the CI tier activated and the infra DSN exported:

```bash
eval "$(uv run minds-admin env activate --create ci-infra)"
export MINDS_HOST_POOL_DSN=$(vault kv get -field=value -mount=secrets minds/ci/neon/DATABASE_URL)
just list-servers            # or await-delivery / setup-server / prep-server <id>
```

The `ci-infra` env root is activation-only scaffolding (no Modal env, no deploy); the ci Modal-env sweep never sees it.
A box replacement is: order + setup the new box (rows land in the infra DB), then destroy the old box's row and cancel the OVH service.

## Open questions and risks

- **Bake stability from a GitHub runner:** the bake SSHes the box and streams a multi-GB image build; a runner network blip mid-bake wastes the run.
  The per-slice bounded retries in the bake fan-out absorb most of this; if it proves flaky in practice, the bake could move onto the box itself (out of scope for phase 1).
- **`import-boxes` drift:** a box replaced mid-run would leave the per-run env pointing at a dead row; acceptable (runs are short and serialized), and release handles unreachable boxes via the `removing`-row retry path.
- **Shared storage bucket with dev:** mirroring dev's bucket means ci workspaces' stop artifacts live beside dev's (namespaced per env).
  Consistent with the tiers' existing shared-credential posture; a dedicated ci bucket is a later hardening step.
- **Upload throttle (measured, follow-up needed):** the ~13 GB stop upload from the vin box ran at ~1.4 MB/s effective, making the full stop/start cycle ~2.6 hours -- the test is opt-in-gated until either the ci tier's upload throughput is raised or the test uses a much smaller workspace artifact.
