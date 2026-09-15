Integration branch for the imbue_cloud slice-fleet generation 2 program (raw qemu slices with routed-tap networking on Debian 13 boxes, replacing lima), merged together with `main` and the three pre-cutover fix PRs: imbue-ai/mngr-internal#857 (SSH certificates from Vault, #850), #855 (DHCP placement, #849) and #856 (the upstream artifact mirror, #851). The per-change details live in this directory's constituent entries, listed below.

Specs, blueprints and root tooling for the gen-2 program (`specs/slice-fleet*`, `blueprint/slice-fleet-*`, the credential-expiry reminder workflow, the `ssh-ca` Vault template, `uv.lock`).

Also on this branch: `test_meta_ratchets.py::test_every_project_has_pypi_readme` is marked flaky (it timed out at the 10s default under a loaded 4-worker run) and now parses each project's `pyproject.toml` with the stdlib `tomllib` instead of `tomlkit`, since the check is read-only.

Constituent entries: `new-fleet-base.md`, `new-fleet-runsc-prototype.md`, `new-fleet-phase-2.md`, `new-fleet-phase-3.md`, `new-fleet-phase-4.md`, `new-fleet-phase-4-impl.md`, `new-fleet-phase-5.5.md`, `mngr-variable-sizing.md`, `mngr-slice-fleet-gen2-phase-2.md`, `mngr-slice-fleet-gen2-phase-3.md`, `mngr-slice-fleet-gen2-phase-4.md`, `mngr-new-fleet-testing.md`, `mngr-slice-fleet-canary-followups.md`, `mngr-finish-new-fleet-canary-testing.md`, `mngr-identify-new-fleet-issues.md`, `mngr-design-network-observation.md`, `mngr-onetun-install-and-canary.md`, `mngr-lima-rename.md`, `mngr-ssh-authority-in-vault.md`, `mngr-remove-init-via-dhcp.md`, `mngr-mirror-upstream-artifacts.md`

Gen-2 small follow-ups: the historical slice-fleet gen-2 spec and the pre-cutover / variable-sizing blueprints note that the per-tier `management_plane.toml` they describe was merged into `deploy.toml` as the `[management_plane]` table.

The `server-order` recipe's example names the current OVH plan code `24sys03-v1-us` (the `24sys032-us` code left the eco catalog).

The `minds-dev-workflow` skill now states that one machine must never run two minds instances against the same env at once (each instance's `mngr latchkey forward` supervisor provisions the same remote machines, and the agents there lose their permission channel with "Unauthorized"), that `just minds-stop` deliberately leaves that supervisor running, and that `uv run minds-admin env stop-local <env>` stops an env root completely.
