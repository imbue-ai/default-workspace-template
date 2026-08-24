Add the CI pool provisioning surface for remote-workspace release tests (specs/remote-workspaces-in-ci.md):

- `minds-admin pool create --content-addressed-cache`: key the per-box image cache on a hash of the workspace content (a throwaway-index git tree hash computed after the vendor-mngr sync), so unpinned CI bakes get the seed-once/load-many behavior and identical content re-bakes warm.

- `minds-admin server import-boxes`: id-preserving upsert of the ready `bare_metal_servers` rows from a source pool DB (the standing CI infra DB) into the target env's DB, making the standing CI boxes leasable from each per-run ci env.

- `minds-admin server sweep-ci-slices`: age-based sweep that destroys stale `ci-*`-owned slices (and orphan disks) left on the ready boxes by crashed release runs, reporting non-ci-tier resources as contamination without touching them.

- `test_deployments.py bake-pool` (`just bake-ci-slices`): the release flow's bake stage -- sweep, import the boxes, clone the requested template ref (read-only deploy key from `secrets/minds/ci/dwt`), bake N slices with the content-addressed cache, and record the stamped `repo_branch_or_tag` in the run's `deployment_envs.json`.

- `test_deployments.py bake-pool` now republishes the template read key to the env's per-run Vault path (`minds/ci/runs/<env>/pool`) before the CI bake (so a cold bake cannot outlive the test job's 30-minute Vault token TTL), letting the separately-authorized test job materialize the template checkout without reading the static `minds/ci/dwt` entry; env destroy deletes the per-run pool secrets alongside the shared-env ones.

- The env deploy's per-run secret publication now includes the tier's `MINDS_ADMIN_KEY`, so the separately-authorized CI test job can call the connector's admin endpoints (backup-retention reap) without reading the static supertokens Vault entry.
