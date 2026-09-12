# Pre-cutover fleet fixes (PR A)

Base branch: `new-fleet-phase-5.5`. The small, independent fixes from the gen-2 review that
should land before the fleet cutover. The larger items were split into their own issues and
are not part of this plan: renames (#848), DHCP placement / no cloud-init replay (#849), SSH
certificates from Vault (#850), artifact mirror (#851).

## Overview

- **Drain becomes a permanent box-maintenance primitive.** `server drain` and the `draining`
  status stay after the cutover (kernel reboots, disk failures, hardware retirement all need
  "stop everything here, exclude from placement"); a new `server undrain` reverses the status.
  Phase 6 of the cutover plan stops listing them for deletion.
- **`uplink_mbps` is always set.** Today no ordering path stamps it, so gen-2 boxes run with no
  fair-share shaping, no egress signal, and no link-speed audit. `server order` derives it from
  the ordered bandwidth option, `register` requires it, and a migration backfills the fleet
  (one 1 Gbps plan everywhere) and makes the column NOT NULL.
- **The shaper gets headroom.** The HTB root class is sized at 95% of the nominal uplink so the
  shaper, not the NIC queue, is the bottleneck; the stored value stays nominal for the audit.
- **Region labels are cross-checked once and mapped once.** The bake refuses a lease-region
  label that does not match the box's datacenter, and the connector's private label map is
  replaced by the shared `OVH_DATACENTER_CODE_BY_US_REGION`, which moves into the plugin's
  `gen2_scripts` subpackage (the only part of the plugin the connector container mounts).
- **Gen-1 boot disk back to 32 GiB.** Cutover phase 1 dropped the shared constant to 20 to fit
  gen-2 machines; gen-2 has since gotten its own constant, so the gen-1 value silently
  diverged from `main` and from every deployed gen-1 slice (all baked at 32).
- **The staging/production management-plane lockdown gets a checklist.** Docs only; the tomls
  are committed at rollout time. Gated on #850 so no production gen-2 box ever authorizes a
  fleet-wide key while locked down.

## Expected behavior

Operator-visible:

- `minds-admin server undrain --server-id <id>` moves a `draining` box back to `ready` and
  refuses any other status with a usage error. It changes nothing else (rows destroyed by the
  drain stay destroyed; stopped workspaces restore wherever the fleet places them).
- `minds-admin server set-status --status draining` is refused with a pointer to `server drain`
  (drain also empties the box; a bare status flip would leave `available` rows leasable). Every
  other status stays settable.
- `minds-admin server order` prints the derived uplink in the confirmation line and stamps it on
  the `ordered` row. When the plan's bandwidth option code does not carry a rate (or the plan
  offers no bandwidth family), the order is refused with the selected option codes listed, the
  cart is deleted, and nothing is charged; `--uplink-mbps N` is the explicit override for that
  case.
- `minds-admin server register --uplink-mbps` is required for every generation.
- `minds-admin server list` shows an `UPLINK` column (Mbit/s).
- `minds-admin pool create` refuses (before any Vault read or clone) when the `--region` label
  maps to a different datacenter than the box row's `region`, or when the box's datacenter is
  not among the shared map's datacenter codes (`OVH_US_DATACENTER_CODES`).
- `minds-admin server register --region` refuses a datacenter code not in the shared map.
  `import-boxes` refuses to import a source row whose datacenter is not in the map.
- `host-pool-setup.md` gains a "Box maintenance" section: drain, do the work, undrain.
- `gen2-cutover.md`'s prerequisites gain the per-tier lockdown steps; `next_deploy.md` gains one
  must-happen item per tier plus the new migration in the existing migrations item.

System behavior:

- On a gen-2 box the helper's root and default HTB classes are `rate = ceil = floor(uplink x
  0.95)` mbit; each machine's guarantee is `shaped x units / total_units` (min 1 mbit) with
  `ceil = shaped`. The telemetry `EGRESS_RATE` threshold and the link-speed audit keep using the
  nominal `uplink_mbps`.
- After migration 040 every `bare_metal_servers` row has `uplink_mbps` set (backfilled to 1000
  where NULL) and the column is NOT NULL, so an insert without it fails at the database.
- Gen-1 bakes carve a 32 GiB boot disk again; gen-2 carves are unchanged (10 GiB).
- The connector's restore candidate filter maps the row's lease-region label through the shared
  map; behavior is unchanged for the two known regions.
- Nothing in this PR changes the lease, stop/start, or cutover flows.

## Implementation plan

### `libs/mngr_imbue_cloud`

- `slices/gen2_scripts/sizing.py`
  - `SLICE_BOOT_DISK_GIB` 20 -> 32; rewrite its comment (gen-1 boot disk holds OS + docker;
    every deployed gen-1 slice was carved at 32; migration 039 assumes it).
  - Add `GEN2_UPLINK_SHAPING_PERCENT: Final[int] = 95` next to the other gen-2 constants,
    with a comment on why (HTB must be the bottleneck, not the NIC queue). An integer
    percentage, not a float fraction: the helper's bash can only do integer arithmetic, so
    one integer formula serves Python and bash alike.
  - No Python-side shaped-rate helper: the shaped rate is only ever computed on the box, in the
    slice helper's bash, from the per-slice `MNGR_SLICE_UPLINK_MBPS` env value.
- `slices/qemu_slice.py` (`render_slice_helper_script`, the `setup` verb's HTB block)
  - Compute `SHAPED_MBIT` from `MNGR_SLICE_UPLINK_MBPS` with the interpolated constant
    (`$(( MNGR_SLICE_UPLINK_MBPS * {GEN2_UPLINK_SHAPING_PERCENT} / 100 ))`, floor 1), and use it
    for the root class, the default class, and every per-machine `ceil`; the guarantee becomes
    `SHAPED_MBIT * units / total_units` (floor 1).
  - Keep the "skipped when no uplink is declared" branch: the env file still allows an empty
    value for gen-2 rows carved before this change; the column is now NOT NULL, so new carves
    always set it.
- `slices/gen2_scripts/regions.py` (new; like the rest of the subpackage it may import only what
  ships into the connector container: the stdlib, pydantic and `imbue_common`)
  - `OVH_DATACENTER_CODE_BY_US_REGION` moves here from `primitives.py`. The connector container
    mounts only `imbue.mngr_imbue_cloud.slices.gen2_scripts` (plus `imbue_common`), not
    `primitives`, so this is the one module both the plugin and the connector can import the
    map from.
- `primitives.py`
  - Import `OVH_DATACENTER_CODE_BY_US_REGION` from `slices.gen2_scripts.regions` and keep
    re-exporting it; `KNOWN_OVH_US_REGIONS`, `US_REGION_BY_OVH_DATACENTER_CODE` and
    `OVH_US_DATACENTER_CODES` stay derived from it here, so no other importer changes.
  - Rewrite the `SERVER_STATUS_DRAINING` comment: draining is the box-maintenance state (repave,
    reboot, repair), entered by `server drain` and left by `server undrain` (or a repave).

### `apps/remote_service_connector`

- `migrations/040_uplink_mbps_not_null.sql`
  - `UPDATE bare_metal_servers SET uplink_mbps = 1000 WHERE uplink_mbps IS NULL;` then
    `ALTER TABLE bare_metal_servers ALTER COLUMN uplink_mbps SET NOT NULL;` in one transaction,
    with 039's header pattern (why + apply command) plus the deploy-together requirement (bake
    tooling and connector from the same version, as `next_deploy.md` states for 039).
- `stop_start.py`
  - Delete `_REGION_LABEL_TO_DATACENTER`; `_list_candidate_boxes` uses
    `OVH_DATACENTER_CODE_BY_US_REGION` from `imbue.mngr_imbue_cloud.slices.gen2_scripts.regions`
    (not from `primitives`, which does not ship into the connector container; the transitive
    import guard in the connector's `test_project_ratchets.py` rejects that import).
  - `BoxRow.uplink_mbps` becomes `int` (no None); `_box_row_from_tuple` stops tolerating NULL.
- `testing.py`
  - The fake DB's `add_box` helper defaults `uplink_mbps` to 1000 instead of None.

### `apps/minds_admin`

- `slices/ordering.py`
  - `parse_uplink_mbps_from_bandwidth_option_code(option_code: str) -> int | None` (`@pure`):
    matches `^bandwidth-(\d+)-`; returns None otherwise. `vrack-bandwidth-...` does not match
    by construction.
  - `derive_uplink_mbps_from_option_codes(option_codes: Sequence[str]) -> int | None`: the
    parsed rate of the one `bandwidth-` code among the selected codes; None when absent or
    unparseable.
  - `build_and_assign_eco_cart` returns the selected option codes already; no change.
- `cli/server.py`
  - `order`: new `--uplink-mbps` (optional override). After `build_and_assign_eco_cart`,
    resolve `uplink = override or derive(option_codes)`; when None, `delete_cart_quietly` and
    raise a usage error listing the selected option codes and pointing at `--uplink-mbps`.
    Print the uplink in the confirmation line and stamp it on the `BareMetalServer` row.
  - `register`: `--uplink-mbps` becomes `required=True`; `--region` becomes
    `click.Choice(sorted(OVH_US_DATACENTER_CODES))`, the way `order` already validates it.
  - `import-boxes`: refuse (before any upsert) a source row whose `region` is not a known
    datacenter code; the uplink rides along on the copied row (source DB is migrated first).
  - `build_registered_server`: `uplink_mbps: int` (no default).
  - New `undrain` command: fetch the row; if status is not `draining`, usage error naming the
    current status; else `_update_server_fields(..., status=SERVER_STATUS_READY)`; emit JSON
    `{server_id, status}`.
  - `set-status`: refuse `draining` with a usage error pointing at `server drain`.
  - `drain` docstring: "for repave" -> box maintenance (repave, reboot, repair), reversed by
    `undrain`.
  - `_format_capacity_table`: add `UPLINK` (right-aligned, `<mbps>M`).
  - `_build_slice_create_args`: `slice_uplink_mbps` is always set (drop the `is not None`
    branch).
  - `prep` / `setup` composition: `declared_uplink_mbps: int` (drop the Optional through
    `build_gen2_box_prep_script` and `slices/box_telemetry.py`'s collector config; the collector's
    `or None` / `else 0` fallbacks go).
- `cli/pool.py`
  - After every existing cheap usage check (`KNOWN_OVH_US_REGIONS`, `--server-id`, the
    bake-source selectors, `--content-addressed-cache`) and immediately before the Vault read:
    fetch the box row and
    call a new `@pure assert_region_label_matches_box_datacenter(region_label, box_datacenter)`
    in `slices/bare_metal_db.py` (or `ordering.py`), which raises `BareMetalConfigError` (the
    exception `assert_gen2_box_disk_fits_default_machines` already uses for a refused box config)
    when the label maps to a different datacenter or the box's datacenter is unknown to the map.
    `pool create` catches it and reports it through `fail_with_json(..., error_class="UsageError")`
    like its other usage errors. The message names the label, the box's datacenter, and the
    expected label.
- `slices/bare_metal_db.py`
  - Row hydration: `uplink_mbps=int(row[20])` (no None branch).

### `libs/mngr_imbue_cloud/data_types.py`

- `BareMetalServer.uplink_mbps: int` (required). Any test factory building servers passes it.

### Docs and plans

- `blueprint/slice-fleet-cutover/plan-slice-fleet-cutover.md`: phase 6 no longer deletes
  `server drain`, `draining`, `SERVER_STATUS_DRAINING`, or the connector's `draining` handling
  (the connector's `ready` filter is the mechanism); the "after the deletion PR" bullet updated.
- `apps/minds/docs/deploy/host-pool-setup.md`: new "Box maintenance" section (drain -> wait for
  the box to report no rows -> reboot/repair -> undrain; note that drained workspaces restore
  elsewhere on their next start and do not come back to this box).
- `apps/minds/docs/deploy/gen2-cutover.md` prerequisites: per tier, "create the tier's Modal
  proxy in the environment named by `[modal_proxy].environment_name`; commit
  `envs/<tier>/management_plane.toml` (operators + proxy; since merged into `deploy.toml` as `[management_plane]`); `env deploy` the connector; then prep
  the gen-2 boxes", referencing the ordering in `gen2-management-plane.md`; gate: "#850 (SSH
  certificates) has landed before any tier's lockdown".
- `apps/minds/docs/deploy/next_deploy.md`: extend the migrations item to 034-040 with the
  uplink NOT NULL note (bake tooling and connector deploy together); one must-happen item per
  tier for the lockdown steps above.
- Command docs / `--help` text for `undrain`, `set-status`, `order --uplink-mbps`,
  `register --uplink-mbps`, `list`.
- Changelog entries: `libs/mngr_imbue_cloud`, `apps/remote_service_connector`,
  `apps/minds_admin`, `apps/minds`, `dev` (blueprint edit).

## Implementation phases

1. **Constants and the shaper.** `SLICE_BOOT_DISK_GIB` back to 32; shaping fraction constant +
   helper; helper script HTB block uses the shaped rate. Helper-script assertions updated. Working
   system: gen-1 carves at 32 again, gen-2 shaping has headroom, nothing else changes.
2. **Uplink always set.** Migration 040; `order` derivation + override; `register` required;
   `BareMetalServer.uplink_mbps: int` and every Optional dropped through the connector, prep,
   telemetry, and bake overrides; `list` column. Working system: every row has an uplink; a
   pre-migration checkout can no longer insert a NULL.
3. **Region checks.** Shared map moved into `gen2_scripts` and imported by the connector; bake
   mismatch refusal; `register` / `import-boxes` datacenter validation.
4. **Drain as maintenance.** `undrain`; `set-status` refuses `draining`; drain docstring;
   primitives comment; phase-6 plan amendment; host-pool-setup section.
5. **Rollout docs.** `gen2-cutover.md` prerequisites, `next_deploy.md` items, changelogs.

Each phase is a separate commit (or a few); the PR is one draft PR against
`new-fleet-phase-5.5`.

## Testing strategy

Unit tests only (decided in Q&A); live box verification happens in the later rollout testing.

- `sizing_test.py`: `GEN2_UPLINK_SHAPING_PERCENT` and the boot-disk constants (32 / 10) pinned.
- `qemu_slice_test.py`: the helper script is checked by substring assertions, not a snapshot;
  the existing `GUARANTEED_MBIT=$(( MNGR_SLICE_UPLINK_MBPS * ... ))` assertion in
  `test_helper_script_installs_the_enforced_ceilings_and_management_block` is updated to the
  shaped form, plus a targeted assertion that the root/default classes and per-machine `ceil`
  render the shaped variable and the guarantee derives from it.
- `ordering_test.py`: `parse_uplink_mbps_from_bandwidth_option_code` on
  `bandwidth-1000-unguaranteed-rise-gen2-us` (1000), `bandwidth-3000-...` (3000),
  `vrack-bandwidth-1000-...` (None), unrelated codes (None); `derive_uplink_mbps_from_option_codes`
  with and without a bandwidth code.
- `server_test.py`: `register` requires a positive `--uplink-mbps` and refuses an unknown
  datacenter before any DB connection; `set-status draining` is refused; `order --uplink-mbps` and
  `undrain` are on the CLI surface; `list` renders the `UPLINK` column (substring assertions on the
  rendered table). `order` (`resolve_ovh_config` + a live OVH client), `undrain` and `import-boxes`
  (`psycopg2.connect` directly) have no injection seam, the same situation as `pool create` below,
  so their wiring is exercised only up to the pre-DB usage checks; the uplink derivation they rely
  on is covered in `ordering_test.py`.
- `bare_metal_db_test.py` (or `ordering_test.py`, wherever the assertion lands):
  `assert_region_label_matches_box_datacenter` refused on label/datacenter mismatch, refused on
  unknown datacenter, passes on a match. `pool create` has no connection seam (`allocate_slices`
  and `_fetch_server_or_raise` call `psycopg2.connect` directly) and `pool_admin_test.py` drives
  the command only through `CliRunner` up to its pre-DB refusals, so the CLI wiring of the check
  is not exercised end-to-end there. Those existing cases pass a fake `--database-url` and a
  `--server-id`, which is why the row fetch must sit after every cheap usage check: placed any
  earlier it would turn the bake-source-selector refusals into a connection attempt.
- Connector `stop_start_test.py`: candidate filtering still matches `US-EAST-VA` -> `vin` and
  `US-WEST-OR` -> `hil` via the shared map; a NULL-free `BoxRow` hydrates.
- Connector migration test, following the existing text-level pattern (`entitlements_test.py`,
  `r2/stores_test.py` read the `.sql` and assert on its statements): 040 carries the
  `UPDATE ... SET uplink_mbps = 1000 WHERE uplink_mbps IS NULL` backfill and the `SET NOT NULL`.
  No connector test executes migration SQL, so the backfill's runtime behavior (1000 where NULL,
  set values untouched) is reviewed by hand and applied by `env deploy`.
- Ratchets: run `test_ratchets.py` (and, where present, `test_project_ratchets.py`) for the
  touched projects; no new violations expected. The connector's transitive import guard is
  what pins the `regions` import to the mounted subpackage.
- Full suite via `just test-offload` before finishing.

## Open questions

- **`import-boxes` and the uplink.** The Q&A settled on a required `--uplink-mbps` for imported
  boxes, but `import-boxes` copies whole rows from a source pool DB, so the uplink arrives on the
  row once the source DB has migration 040. The plan therefore adds no flag and instead requires
  the source DB to be migrated first (documented in the command's docstring). Confirm.
- **Order refusal timing.** OVH only exposes the eco options after a cart exists, so "refuse
  before building the cart" is implemented as "refuse after the cart is built and assigned but
  before checkout, then delete the cart" (the same non-charging path the dry run and the abort
  already use). Confirm.
- **Shaping percentage applied in bash.** The helper does integer arithmetic on the box, so the
  constant is an integer percentage (95) rather than a float fraction; the Python constant is
  the source of truth and a test pins that the rendered script carries it.
- **Gen-2 rows carved before this change** have an empty `MNGR_SLICE_UPLINK_MBPS` in their env
  file until their next placement (restore re-renders the env). The helper keeps its "skip
  shaping when empty" branch for them; no backfill on boxes.
